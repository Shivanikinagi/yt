"""Audio assembly: original procedural background music + narration timeline mix."""
from __future__ import annotations

import numpy as np

from .utils import decode_audio

# Chord progressions (semitone offsets from root) for a calm, modern pad. All original.
PROGRESSIONS = [
    [[0, 4, 7, 11], [5, 9, 12, 16], [7, 11, 14, 17], [2, 5, 9, 12]],   # I  IV V  ii (maj7 colour)
    [[0, 3, 7, 10], [8, 12, 15, 19], [5, 8, 12, 15], [3, 7, 10, 14]],  # minor, cinematic
    [[0, 4, 7, 14], [9, 12, 16, 19], [5, 9, 12, 16], [7, 11, 14, 19]], # I vi IV V with 9ths
]


def _one_pole_lowpass(x: np.ndarray, sr: int, cutoff: float) -> np.ndarray:
    """Cheap smoothing lowpass (Hann-windowed moving average) - vectorised, no scipy needed."""
    width = max(3, int(sr / cutoff))
    kernel = np.hanning(width).astype(np.float32)
    kernel /= kernel.sum()
    return np.convolve(x, kernel, mode="same").astype(np.float32)


def synth_music(duration: float, sr: int, seed: int) -> np.ndarray:
    """Soft synth pad with a slow pulse. Generated from scratch, royalty-free by construction."""
    rng = np.random.default_rng(seed)
    prog = PROGRESSIONS[seed % len(PROGRESSIONS)]
    root_hz = [110.0, 123.47, 130.81, 146.83][seed % 4]
    bar = 4.0
    n = int(duration * sr) + sr
    t = np.arange(n) / sr
    out = np.zeros(n, dtype=np.float32)
    for bar_idx in range(int(np.ceil((duration + 1) / bar))):
        chord = prog[bar_idx % len(prog)]
        s0, s1 = int(bar_idx * bar * sr), min(int((bar_idx + 1) * bar * sr) + int(0.8 * sr), n)
        seg_t = t[s0:s1] - t[s0]
        env = np.minimum(seg_t / 0.9, 1.0) * np.exp(-np.maximum(seg_t - (bar - 0.3), 0) * 2.5)
        seg = np.zeros_like(seg_t)
        for semis in chord:
            f = root_hz * 2 ** (semis / 12)
            detune = 1 + rng.uniform(-0.002, 0.002)
            seg += np.sin(2 * np.pi * f * detune * seg_t) * 0.5
            seg += np.sin(2 * np.pi * f * 2 * seg_t + 0.3) * 0.12
            seg += np.sin(2 * np.pi * f * 0.5 * seg_t) * 0.25
        out[s0:s1] += (seg * env).astype(np.float32)
    # slow pulse + gentle lowpass for warmth
    pulse = 0.82 + 0.18 * np.sin(2 * np.pi * 0.5 * t)
    out *= pulse.astype(np.float32)
    out = _one_pole_lowpass(out, sr, 1200.0)
    out = out[: int(duration * sr)]
    # fade in / out
    fade = int(1.5 * sr)
    if len(out) > 2 * fade:
        out[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)
        out[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)
    peak = float(np.max(np.abs(out))) or 1.0
    return (out / peak * 0.9).astype(np.float32)


def moving_average(x: np.ndarray, width: int) -> np.ndarray:
    """Same as np.convolve(x, ones(width) / width, "same") but O(n): a 10-minute track has 29M samples."""
    width = max(int(width), 1)
    c = np.concatenate(([0.0], np.cumsum(x, dtype=np.float64)))
    end = np.arange(len(x)) + (width - 1) // 2 + 1          # window of output i: x[end - width : end]
    lo, hi = np.clip(end - width, 0, len(x)), np.clip(end, 0, len(x))
    return ((c[hi] - c[lo]) / width).astype(np.float32)


def db_to_gain(db: float) -> float:
    return float(10 ** (db / 20))


def build_mix(scene_wavs: list, starts: list[float], total: float, sr: int, music_gain_db: float,
              music_enabled: bool, seed: int) -> tuple[np.ndarray, np.ndarray]:
    """Place each narration clip at its start time; add ducked music. Returns (mix, narration_only)."""
    n = int(np.ceil(total * sr))
    narration = np.zeros(n, dtype=np.float32)
    for wav, start in zip(scene_wavs, starts):
        samples = decode_audio(wav, sr)
        i0 = int(start * sr)
        i1 = min(i0 + len(samples), n)
        narration[i0:i1] += samples[: i1 - i0]
    peak = float(np.max(np.abs(narration))) or 1.0
    narration = narration / peak * 0.85
    mix = narration.copy()
    if music_enabled:
        music = synth_music(total, sr, seed) * db_to_gain(music_gain_db)
        # duck music a little while speech is present
        speech = moving_average(np.abs(narration), int(0.15 * sr))
        duck = 1.0 - 0.5 * np.clip(speech / (speech.max() or 1.0) * 3, 0, 1)
        if len(music) < n:
            music = np.pad(music, (0, n - len(music)))
        mix = mix + music[:n] * duck.astype(np.float32)
    return np.clip(mix, -1, 1).astype(np.float32), narration
