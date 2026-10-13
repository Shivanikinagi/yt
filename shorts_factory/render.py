"""Compose narration, music, captions and animated frames into a vertical MP4 with FFmpeg."""
from __future__ import annotations

import math
import re
import subprocess
import time
from itertools import groupby
from pathlib import Path

from .align import align, chunk_words, to_srt
from .audio import build_mix
from .scripting import VideoScript
from .tts import TTSResult
from .utils import PipelineError, ffmpeg_exe, write_wav
from .visuals import Renderer, source_meta, speaker_at


def build_timeline(durations: list[float], cfg, squeeze: bool = False) -> list[dict]:
    v = cfg.video
    lead = float(v.lead_in_seconds)
    gap = 0.15 if squeeze else float(v.scene_gap_seconds)
    tail = 0.5 if squeeze else float(v.tail_seconds)
    out, cursor = [], 0.0
    for i, d in enumerate(durations):
        start = cursor
        audio_start = start + (lead if i == 0 else gap)
        end = audio_start + d + (tail if i == len(durations) - 1 else 0.0)
        out.append({"start": start, "audio_start": audio_start, "audio_dur": d, "end": end})
        cursor = end
    return out


def _norm(word: str) -> str:
    return re.sub(r"[^a-z0-9]", "", word.lower())


def cue_times(items: list[str], timed: list[tuple], offset: float) -> list[float] | None:
    """Scene-local times at which the narration first mentions each item, in order.

    Lets a bullet or diagram node appear when the voice reaches it instead of on a fixed timer.
    Words match on a 5-letter prefix so "strengths" finds "strength". None if any item is not found."""
    words = [(_norm(w), s) for w, s, _ in timed]
    cues, pos = [], 0
    for item in items:
        keys = [k for k in (_norm(w) for w in item.split()) if len(k) >= 4]
        hit = next((j for j in range(pos, len(words)) if len(words[j][0]) >= 4
                    and any(words[j][0][:5] == k[:5] for k in keys)), None)
        if hit is None:
            return None
        cues.append(offset + words[hit][1])
        pos = hit + 1
    return cues


def finish_ffmpeg(proc: subprocess.Popen, timeout: float = 600) -> bytes:
    """Close ffmpeg's stdin, wait for it to exit and return its stderr.

    Popen.communicate() must not be called after stdin was closed by hand: on Linux with
    Python <= 3.12 it flushes the closed pipe and raises "ValueError: flush of closed file"
    (Windows and Python 3.13 tolerate it, so this only failed on the GitHub runner).
    ffmpeg runs with -loglevel error, so stderr is tiny and reading it after exit cannot deadlock."""
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
    except (BrokenPipeError, OSError, ValueError):
        pass
    proc.stdin = None
    err = b""
    try:
        if proc.stderr is not None:
            err = proc.stderr.read() or b""
    except (OSError, ValueError):
        pass
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    return err


def render_video(script: VideoScript, audio: list[TTSResult], out_dir: Path, cfg, logger,
                 deadline=None, seed: int = 0) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    v = cfg.video
    durations = [a.duration for a in audio]
    timeline = build_timeline(durations, cfg)
    total = timeline[-1]["end"]
    if total > float(v.max_seconds):
        timeline = build_timeline(durations, cfg, squeeze=True)
        total = timeline[-1]["end"]
    if total > float(v.max_seconds) + 0.5:
        raise PipelineError(f"video too long: {total:.1f}s > {v.max_seconds}s")
    if total < float(v.min_seconds):
        raise PipelineError(f"video too short: {total:.1f}s < {v.min_seconds}s")

    # --- captions (word timing per scene, shifted to global time)
    methods, views, chunks = set(), [], []
    max_w, max_c = int(cfg.captions.max_words_per_chunk), int(cfg.captions.max_chars_per_chunk)
    for scene, a, tl in zip(script.scenes, audio, timeline):
        timed, method = align(scene["narration"], a.path, a.duration, cfg, logger)
        methods.add(method)
        offset = tl["audio_start"] - tl["start"]
        view = dict(scene)
        # dialogue: who talks when, in scene-local seconds
        turns = [(who, offset + f0 * a.duration, offset + f1 * a.duration) for who, f0, f1 in (a.turns or [])]
        if turns:
            view["_turns"] = turns
        items = scene.get("bullets") or scene.get("nodes")
        cues = cue_times(items, timed, offset) if items else None
        if cues:
            view["_cues"] = cues
        views.append(view)
        # a caption chunk never spans two speakers, so the bubble always belongs to one character
        for who, group in groupby(timed, key=lambda w: speaker_at(turns, offset + w[1])):
            shifted = [(w, s + tl["audio_start"], e + tl["audio_start"]) for w, s, e in group]
            for c in chunk_words(shifted, max_w, max_c):
                c["who"] = who
                chunks.append(c)
    srt_path = out_dir / "captions.srt"
    srt_path.write_text(to_srt(chunks), encoding="utf-8")

    # --- audio mix
    sr = int(cfg.tts.sample_rate)
    mix, narration = build_mix([a.path for a in audio], [tl["audio_start"] for tl in timeline], total, sr,
                               float(cfg.music.gain_db), bool(cfg.music.enabled), seed)
    mix_path = out_dir / "mix.wav"
    write_wav(mix_path, mix, sr)

    # --- video frames -> ffmpeg
    fps = int(v.fps)
    W, H = int(v.width), int(v.height)
    n_frames = int(math.ceil(total * fps))
    out_mp4 = out_dir / "video.mp4"
    cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps), "-i", "pipe:0",
           "-i", str(mix_path),
           "-c:v", "libx264", "-preset", str(v.preset), "-crf", str(v.crf), "-pix_fmt", "yuv420p",
           "-profile:v", "high", "-level", "4.2", "-r", str(fps),
           "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
           "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
           "-shortest", "-movflags", "+faststart",
           "-metadata", f"title={script.title}", "-metadata", "comment=Generated by Tech Shorts Factory",
           str(out_mp4)]
    renderer = Renderer(cfg, script.theme, seed=seed, scenes=script.scenes, meta=source_meta(script))
    label = script.category or "Tech"
    t0 = time.time()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    chunk_idx = 0
    scene_idx = 0
    pipe_broken = False
    try:
        for i in range(n_frames):
            t = i / fps
            while scene_idx < len(timeline) - 1 and t >= timeline[scene_idx]["end"]:
                scene_idx += 1
            tl = timeline[scene_idx]
            t_local = t - tl["start"]
            while chunk_idx < len(chunks) - 1 and t >= chunks[chunk_idx]["end"] + 0.05:
                chunk_idx += 1
            cap = chunks[chunk_idx] if chunks and chunks[chunk_idx]["start"] - 0.05 <= t < chunks[chunk_idx]["end"] + 0.05 else None
            # no crossfade: the frame chrome stays put and only the panel contents change, like an edited cut
            img = renderer.frame(views[scene_idx], t_local, tl["end"] - tl["start"], t, t / total, label, cap,
                                 index=scene_idx)
            try:
                proc.stdin.write(img.tobytes())
            except (BrokenPipeError, OSError):
                pipe_broken = True   # ffmpeg exited early; its stderr below says why
                break
            if i % 90 == 0:
                if deadline is not None:
                    deadline.check("rendering")
                if i and i % 450 == 0:
                    logger.info("  frames %d/%d (%.1f fps)", i, n_frames, i / max(time.time() - t0, 1e-3))
    except BaseException:
        proc.kill()
        raise
    finally:
        err = finish_ffmpeg(proc)
    if proc.returncode != 0 or pipe_broken:
        raise PipelineError(f"ffmpeg encode failed (exit {proc.returncode}): {err.decode('utf-8', 'replace')[-800:]}")
    mix_path.unlink(missing_ok=True)
    elapsed = time.time() - t0
    logger.info("rendered %s (%.1fs video in %.0fs, captions=%s)", out_mp4.name, total, elapsed, "+".join(sorted(methods)))
    return {"path": str(out_mp4), "duration": total, "frames": n_frames, "captions": len(chunks),
            "srt": str(srt_path), "render_seconds": elapsed, "caption_method": "+".join(sorted(methods)),
            "narration": narration}
