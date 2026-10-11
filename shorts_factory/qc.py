"""Quality control for scripts and rendered videos. Errors block publishing; warnings are reported."""
from __future__ import annotations

import io
import re
import subprocess
from pathlib import Path

import numpy as np

from .research import Topic
from .scripting import BANNED, VideoScript
from .state import State
from .utils import ffmpeg_exe, media_info, rms_dbfs

NUM_RE = re.compile(r"(?<![A-Za-z])\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> list[str]:
    out = []
    for m in NUM_RE.finditer(text):
        raw = m.group(0).replace(",", "")
        try:
            val = float(raw)
        except ValueError:
            continue
        if val <= 12 and float(val).is_integer():
            continue  # small counts like "3 steps" are not claims worth sourcing
        out.append(raw)
    return out


def check_script(script: VideoScript, topic: Topic, state: State, cfg) -> dict:
    errors, warnings = [], []
    text = script.narration() + " " + " ".join(s.get("on_screen", "") for s in script.scenes)
    source = re.sub(r",", "", topic.source_text())
    unverified = []
    for num in _numbers(text):
        if num not in source and num.rstrip("0").rstrip(".") not in source:
            unverified.append(num)
    if unverified:
        msg = f"numbers not found in source: {sorted(set(unverified))}"
        if cfg.qc.strict_numbers and len(set(unverified)) > int(cfg.qc.max_unverified_numbers):
            errors.append(msg)
        else:
            warnings.append(msg)
    low = text.lower()
    for b in BANNED:
        if b in low:
            errors.append(f"banned phrase in script: {b}")
    if not topic.verified:
        errors.append("source URL could not be verified")
    if not script.sources:
        errors.append("no sources")
    dup = state.similar_title(script.title, int(cfg.research.dedupe_days), float(cfg.research.title_similarity_threshold))
    if dup:
        errors.append(f"title too similar to a recent video: {dup!r}")
    # originality heuristic: narration must not copy long source spans verbatim
    src_low = topic.source_text().lower()
    copied = 0
    for s in script.scenes:
        words = s["narration"].lower().split()
        for i in range(0, max(len(words) - 11, 0), 4):
            if " ".join(words[i:i + 12]) in src_low:
                copied += 1
                break
    if copied >= 2:
        warnings.append(f"{copied} scenes contain 12+ word spans copied from the source")
    if copied >= 4:
        errors.append("script copies the source text too closely")
    return {"errors": errors, "warnings": warnings}


def _frame_std(path: Path, at: float) -> float:
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-ss", f"{at:.2f}", "-i", str(path),
                           "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
                          capture_output=True, timeout=120)
    if proc.returncode != 0 or not proc.stdout:
        return -1.0
    from PIL import Image
    img = Image.open(io.BytesIO(proc.stdout)).convert("L")
    return float(np.asarray(img, dtype=np.float32).std())


def check_video(path: Path, cfg, expected_duration: float, narration: np.ndarray, caption_count: int) -> dict:
    errors, warnings = [], []
    path = Path(path)
    if not path.exists():
        return {"errors": ["file missing"], "warnings": []}
    size_kb = path.stat().st_size / 1024
    if size_kb < float(cfg.qc.min_file_size_kb):
        errors.append(f"file too small: {size_kb:.0f} KB")
    info = media_info(path)
    v = cfg.video
    if (info["width"], info["height"]) != (int(v.width), int(v.height)):
        errors.append(f"unexpected resolution {info['width']}x{info['height']}")
    if info["vcodec"] != "h264":
        errors.append(f"unexpected video codec {info['vcodec']}")
    if info["acodec"] != "aac":
        errors.append(f"unexpected audio codec {info['acodec']}")
    if not (float(v.min_seconds) - 0.5 <= info["duration"] <= float(v.max_seconds) + 1.0):
        errors.append(f"duration {info['duration']:.1f}s outside {v.min_seconds}-{v.max_seconds}s")
    if abs(info["duration"] - expected_duration) > 1.5:
        warnings.append(f"duration {info['duration']:.1f}s differs from timeline {expected_duration:.1f}s")
    level = rms_dbfs(narration)
    if level < float(cfg.qc.min_rms_dbfs):
        errors.append(f"narration too quiet: {level:.1f} dBFS")
    if caption_count < 8:
        errors.append(f"too few caption chunks: {caption_count}")
    for frac in (0.15, 0.5, 0.85):
        std = _frame_std(path, info["duration"] * frac)
        if std < 0:
            errors.append("could not decode a frame")
            break
        if std < 8:
            errors.append(f"frame at {frac:.0%} looks blank (std {std:.1f})")
    return {"errors": errors, "warnings": warnings, "info": info, "size_kb": round(size_kb), "narration_dbfs": round(level, 1)}
