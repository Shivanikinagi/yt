# Tech Shorts Factory

Automated pipeline that researches the day's technology news, writes five original scripts,
narrates them with free text-to-speech, renders 1080x1920 motion-graphics Shorts with burned-in
word-synced captions, quality-checks every file, and uploads them to YouTube on a schedule.
Runs for ₹0 in the cloud on GitHub Actions, or locally on Windows as a fallback.

This folder is a standalone project. It does not depend on anything outside `trial/`.

## What was inspected and decided

| Component | Choice | Why |
|---|---|---|
| Orchestration | Python 3.12/3.13, single CLI (`python -m shorts_factory`) | Already installed; no services needed |
| Research | 14 RSS/Atom feeds (OpenAI, DeepMind, Google Research, Hugging Face, Microsoft Research, GitHub, The Verge, Ars Technica, MIT Tech Review, Quanta, arXiv cs.AI/cs.LG, Hacker News, Simon Willison) | Official feeds with publication dates; every candidate URL is re-fetched and must return HTTP < 400 |
| Script LLM | Groq free tier (`openai/gpt-oss-120b`, fallback `gpt-oss-20b`), then Ollama, GitHub Models, Gemini, OpenRouter, then a deterministic template writer | Your Claude Max plan has no API access and this project does not try to borrow its session. Groq key tested: 1000 requests/day, 8000 tokens/min |
| Narration | Piper (offline, MIT-licensed voice `en_US-lessac-medium`), optional Groq Orpheus, Windows SAPI, Edge read-aloud (opt-in) | Piper needs no key and runs on the CI runner CPU |
| Caption timing | Groq Whisper `whisper-large-v3-turbo` word timestamps, proportional timing fallback | Free, accurate word highlighting |
| Visuals | Procedural Pillow renderer: 5 themes (neon, voxel/game-inspired, blueprint, sunset, terminal), 7 scene types | Your PC has no GPU (Intel UHD) and the free runner has none either, so full AI video generation is out; motion graphics render at ~10 fps on CPU, ~2.5 min per video |
| Music | Synthesised pad generated in `audio.py` | Original by construction, no licensing risk |
| Encode | FFmpeg 7.1 bundled through `imageio-ffmpeg` (libx264 + aac) | No system install needed |
| Publishing | YouTube Data API v3, OAuth installed-app flow, resumable uploads | Official API; private uploads by default |
| Scheduler | GitHub Actions cron (cloud, always on) or Windows Task Scheduler (local) | See "Where it runs" |
| State | `state/state.json` + `reports/*.md`, committed back by the workflow | Duplicate prevention and resumable uploads survive across runs |

## Where it runs and what stops when your PC is off

**Recommended: GitHub Actions (cloud, free).** The workflow in `.github/workflows/daily-shorts.yml`
runs every day at 07:00 IST on a free `ubuntu-latest` runner (4 vCPU, 16 GB RAM, no GPU, 6 h job
limit). Nothing depends on your PC. A full run measured locally takes about 4 minutes per video, so
a 5-video day is roughly 20-25 minutes of runner time, well under the 120-minute cap.

Free-tier limits that matter:
- Private repositories get 2,000 free Actions minutes per month. At ~25 min/day that is ~750 min/month. Public repositories are unlimited.
- Artifacts (the MP4s and logs) are kept 7 days; storage is 500 MB on the free plan, so the workflow keeps only a week.
- The scheduled trigger can start 10-30 minutes late under load; the publish slots are hours apart so this does not matter.
- Scheduled workflows are disabled automatically after 60 days without any commit to the repo. The workflow commits `state/` and `reports/` every day, which keeps it alive.

**Fallback: Windows Task Scheduler (local).** `scheduler/register_windows_task.ps1` registers a
daily task. Everything (research, scripting, TTS, rendering, upload) runs on the laptop, so
**every job stops when the PC is shut down**; the task's `StartWhenAvailable` flag runs it the next
time the PC is on. There is no hybrid split: the cloud run is complete on its own.

Other free hosts were ruled out: Render/Railway/Fly free tiers sleep or are too small for FFmpeg
rendering; Google Colab/Kaggle cannot be scheduled reliably and forbid unattended automation;
Cloudflare Workers and Vercel functions have second-scale execution limits.

## Setup (one time, ~20 minutes)

```powershell
cd trial
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env         # then fill in keys (GROQ_API_KEY is already present in your .env)
.\.venv\Scripts\python -m shorts_factory doctor
.\.venv\Scripts\python -m shorts_factory demo   # renders one evergreen Short, no upload
```

### YouTube authorization (manual, once)
1. Google Cloud Console: create a project, enable **YouTube Data API v3**, configure the OAuth consent screen (External, add your Google account as a test user), create an **OAuth client ID** of type **Desktop app**, download the JSON.
2. Save it as `secrets/youtube_client_secret.json`.
3. Run `python -m shorts_factory auth`. A browser opens; approve the `youtube.upload` and `youtube.readonly` scopes. The refresh token is saved to `secrets/youtube_token.json` and a base64 copy is printed for GitHub.
4. Test: `python -m shorts_factory demo --upload` uploads one **private** video and records its ID and URL in `state/state.json` and `reports/latest.md`.

Unverified OAuth apps in "testing" mode issue refresh tokens that expire after 7 days. To avoid
re-authorizing weekly, either publish the consent screen (no verification is needed for an app
only you use with these scopes, Google just shows an "unverified app" warning once) or re-run
`auth` whenever the report says the token is not refreshable.

### Deploy to GitHub Actions
1. Create a GitHub repository (private is fine) and push this folder as its root:
   ```powershell
   git add -A
   git commit -m "Tech Shorts Factory"
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```
2. Repository **Settings → Secrets and variables → Actions → Secrets**:
   - `GROQ_API_KEY` (required for LLM scripts; without it the template writer is used)
   - `YOUTUBE_CLIENT_SECRET_JSON` (contents of the client JSON) and `YOUTUBE_TOKEN_JSON` (the base64 printed by `auth`)
   - optional: `GEMINI_API_KEY`, `OPENROUTER_API_KEY`
3. **Variables** (not secrets): `SHORTS_PUBLISH_MODE` = `private` (default) and later `scheduled`; `ALLOW_PUBLIC_SCHEDULING` = `false` until you are ready.
4. Settings → Actions → General → Workflow permissions: **Read and write** (needed to commit state/reports).
5. Run it once by hand: Actions → Daily Shorts → Run workflow (tick *no_upload* for a dry run). Check the artifact and `reports/latest.md`.

### Go public (your explicit decision)
Public scheduling is refused unless **both** `SHORTS_PUBLISH_MODE=scheduled` and
`ALLOW_PUBLIC_SCHEDULING=true` are set. Videos are then uploaded private with a `publishAt` time
at the next free slot (09:00, 12:00, 15:00, 18:00, 21:00 IST, at least 60 minutes ahead) and
YouTube flips them public automatically. Slots already booked in `state.json` are never reused.

## Daily run, step by step

1. `research`: fetch feeds, keep items published within 7 days, re-fetch each URL, pull a text excerpt from the article when the feed summary is thin, rank by source weight, recency, richness and hook words, cap 2 per source, drop anything similar to the last 120 days of titles, pad with evergreen topics (`config/evergreen_topics.yaml`) if fewer than 14 candidates remain.
2. `script`: the LLM gets only the source text and must stay within it; the JSON is validated (5-8 scenes, 95-165 narration words, scene shapes, banned phrases). Two attempts, then the template writer.
3. `script QC`: every number in the narration must appear in the source text (max 1 unverified), no 12-word spans copied verbatim, no duplicate titles, source URL verified.
4. `tts`: one WAV per scene (silence-trimmed, 48 kHz).
5. `render`: timeline with lead-in, gaps and tail (auto-squeezed if over 59 s), Whisper word timestamps, caption chunks, ducked synthetic music, frames piped to FFmpeg, `loudnorm` to -14 LUFS, faststart MP4, sidecar SRT.
6. `video QC`: 1080x1920, h264/aac, 25-59 s, file size, narration level, caption count, three decoded frames must not be blank.
7. `upload` (if authorized): resumable upload, quota tracking (1600 units each, budget 9000/day of the 10000 default), title/description/tags/category 28, `selfDeclaredMadeForKids=false`, `containsSyntheticMedia=true` plus a disclosure line in the description. Videos rendered but not uploaded are retried next run.
8. `report`: `reports/YYYY-MM-DD.md` and `.json`, `reports/latest.md`; `state/state.json` updated after every video so a crash loses at most one.

A failing topic never blocks the day: the runner moves to the next candidate and a topic that
fails twice is skipped in future. The run stops starting new videos when less than 15 minutes
remain and stops everything at 115 minutes; the workflow kills the job at 125.

## Commands

| Command | What it does |
|---|---|
| `python -m shorts_factory doctor` | environment, fonts, providers, credentials, state summary |
| `python -m shorts_factory research` | list today's candidate topics (no production) |
| `python -m shorts_factory demo [--limit N] [--upload]` | evergreen topics, no feeds, private upload only with `--upload`; demo videos never count toward the daily target and are not uploaded by `run` |
| `python -m shorts_factory run [--limit N] [--no-upload]` | the full daily run |
| `python -m shorts_factory upload` | upload previously rendered videos only |
| `python -m shorts_factory auth` | one-time YouTube OAuth |
| `python -m shorts_factory report` | print the latest report |
| `python -m pytest -q tests` | unit tests (no network) |

## Configuration

- `config/settings.yaml`: targets, time budget, video format, brand name/handle/CTA, theme list, LLM and TTS chains, QC thresholds, YouTube mode and slots.
- `config/sources.yaml`: feeds with kind and weight.
- `config/evergreen_topics.yaml`: fallback topics with canonical sources.
- `.env` / GitHub secrets: keys only. Never committed (`.gitignore`).

Change `brand.channel_name` and `brand.handle` before your first upload. Drop a TTF into
`assets/fonts/` as `Bold.ttf` / `Regular.ttf` / `Mono.ttf` to change typography.

## Costs

| Item | Cost |
|---|---|
| GitHub Actions (private repo, ~25 min/day) | ₹0 within the 2,000 min/month free tier |
| Groq API (scripts + Whisper alignment) | ₹0 free tier; ~10 chat and ~35 Whisper requests per day |
| Piper TTS, FFmpeg, Pillow | ₹0, open source |
| YouTube Data API | ₹0; 10,000 units/day default quota, 5 uploads use 8,000 |
| Optional: Gemini / OpenRouter / Ollama | ₹0 free tiers or local |

## Limitations and manual steps (read this)

- **Claude is not used.** Claude Max has no API; the pipeline uses Groq's free models and documented fallbacks.
- **YouTube authorization is manual** (OAuth consent in a browser) and may need repeating every 7 days while the Google OAuth app is in "testing" mode. The run reports this and keeps rendering; uploads resume once re-authorized.
- **The first private test upload has not been performed here** because no Google OAuth client existed in this environment. The upload code is implemented and unit-tested for metadata and slot logic; follow "YouTube authorization" and run `demo --upload`.
- **No AI video generation.** No GPU locally or on the free runner; visuals are motion graphics and diagrams, which is the documented fallback. The "voxel" theme is an original blocky look, not Minecraft assets.
- **Accuracy is bounded by the sources.** The LLM is confined to the feed text and article excerpt, numbers are cross-checked, and sources are linked in the description, but no automated system verifies claims against the wider world. Research-paper topics describe the abstract only.
- **Synthetic media flag.** The API field `status.containsSyntheticMedia` is sent; if the API rejects it the upload is retried without it and the description still carries the disclosure. Confirm the "altered or synthetic content" setting in YouTube Studio for the first few uploads.
- **Monetisation.** YouTube's reused-content and inauthentic-content policies can demonetise automated channels regardless of originality. Every script is original, every voice is synthetic and disclosed, and every source is cited, but acceptance is YouTube's decision.
- **Groq free-tier limits** (8,000 tokens/min) are handled with retry-after waits; a burst of failures falls back to the template writer, which only rearranges source sentences and is visibly plainer (and flagged in QC warnings).
- **Windows local mode** cannot run while the PC is off; there is no partial hybrid.
- **GitHub cron lateness** of up to 30 minutes is normal.
- **Fonts on the runner** are DejaVu/Liberation; locally Segoe UI. Supply your own TTF for a consistent brand.

## Verification performed in this environment (2026-10-11)

- `pytest`: 9 tests pass (template writer on all evergreen topics, validation, JSON extraction, captions/SRT, timeline, every scene type renders, QC number check, deadline, publish slot/metadata).
- Groq chat, Whisper word timestamps and Piper synthesis each verified live.
- Live research: 14 fresh, URL-verified candidates from 12 of 14 feeds (Hacker News timed out once; arXiv feeds were empty on a weekend).
- `demo`: three evergreen Shorts rendered in `output/2026-10-11/` (44-49 s each, 1080x1920 h264/aac, Whisper-aligned captions): one by the template writer, two by Groq `gpt-oss-120b`, themes neon/voxel/blueprint. All passed QC; reports and `state/state.json` written. Render time on this laptop: 100-200 s per video.
