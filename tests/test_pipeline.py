"""Fast unit tests (no network, no LLM, no YouTube). Run: .venv/Scripts/python -m pytest -q"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shorts_factory.align import chunk_words, proportional, to_srt  # noqa: E402
from shorts_factory.config import load_settings  # noqa: E402
from shorts_factory.research import Topic, evergreen_topics  # noqa: E402
from shorts_factory.scripting import MAX_WORDS, MIN_WORDS, ScriptError, template_script, validate_and_normalize  # noqa: E402
from shorts_factory.state import State  # noqa: E402
from shorts_factory.utils import extract_json  # noqa: E402
from shorts_factory.visuals import Renderer  # noqa: E402
from shorts_factory.qc import check_script  # noqa: E402
from shorts_factory.render import build_timeline  # noqa: E402
from shorts_factory.runner import Deadline, is_systemic_failure, tts_speedup_factor, upload_queue  # noqa: E402
from shorts_factory.utils import PipelineError  # noqa: E402
from shorts_factory.youtube import build_metadata, next_publish_slot  # noqa: E402

cfg = load_settings()


def _topic():
    return evergreen_topics("test")[0]


def test_template_script_is_valid_for_every_evergreen_topic():
    for t in evergreen_topics("seed"):
        s = template_script(t, "neon")
        assert MIN_WORDS <= s.total_words() <= MAX_WORDS, (t.title, s.total_words())
        assert s.scenes[0]["type"] == "title" and s.scenes[-1]["type"] == "payoff"


def test_validate_rejects_short_scripts():
    data = {"title": "A good title here", "scenes": [{"type": "title", "narration": "too short"}]}
    try:
        validate_and_normalize(data, _topic(), "test", "neon")
        assert False, "should reject"
    except ScriptError:
        pass


def test_extract_json_from_fenced_text():
    assert extract_json('here ```json {"a": 1, "b": {"c": "}"}} ``` done') == {"a": 1, "b": {"c": "}"}}


def test_captions_chunking_and_srt():
    words = proportional("Hello world. This is a longer caption test sentence!".split(), 4.0)
    chunks = chunk_words(words, 4, 24)
    assert chunks and chunks[0]["start"] >= 0 and chunks[-1]["end"] <= 4.0
    srt = to_srt(chunks)
    assert "-->" in srt


def test_timeline_and_squeeze():
    tl = build_timeline([5.0, 6.0, 7.0], cfg)
    assert tl[0]["audio_start"] == cfg.video.lead_in_seconds
    assert tl[-1]["end"] > 18.0
    tl2 = build_timeline([5.0, 6.0, 7.0], cfg, squeeze=True)
    assert tl2[-1]["end"] < tl[-1]["end"]


def test_renderer_frame_for_every_scene_type():
    r = Renderer(cfg, "voxel", seed=3)
    scenes = [
        {"type": "title", "on_screen": "Why floating point lies to you"},
        {"type": "explain", "on_screen": "The core idea", "bullets": ["Binary cannot store 0.1", "Errors add up"]},
        {"type": "diagram", "on_screen": "Flow", "nodes": ["Input", "Model", "Output"]},
        {"type": "compare", "on_screen": "Then vs now", "left_title": "RNN", "right_title": "Transformer",
         "left": ["Sequential"], "right": ["Parallel"]},
        {"type": "code", "on_screen": "Try it", "code": ["x = 0.1 + 0.2", "print(x)"]},
        {"type": "fact", "on_screen": "0.30000000000000004"},
        {"type": "payoff", "on_screen": "Now you know"},
    ]
    cap = {"words": [("Hello", 0.0, 0.5), ("world", 0.5, 1.0)], "start": 0.0, "end": 1.0}
    for s in scenes:
        img = r.frame(s, 1.2, 5.0, 3.0, 0.4, "Tech", cap)
        assert img.size == (1080, 1920)


def test_qc_flags_unsourced_numbers(tmp_path):
    t = _topic()
    s = template_script(t, "neon")
    s.scenes[1]["narration"] += " It costs exactly 4,567 dollars and 98.6 percent agree."
    st = State(tmp_path / "state.json")
    res = check_script(s, t, st, cfg)
    assert res["errors"], res


def test_deadline():
    d = Deadline(0.0001)
    import time
    time.sleep(0.02)
    assert d.remaining() < 0


def test_finish_ffmpeg_after_stdin_closed():
    """Regression: the cloud run failed every render with 'flush of closed file'."""
    import subprocess
    from shorts_factory.render import finish_ffmpeg
    child = "import sys; sys.stdin.buffer.read(); sys.stderr.write('done'); sys.exit(3)"
    proc = subprocess.Popen([sys.executable, "-c", child], stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    proc.stdin.write(b"x" * 1000)
    proc.stdin.close()                      # the renderer closes stdin before finishing
    err = finish_ffmpeg(proc, timeout=30)
    assert proc.returncode == 3 and b"done" in err


def test_systemic_failure_detection():
    same = [{"topic": f"t{i}", "error": "render: ValueError: flush of closed file"} for i in range(3)]
    assert is_systemic_failure(same)
    assert not is_systemic_failure(same[:2])
    mixed = same[:2] + [{"topic": "t9", "error": "script QC failed: numbers not found in source: ['42']"}]
    assert not is_systemic_failure(mixed)
    # durations differ between topics but it is the same failure
    too_long = [{"topic": f"t{i}", "error": f"render: video too long: {60 + i}.5s > 59s"} for i in range(3)]
    assert is_systemic_failure(too_long)


def test_tts_speedup_factor():
    assert tts_speedup_factor(45.0, 7, cfg) is None                 # fits, no change
    f = tts_speedup_factor(60.1, 7, cfg)                            # the case seen in a real demo run
    assert f is not None and 0.80 <= f < 1.0
    assert 60.1 * f + cfg.video.lead_in_seconds + 0.15 * 6 + 0.5 <= cfg.video.max_seconds
    try:
        tts_speedup_factor(90.0, 7, cfg)
        assert False, "should refuse an extreme speed-up"
    except PipelineError:
        pass


def test_tempo_adjust_shortens_audio(tmp_path):
    import numpy as np
    from shorts_factory.tts import TTSResult, tempo_adjust
    from shorts_factory.utils import wav_duration, write_wav
    sr = 48000
    t = np.arange(int(sr * 4.0)) / sr
    wav = tmp_path / "s.wav"
    write_wav(wav, 0.3 * np.sin(2 * np.pi * 220 * t), sr)
    res = tempo_adjust(TTSResult(wav, wav_duration(wav), "test"), 1.08, sr)
    assert abs(res.duration - 4.0 / 1.08) < 0.1, res.duration


def test_upload_queue_scopes_demo_uploads_to_this_run(tmp_path):
    st = State(tmp_path / "state.json")
    mp4 = tmp_path / "v.mp4"
    mp4.write_bytes(b"x")
    old_demo = {"id": "old-demo", "status": "rendered", "demo": True, "path": str(mp4)}
    new_demo = {"id": "new-demo", "status": "rendered", "demo": True, "path": str(mp4)}
    real_old = {"id": "real-old", "status": "rendered", "path": str(mp4)}
    held = {"id": "held", "status": "held", "path": str(mp4)}
    uploaded = {"id": "done", "status": "uploaded", "youtube_id": "abc", "path": str(mp4)}
    for v in (old_demo, new_demo, real_old, held, uploaded):
        st.add_video(v)
    assert [v["id"] for v in upload_queue(st, [new_demo], demo=True)] == ["new-demo"]
    assert [v["id"] for v in upload_queue(st, [], demo=False)] == ["real-old"]


def test_publish_slot_and_metadata(tmp_path):
    st = State(tmp_path / "state.json")
    slot = next_publish_slot(cfg, st)
    assert slot.tzinfo is not None
    s = template_script(_topic(), "neon")
    body = build_metadata(s, cfg, "scheduled", slot)
    assert body["status"]["privacyStatus"] == "private" and body["status"]["publishAt"].endswith("Z")
    assert len(body["snippet"]["title"]) <= 100
    body2 = build_metadata(s, cfg, "private", None)
    assert "publishAt" not in body2["status"]
