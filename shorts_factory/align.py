"""Word-level timing for captions. Uses Groq Whisper when available, else proportional timing."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

import requests

WORD_RE = re.compile(r"\S+")


def script_words(text: str) -> list[str]:
    return WORD_RE.findall(" ".join(text.split()))


def proportional(words: list[str], duration: float, lead: float = 0.05) -> list[tuple[str, float, float]]:
    """Spread words over the clip proportionally to their length (+ pause weight for punctuation)."""
    if not words:
        return []
    weights = []
    for w in words:
        wt = len(re.sub(r"[^A-Za-z0-9]", "", w)) + 1.5
        if w.endswith((".", "!", "?")):
            wt += 3.0
        elif w.endswith((",", ";", ":")):
            wt += 1.5
        weights.append(wt)
    total = sum(weights)
    usable = max(duration - lead, 0.2)
    out, t = [], lead
    for w, wt in zip(words, weights):
        d = usable * wt / total
        out.append((w, t, t + d))
        t += d
    return out


def whisper_words(wav_path: Path, cfg, logger) -> list[dict] | None:
    key = os.environ.get("GROQ_API_KEY")
    if not key or not cfg.captions.use_whisper_alignment:
        return None
    try:
        for attempt in range(3):
            with open(wav_path, "rb") as f:
                r = requests.post("https://api.groq.com/openai/v1/audio/transcriptions",
                                  headers={"Authorization": f"Bearer {key}"},
                                  files={"file": (wav_path.name, f, "audio/wav")},
                                  data={"model": cfg.captions.whisper_model, "response_format": "verbose_json",
                                        "timestamp_granularities[]": "word", "language": "en"},
                                  timeout=90)
            if r.status_code != 429 or attempt == 2:
                break
            # free tier allows ~20 requests a minute; a long video has ~45 scenes, so wait it out
            try:
                wait = min(float(r.headers.get("retry-after") or 10), 30.0)
            except ValueError:
                wait = 10.0
            time.sleep(wait)
        if r.status_code >= 400:
            logger.warning("whisper HTTP %s: %s", r.status_code, r.text[:160])
            return None
        words = r.json().get("words") or []
        return words or None
    except Exception as e:
        logger.warning("whisper alignment failed: %s", str(e)[:160])
        return None


def align(text: str, wav_path: Path, duration: float, cfg, logger) -> tuple[list[tuple[str, float, float]], str]:
    """Return [(word, start, end)] in clip-local seconds plus the method used."""
    words = script_words(text)
    ww = whisper_words(wav_path, cfg, logger)
    if ww and len(words) > 0:
        n_s, n_w = len(words), len(ww)
        if abs(n_s - n_w) <= max(2, int(0.25 * n_s)):
            out = []
            for i, w in enumerate(words):
                j = round(i * (n_w - 1) / max(n_s - 1, 1))
                start = float(ww[j].get("start", 0.0))
                end = float(ww[j].get("end", start + 0.2))
                out.append((w, min(start, duration), min(max(end, start + 0.08), duration)))
            # enforce monotonic non-overlapping times
            for i in range(1, len(out)):
                w, s, e = out[i]
                ps, pe = out[i - 1][1], out[i - 1][2]
                if s < pe:
                    s = pe
                if e < s + 0.05:
                    e = s + 0.05
                out[i] = (w, s, min(e, duration))
            return out, "whisper"
        logger.info("whisper word count %d vs script %d, using proportional timing", n_w, n_s)
    return proportional(words, duration), "proportional"


def chunk_words(timed: list[tuple[str, float, float]], max_words: int, max_chars: int) -> list[dict]:
    """Group timed words into caption chunks: [{'words': [(w,s,e)...], 'start','end'}]."""
    chunks, cur = [], []

    def flush():
        if cur:
            chunks.append({"words": list(cur), "start": cur[0][1], "end": cur[-1][2]})
            cur.clear()
    for w, s, e in timed:
        if cur and (len(cur) >= max_words or sum(len(x[0]) + 1 for x in cur) + len(w) > max_chars):
            flush()
        cur.append((w, s, e))
        if w.endswith((".", "!", "?")) and len(cur) >= 2:
            flush()
    flush()
    return chunks


def to_srt(chunks_global: list[dict]) -> str:
    def ts(t: float) -> str:
        ms = int(round(t * 1000))
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        s, ms = divmod(ms, 1000)
        return f"{h:02}:{m:02}:{s:02},{ms:03}"
    lines = []
    for i, c in enumerate(chunks_global, 1):
        text = " ".join(w for w, _, _ in c["words"])
        lines += [str(i), f"{ts(c['start'])} --> {ts(c['end'])}", text, ""]
    return "\n".join(lines)
