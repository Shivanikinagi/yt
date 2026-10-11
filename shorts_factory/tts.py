"""Text-to-speech with a chain of free engines. Output is always a 48 kHz mono WAV."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import requests

from .utils import MODELS_DIR, PipelineError, ffmpeg_exe, run, wav_duration


@dataclass
class TTSResult:
    path: Path
    duration: float
    engine: str


_PIPER_VOICE = None


def _normalize(src: Path, dst: Path, sr: int) -> None:
    run([ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
         "-ac", "1", "-ar", str(sr), "-af", "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.15,"
                                          "areverse,silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.25,areverse",
         "-c:a", "pcm_s16le", str(dst)], timeout=120)


# ----------------------------------------------------------------------------- piper (offline)
def _piper_voice_paths(voice: str) -> tuple[Path, Path]:
    d = MODELS_DIR / "piper"
    return d / f"{voice}.onnx", d / f"{voice}.onnx.json"


def ensure_piper_voice(voice: str, logger) -> Path:
    onnx, meta = _piper_voice_paths(voice)
    if onnx.exists() and meta.exists():
        return onnx
    onnx.parent.mkdir(parents=True, exist_ok=True)
    logger.info("downloading piper voice %s ...", voice)
    proc = subprocess.run([sys.executable, "-m", "piper.download_voices", "--download-dir", str(onnx.parent), voice],
                          capture_output=True, text=True, timeout=900)
    if proc.returncode != 0 or not onnx.exists():
        raise PipelineError(f"piper voice download failed: {proc.stderr[-400:]}")
    return onnx


def _piper(text: str, out: Path, cfg, logger) -> None:
    global _PIPER_VOICE
    import wave
    from piper import PiperVoice
    model = ensure_piper_voice(cfg.tts.piper_voice, logger)
    if _PIPER_VOICE is None:
        _PIPER_VOICE = PiperVoice.load(str(model))
    syn_config = None
    try:
        from piper import SynthesisConfig
        syn_config = SynthesisConfig(length_scale=float(cfg.tts.piper_length_scale))
    except Exception:
        pass
    with wave.open(str(out), "wb") as w:
        if syn_config is not None:
            _PIPER_VOICE.synthesize_wav(text, w, syn_config=syn_config)
        else:
            _PIPER_VOICE.synthesize_wav(text, w)


# ----------------------------------------------------------------------------- groq orpheus (free tier, needs terms acceptance)
def _groq(text: str, out: Path, cfg, logger) -> None:
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise PipelineError("GROQ_API_KEY not set")
    r = requests.post("https://api.groq.com/openai/v1/audio/speech",
                      headers={"Authorization": f"Bearer {key}"},
                      json={"model": cfg.tts.groq_tts_model, "input": text, "voice": cfg.tts.groq_tts_voice,
                            "response_format": "wav"}, timeout=120)
    if r.status_code >= 400:
        raise PipelineError(f"groq tts HTTP {r.status_code}: {r.text[:200]}")
    out.write_bytes(r.content)


# ----------------------------------------------------------------------------- edge (unofficial endpoint, opt-in)
def _edge(text: str, out: Path, cfg, logger) -> None:
    import edge_tts
    mp3 = out.with_suffix(".mp3")

    async def go():
        await edge_tts.Communicate(text, cfg.tts.edge_voice).save(str(mp3))
    asyncio.run(go())
    run([ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(mp3), str(out)], timeout=120)
    mp3.unlink(missing_ok=True)


# ----------------------------------------------------------------------------- windows SAPI (last resort)
def _sapi(text: str, out: Path, cfg, logger) -> None:
    if os.name != "nt":
        raise PipelineError("SAPI only on Windows")
    safe = text.replace("'", "''")
    ps = (f"Add-Type -AssemblyName System.Speech; "
          f"$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; $s.Rate = 1; "
          f"$s.SetOutputToWaveFile('{out}'); $s.Speak('{safe}'); $s.Dispose()")
    proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                          capture_output=True, text=True, timeout=180)
    if proc.returncode != 0 or not out.exists():
        raise PipelineError(f"sapi failed: {proc.stderr[-300:]}")


ENGINES = {"piper": _piper, "groq": _groq, "edge": _edge, "sapi": _sapi}


def synthesize(text: str, out_wav: Path, cfg, logger) -> TTSResult:
    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    raw = out_wav.with_name(out_wav.stem + "_raw.wav")
    errors = []
    for name in cfg.tts.engines:
        fn = ENGINES.get(name)
        if not fn:
            continue
        try:
            fn(text, raw, cfg, logger)
            _normalize(raw, out_wav, int(cfg.tts.sample_rate))
            raw.unlink(missing_ok=True)
            dur = wav_duration(out_wav)
            if dur < 0.4:
                raise PipelineError("audio too short")
            return TTSResult(out_wav, dur, name)
        except Exception as e:
            errors.append(f"{name}: {str(e)[:160]}")
            logger.warning("tts %s failed: %s", name, str(e)[:160])
    raise PipelineError("all TTS engines failed: " + " | ".join(errors))
