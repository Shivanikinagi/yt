"""Shared helpers: logging, paths, subprocess, ffmpeg discovery, audio IO, JSON."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"
STATE_DIR = ROOT / "state"
REPORT_DIR = ROOT / "reports"
SECRETS_DIR = ROOT / "secrets"
MODELS_DIR = ROOT / "models"
LOG_DIR = ROOT / "output" / "logs"


class PipelineError(Exception):
    """A recoverable failure for one topic/video."""


class TimeBudgetExceeded(Exception):
    """Raised when the daily run is out of time."""


# ----------------------------------------------------------------------------- logging
def setup_logging(name: str = "run", level: int = logging.INFO) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("shorts")
    if logger.handlers:
        return logger
    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    fh = logging.FileHandler(LOG_DIR / f"{name}_{stamp}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.propagate = False
    return logger


# ----------------------------------------------------------------------------- project-relative paths
def rel_path(path) -> str:
    """Store paths relative to the project root (forward slashes), so state and reports never
    contain machine-specific folders such as the user's home or OneDrive directory."""
    for candidate in (Path(path), abs_path(str(path))):
        try:
            return candidate.resolve().relative_to(ROOT.resolve()).as_posix()
        except ValueError:
            continue
    return Path(path).as_posix()


def abs_path(stored: str) -> Path:
    """Resolve a stored path. Accepts project-relative paths and legacy absolute ones; a legacy
    absolute path from another machine is re-rooted at its `output/` segment."""
    s = str(stored)
    # absolute in either OS style (a Linux CI path must be recognised on Windows and vice versa)
    is_abs = s.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", s) is not None
    if not is_abs:
        return ROOT / s
    p = Path(s)
    if p.exists():
        return p
    parts = re.split(r"[\\/]+", s)
    if "output" in parts:
        return ROOT.joinpath(*parts[parts.index("output"):])
    return p


# ----------------------------------------------------------------------------- text
def slugify(text: str, maxlen: int = 48) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:maxlen].rstrip("-") or "video"


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def strip_html(text: str) -> str:
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))
    text = re.sub(r"&#\d+;|&\w+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9'’\-]+", text))


def extract_json(text: str) -> dict:
    """Return the first balanced JSON object found in text (tolerates code fences and prose)."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in response")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unbalanced JSON object in response")


# ----------------------------------------------------------------------------- time
def now_local(tz_name: str) -> datetime:
    return datetime.now(ZoneInfo(tz_name))


def today_str(tz_name: str) -> str:
    return now_local(tz_name).strftime("%Y-%m-%d")


# ----------------------------------------------------------------------------- json files
def read_json(path: Path, default=None):
    if not Path(path).exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data, indent: int = 2) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False, default=str)
    os.replace(tmp, path)


# ----------------------------------------------------------------------------- subprocess / ffmpeg
def ffmpeg_exe() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg  # bundled static binary, no install needed
    return imageio_ffmpeg.get_ffmpeg_exe()


def run(cmd: list[str], timeout: float = 600, input_bytes: bytes | None = None,
        check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, input=input_bytes, capture_output=True, timeout=timeout)
    if check and proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace")[-1500:]
        raise PipelineError(f"command failed ({proc.returncode}): {cmd[0]}\n{tail}")
    return proc


_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)")
_VID_RE = re.compile(r"Stream #\d+:\d+.*?Video:\s*(\w+).*?(\d{2,5})x(\d{2,5})")
_AUD_RE = re.compile(r"Stream #\d+:\d+.*?Audio:\s*(\w+)")


def media_info(path: Path) -> dict:
    """Probe a media file with ffmpeg (ffprobe is not bundled)."""
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)],
                          capture_output=True, timeout=60)
    text = proc.stderr.decode("utf-8", "replace")
    info = {"duration": 0.0, "width": 0, "height": 0, "vcodec": None, "acodec": None}
    m = _DUR_RE.search(text)
    if m:
        h, mi, s, frac = m.groups()
        info["duration"] = int(h) * 3600 + int(mi) * 60 + int(s) + float(f"0.{frac}")
    m = _VID_RE.search(text)
    if m:
        info["vcodec"], info["width"], info["height"] = m.group(1), int(m.group(2)), int(m.group(3))
    m = _AUD_RE.search(text)
    if m:
        info["acodec"] = m.group(1)
    return info


# ----------------------------------------------------------------------------- audio
def decode_audio(path: Path, sr: int = 48000) -> np.ndarray:
    """Decode any audio file to float32 mono samples at sr via ffmpeg."""
    proc = run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(path),
                "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", str(sr), "pipe:1"],
               timeout=300)
    data = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    return data


def write_wav(path: Path, samples: np.ndarray, sr: int) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    clipped = np.clip(samples, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def rms_dbfs(samples: np.ndarray) -> float:
    if samples.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(samples))))
    return 20 * np.log10(max(rms, 1e-9))


def env_flag(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}
