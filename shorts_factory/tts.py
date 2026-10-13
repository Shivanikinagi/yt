"""Text-to-speech with a chain of free engines. Output is always a 48 kHz mono WAV."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import requests

from .utils import MODELS_DIR, PipelineError, decode_audio, ffmpeg_exe, run, wav_duration, write_wav

DIALOGUE_GAP_SECONDS = 0.22     # pause between two characters' lines


@dataclass
class TTSResult:
    path: Path
    duration: float
    engine: str
    # dialogue only: [(who, start_fraction, end_fraction)] of this clip, robust to later tempo changes
    turns: list | None = None


_PIPER_VOICES: dict = {}


def _normalize(src: Path, dst: Path, sr: int, pitch: float = 1.0) -> None:
    trim = ("silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.15,"
            "areverse,silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.25,areverse")
    if abs(pitch - 1.0) > 0.005:
        # raise/lower the voice without changing its speed (cartoon characters)
        trim = f"aresample={sr},asetrate={sr * pitch:.0f},aresample={sr},atempo={1 / pitch:.4f}," + trim
    run([ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
         "-ac", "1", "-ar", str(sr), "-af", trim, "-c:a", "pcm_s16le", str(dst)], timeout=120)


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


def _piper(text: str, out: Path, cfg, logger, voice: dict | None = None) -> None:
    import wave
    from piper import PiperVoice
    name = (voice or {}).get("piper") or cfg.tts.piper_voice
    if name not in _PIPER_VOICES:
        _PIPER_VOICES[name] = PiperVoice.load(str(ensure_piper_voice(name, logger)))
    piper_voice = _PIPER_VOICES[name]
    syn_config = None
    try:
        from piper import SynthesisConfig
        syn_config = SynthesisConfig(length_scale=float(cfg.tts.piper_length_scale))
    except Exception:
        pass
    with wave.open(str(out), "wb") as w:
        if syn_config is not None:
            piper_voice.synthesize_wav(text, w, syn_config=syn_config)
        else:
            piper_voice.synthesize_wav(text, w)


# ----------------------------------------------------------------------------- groq orpheus (free tier, needs terms acceptance)
def _groq(text: str, out: Path, cfg, logger, voice: dict | None = None) -> None:
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
def _edge(text: str, out: Path, cfg, logger, voice: dict | None = None) -> None:
    import edge_tts
    mp3 = out.with_suffix(".mp3")

    async def go():
        await edge_tts.Communicate(text, (voice or {}).get("edge") or cfg.tts.edge_voice).save(str(mp3))
    asyncio.run(go())
    run([ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(mp3), str(out)], timeout=120)
    mp3.unlink(missing_ok=True)


# ----------------------------------------------------------------------------- windows SAPI (last resort)
def _sapi(text: str, out: Path, cfg, logger, voice: dict | None = None) -> None:
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


def tempo_adjust(result: TTSResult, tempo: float, sr: int) -> TTSResult:
    """Speed a narration WAV up by `tempo` (e.g. 1.05 = 5 % faster) without changing pitch.
    Works for every engine, unlike Piper's length_scale which shortens pauses less than speech."""
    src = Path(result.path)
    tmp = src.with_name(src.stem + "_tempo.wav")
    run([ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
         "-af", f"atempo={tempo:.4f}", "-ac", "1", "-ar", str(sr), "-c:a", "pcm_s16le", str(tmp)], timeout=120)
    os.replace(tmp, src)
    return TTSResult(src, wav_duration(src), result.engine, result.turns)


def synthesize(text: str, out_wav: Path, cfg, logger, voice: dict | None = None) -> TTSResult:
    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    raw = out_wav.with_name(out_wav.stem + "_raw.wav")
    errors = []
    for name in cfg.tts.engines:
        fn = ENGINES.get(name)
        if not fn:
            continue
        try:
            fn(text, raw, cfg, logger, voice)
            # sapi/groq ignore the character voice, so only pitch-shift engines that honour it
            pitch = float((voice or {}).get("pitch", 1.0)) if name in ("piper", "edge") else 1.0
            _normalize(raw, out_wav, int(cfg.tts.sample_rate), pitch)
            raw.unlink(missing_ok=True)
            dur = wav_duration(out_wav)
            if dur < 0.4:
                raise PipelineError("audio too short")
            return TTSResult(out_wav, dur, name)
        except Exception as e:
            errors.append(f"{name}: {str(e)[:160]}")
            logger.warning("tts %s failed: %s", name, str(e)[:160])
    raise PipelineError("all TTS engines failed: " + " | ".join(errors))


def synthesize_lines(lines: list[dict], out_wav: Path, cfg, logger, voices: dict[str, dict]) -> TTSResult:
    """A conversation: each line in its speaker's voice, joined with short pauses into one clip.

    `lines` is [{"who": "host"|"guest", "text": ...}], `voices` maps who -> character voice spec.
    The result records who is talking when, so the renderer can animate the right character."""
    out_wav = Path(out_wav)
    sr = int(cfg.tts.sample_rate)
    gap = np.zeros(int(DIALOGUE_GAP_SECONDS * sr), np.float32)
    pieces, spans, engines, cursor = [], [], [], 0
    for k, line in enumerate(lines):
        part = out_wav.with_name(f"{out_wav.stem}_{k}.wav")
        res = synthesize(line["text"], part, cfg, logger, voice=voices.get(line["who"]))
        samples = decode_audio(part, sr)
        part.unlink(missing_ok=True)
        if pieces:
            pieces.append(gap)
            cursor += len(gap)
        pieces.append(samples)
        spans.append((line["who"], cursor, cursor + len(samples)))
        engines.append(res.engine)
        cursor += len(samples)
    write_wav(out_wav, np.concatenate(pieces), sr)
    turns = [(who, a / cursor, b / cursor) for who, a, b in spans]
    return TTSResult(out_wav, wav_duration(out_wav), "+".join(sorted(set(engines))), turns)


def speak_scene(scene: dict, out_wav: Path, cfg, logger, voices: dict[str, dict] | None = None) -> TTSResult:
    """Narrate one scene: as a dialogue when the video has a cast, otherwise with the default voice."""
    if voices:
        lines = scene.get("lines") or [{"who": "host", "text": scene["narration"]}]
        return synthesize_lines(lines, out_wav, cfg, logger, voices)
    return synthesize(scene["narration"], out_wav, cfg, logger)
