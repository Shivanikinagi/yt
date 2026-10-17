# Tech Shorts Factory

Automated pipeline that researches the day's technology news, writes original scripts, narrates
them with free text-to-speech, renders motion-graphics videos with burned-in word-synced captions,
quality-checks every file, and uploads them to YouTube on a schedule. Every day: **2 Shorts**
(1080x1920, under a minute) and **1 long-form video** (1920x1080, about 8-10 minutes). The counts
are `daily_target` and `long_video.daily_target` in `config/settings.yaml`.
Runs for ₹0 in the cloud on GitHub Actions, or locally on Windows as a fallback.

This folder is a standalone project. It does not depend on anything outside `trial/`.

## Status and what you still have to do (2026-10-11)

Done and verified: research, scripting (Groq), narration (Piper), rendering, captions, QC, reports,
state, unit tests, local demo videos, and the code is pushed to the private repository
`github.com/Shivanikinagi/yt` with the daily workflow in place.

Not done, because each needs your Google or GitHub account in a browser:

1. ~~Create the YouTube OAuth client and run `auth`~~ Done 2026-10-11, channel TechDecoded.
2. ~~Run the first private test upload~~ Done: https://youtube.com/shorts/EAQrZrhexs8 (private).
3. **Add the GitHub Actions secrets** `GROQ_API_KEY`, `YOUTUBE_CLIENT_SECRET_JSON`, `YOUTUBE_TOKEN_JSON`
   (repository Settings → Secrets and variables → Actions) and set workflow permissions to
   *Read and write*. Without `GROQ_API_KEY` the cloud run falls back to the plainer template writer.
4. **Trigger the workflow once by hand** (Actions → Daily Shorts → Run workflow, tick *no_upload*
   for a dry run) and check the job summary, which now shows the daily report.
5. Only when you are satisfied: set the repository variables `SHORTS_PUBLISH_MODE=scheduled` and
   `ALLOW_PUBLIC_SCHEDULING=true` to let videos go public on the schedule.

## What steers the content: the channel's own numbers and YouTube's trending charts

Each run starts by reading (read-only, about 5 quota units a day):

- view, like and comment counts of every video the pipeline uploaded, and
- YouTube's most-popular Science & Technology videos in the US, India and the UK.

Topics whose title words go with more views on this channel, or recur in trending titles, rank
higher. The script prompts also show the model the channel's best and weakest titles and the
biggest trending tech videos with their view counts, so titles follow what actually gets clicked.
The daily report has a "What's working" section, and `python -m shorts_factory insights` prints it
on demand. The pattern so far (2026-10-17): AI topics framed as a question with a stake for the
viewer ("Can AI tests outpace human benchmarks?", 158 views) beat obscure paper and product names
("EliteTable", "Probe-and-Resume", 0-2 views).

## Long-form videos

One a day, 16:9, the same two characters filmed as an 8-10 minute conversation with chapters.

- **Topics**: the strongest news story with enough material for a deep dive (the full article
  text, 3,000+ characters), otherwise an evergreen deep dive from
  `config/longform_topics.yaml`. Those are stories in the style of the most-watched tech explainers
  (the xz backdoor, the CrowdStrike outage, how ChatGPT works...), and their facts come from the
  linked Wikipedia articles, fetched on the day.
- **Script**: an outline call (title, thumbnail text, 5-7 chapters), then one call per chapter.
  This keeps each request inside Groq's free per-minute limit, and a bad chapter is retried on its
  own. There is no template fallback: if a long video fails, the day simply has fewer.
- **Video**: a cold-open hook, chapter cards with the guest's question, and cuts between close-ups
  and the info panel. YouTube chapter timestamps go in the description, and a custom 1280x720
  thumbnail is uploaded with it.

## Calls to action

Spoken asks are written by code, not by the model, so they stay short and never sound spammy.
They follow what works in creator tests: ask after the video has delivered something, give a
reason, and make the comment ask a real question.

- **Shorts**: in the last scene the guest asks the viewers a question about the topic ("...Tell us
  in the comments."), then the host signs off with one subscribe line. The shot cuts wide to
  animated LIKE / SHARE / SUBSCRIBE buttons.
- **Long-form**: a single like ask at about the halfway chapter, once the viewer has got value.
  At the end come a subscribe ask tied to the benefit, "send this to <someone specific>", and the
  comment question, over the same animated buttons.
- **Descriptions**: the comment question and a `?sub_confirmation=1` subscribe link.

YouTube's end-screen elements and the "related video" link on a Short can't be set through the
API. Add an end-screen template once in YouTube Studio (Settings → Upload defaults); the outro of
every long video leaves about 10 seconds for it.

## Daily X (Twitter) content, posted by hand

`python -m shorts_factory tweet` writes `tweets/<date>.md`: one tweet from the day's news (plus two
alternatives, length checked the way X counts it), 5 accounts to follow and a draft reply for each.
The GitHub workflow runs it every morning before the video job, shows it at the top of the job
summary and commits the file. In Claude Code, `/x-daily` shows today's content and `/x-engage`
drafts replies to comments you paste in.

Nothing posts, likes or follows automatically: X's automation rules ban automated likes and
proactive follows, and the X API is paid per post. Handles are never guessed. They come from
x.com links on the article pages or from GitHub profiles (`config/x_accounts.yaml` lists builders
by GitHub username), and an account is not suggested again for 30 days (`state/tweets.json`).

## What was inspected and decided

| Component | Choice | Why |
|---|---|---|
| Orchestration | Python 3.12/3.13, single CLI (`python -m shorts_factory`) | Already installed; no services needed |
| Research | 14 RSS/Atom feeds (OpenAI, DeepMind, Google Research, Hugging Face, Microsoft Research, GitHub, The Verge, Ars Technica, MIT Tech Review, Quanta, arXiv cs.AI/cs.LG, Hacker News, Simon Willison) | Official feeds with publication dates; every candidate URL is re-fetched and must return HTTP < 400 |
| Script LLM | Groq free tier (`openai/gpt-oss-120b`, fallback `gpt-oss-20b`), then Ollama, GitHub Models, Gemini, OpenRouter, then a deterministic template writer | Your Claude Max plan has no API access and this project does not try to borrow its session. Groq key tested: 1000 requests/day, 8000 tokens/min |
| Narration | Piper (offline, MIT-licensed voice `en_US-lessac-medium`), optional Groq Orpheus, Windows SAPI, Edge read-aloud (opt-in) | Piper needs no key and runs on the CI runner CPU |
| Caption timing | Groq Whisper `whisper-large-v3-turbo` word timestamps, proportional timing fallback | Free, accurate word highlighting |
| Visuals | Procedural Pillow renderer. Each Short is a conversation between two original characters (anime: Yuki, Sora, Hana, Ren; cartoon: Nova, Kai, Bit, Mochi), each with their own voice, filmed as close-ups and wide shots over a night-city bokeh, cutting to a HUD info panel (7 animated scene types, narration-synced reveals) for the facts. Pairs rotate per video (`video.cast_pairs`), `video.cinematic: false` keeps only the panel layout. Bundled OFL fonts (Archivo Black, IBM Plex) | Your PC has no GPU (Intel UHD) and the free runner has none either, so full AI video generation is out; motion graphics render at ~10 fps on CPU, ~2.5 min per video |
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
1. The code is already pushed to the private repository `https://github.com/Shivanikinagi/yt`
   (branch `main`). After any local change: `git add -A; git commit -m "..."; git push`.
2. Repository **Settings → Secrets and variables → Actions → Secrets**:
   - `GROQ_API_KEY` (required for LLM scripts; without it the template writer is used)
   - `YOUTUBE_CLIENT_SECRET_JSON` (contents of the client JSON) and `YOUTUBE_TOKEN_JSON` (the base64 printed by `auth`)
   - optional: `GEMINI_API_KEY`, `OPENROUTER_API_KEY`
3. **Variables** (not secrets): `SHORTS_PUBLISH_MODE` = `private` (default) and later `scheduled`; `ALLOW_PUBLIC_SCHEDULING` = `false` until you are ready.
4. Settings → Actions → General → Workflow permissions: **Read and write** (needed to commit state/reports).
5. Run it once by hand: Actions → Daily Shorts → Run workflow (tick *no_upload* for a dry run). Check the artifact and `reports/latest.md`.

### Monitoring (free)
- Every run writes the daily report into the GitHub **job summary** (Actions → the run), commits it to `reports/`, and attaches the MP4s, SRTs, scripts, QC results and logs as a 7-day artifact.
- The job is marked **failed** when a run produces zero videos. GitHub emails the repository owner when a scheduled workflow fails (Settings → Notifications → Actions), so a broken day is reported without any extra service.
- `python -m shorts_factory report` prints the latest report locally; `state/state.json` holds every video, its upload status, YouTube ID, publish time and quota use.

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
fails twice is skipped in future. Shorts are made first; a long video only starts with at least
24 minutes plus the upload reserve left. Everything stops at 140 minutes; the workflow kills the
job at 150.

## Commands

| Command | What it does |
|---|---|
| `python -m shorts_factory doctor` | environment, fonts, providers, credentials, state summary |
| `python -m shorts_factory research` | list today's candidate topics (no production) |
| `python -m shorts_factory demo [--limit N] [--long N] [--upload]` | evergreen topics, no feeds, private upload only with `--upload`; demo videos never count toward the daily target and are not uploaded by `run` |
| `python -m shorts_factory run [--limit N] [--long N] [--no-upload]` | the full daily run (`--limit` Shorts, `--long` long-form) |
| `python -m shorts_factory insights` | refresh view counts and trending videos, print what is working |
| `python -m shorts_factory upload` | upload previously rendered videos only |
| `python -m shorts_factory auth` | one-time YouTube OAuth |
| `python -m shorts_factory report` | print the latest report |
| `python -m pytest -q tests` | unit tests (no network) |

## Configuration

- `config/settings.yaml`: targets, time budget, video format, brand name/handle/CTA, theme list, LLM and TTS chains, QC thresholds, YouTube mode and slots.
- `config/sources.yaml`: feeds with kind and weight.
- `config/evergreen_topics.yaml`: fallback topics with canonical sources.
- `.env` / GitHub secrets: keys only. Never committed (`.gitignore`).

To keep a rendered video from ever being uploaded, change its `status` in `state/state.json` from
`rendered` to `held` (the 2026-10-11 newsletter-style video is held this way).

Change `brand.channel_name` and `brand.handle` before your first upload. Drop a TTF into
`assets/fonts/` as `Bold.ttf` / `Regular.ttf` / `Mono.ttf` to change typography.

## Costs

| Item | Cost |
|---|---|
| GitHub Actions (private repo, ~25 min/day) | ₹0 within the 2,000 min/month free tier |
| Groq API (scripts + Whisper alignment) | ₹0 free tier; ~10 chat and ~35 Whisper requests per day |
| Piper TTS, FFmpeg, Pillow | ₹0, open source |
| YouTube Data API | ₹0; 10,000 units/day default quota: 3 uploads 4,800, 1 thumbnail 50, analytics ~5 |
| Optional: Gemini / OpenRouter / Ollama | ₹0 free tiers or local |

## Limitations and manual steps (read this)

- **Claude is not used.** Claude Max has no API; the pipeline uses Groq's free models and documented fallbacks.
- **YouTube authorization is manual** (OAuth consent in a browser) and may need repeating every 7 days while the Google OAuth app is in "testing" mode. The run reports this and keeps rendering; uploads resume once re-authorized.
- **First private test upload done (2026-10-11 13:06 IST).** `demo --upload` rendered and uploaded "Why 128-bit hashes only give 64-bit collision security" to the TechDecoded channel as private: https://youtube.com/shorts/EAQrZrhexs8. The API accepted `containsSyntheticMedia` without error but does not return it when the video is read back, so confirm "Altered or synthetic content" in YouTube Studio for the first few uploads.
- **GitHub Actions secrets** are set and the cloud workflow has run end to end (5 private uploads on 2026-10-12).
- **Keep the local copy out of cloud-synced folders.** `output/` grows by hundreds of MB of MP4 and WAV files per week, and `.env` and `secrets/` hold API keys and the YouTube token. A synced folder would upload all of it and can lock files mid-run. The cloud run is unaffected by where the local copy lives.
- **No AI video generation.** No GPU locally or on the free runner; visuals are motion graphics and diagrams, which is the documented fallback. The "voxel" theme is an original blocky look, not Minecraft assets.
- **Accuracy is bounded by the sources.** The LLM is confined to the feed text and article excerpt, numbers are cross-checked, and sources are linked in the description, but no automated system verifies claims against the wider world. Research-paper topics describe the abstract only.
- **Synthetic media flag.** The API field `status.containsSyntheticMedia` is sent; if the API rejects it the upload is retried without it and the description still carries the disclosure. Confirm the "altered or synthetic content" setting in YouTube Studio for the first few uploads.
- **Monetisation.** YouTube's reused-content and inauthentic-content policies can demonetise automated channels regardless of originality. Every script is original, every voice is synthetic and disclosed, and every source is cited, but acceptance is YouTube's decision.
- **Groq free-tier limits** (8,000 tokens/min) are handled with retry-after waits; a burst of failures falls back to the template writer, which only rearranges source sentences and is visibly plainer (and flagged in QC warnings).
- **Custom thumbnails and videos over 15 minutes need a phone-verified channel** (youtube.com/verify).
  Until then the long-form upload still succeeds, YouTube picks a frame as the thumbnail, and the
  report says so. Long videos are capped at 14 minutes.
- **Windows local mode** cannot run while the PC is off; there is no partial hybrid.
- **GitHub cron lateness** of up to 30 minutes is normal.
- **Fonts on the runner** are DejaVu/Liberation; locally Segoe UI. Supply your own TTF for a consistent brand.

## Verification performed in this environment (2026-10-11)

- `pytest`: 11 tests pass (template writer on all evergreen topics, validation, JSON extraction, captions/SRT, timeline, every scene type renders, QC number check, deadline, publish slot/metadata, upload queue scoping, narration speed-up maths).
- Second audit pass (12:30 IST): Groq key re-verified against the models endpoint (`gpt-oss-120b`, `gpt-oss-20b`, `whisper-large-v3-turbo` all listed); live research produced 14 verified candidates with the newsletter penalty working; a fresh `demo` rendered "Why Large Language Models Forget Anything Beyond Their Window" (42 s, neon, Whisper captions, QC clean, 3.2 min total). One evergreen topic in that run was lost because a 164-word script spoke for 60 s; the pipeline now re-synthesizes narration up to 20 % faster instead of dropping the topic, and the prompt asks for at most 145 words.
- Fixed in that pass: `demo --upload` now uploads only the video it just rendered (it previously would have uploaded every old demo); demo runs write `reports/demo-<date>.md` instead of overwriting the daily report; the GitHub job fails (and so notifies you) when a run produces nothing, and the daily report is published to the job summary.
- Groq chat, Whisper word timestamps and Piper synthesis each verified live.
- Live research: 14 fresh, URL-verified candidates from 12 of 14 feeds (Hacker News timed out once; arXiv feeds were empty on a weekend).
- `demo`: three evergreen Shorts rendered in `output/2026-10-11/` (44-49 s each, 1080x1920 h264/aac, Whisper-aligned captions): one by the template writer, two by Groq `gpt-oss-120b`, themes neon/voxel/blueprint. All passed QC; reports and `state/state.json` written. Render time on this laptop: 100-200 s per video.
