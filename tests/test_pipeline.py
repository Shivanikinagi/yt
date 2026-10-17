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


def test_paths_are_stored_relative_and_legacy_paths_resolve():
    from shorts_factory.utils import ROOT, abs_path, rel_path
    p = ROOT / "output" / "2026-10-12" / "01-x" / "video.mp4"
    assert rel_path(p) == "output/2026-10-12/01-x/video.mp4"
    assert abs_path("output/2026-10-12/01-x/video.mp4") == p
    legacy_ci = "/home/runner/work/yt/yt/output/2026-10-12/01-x/video.mp4"
    assert abs_path(legacy_ci) == p


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


def test_cue_times_follow_narration():
    from shorts_factory.render import cue_times
    timed = [("Its", 0.0, 0.2), ("variable", 0.2, 0.6), ("length", 0.6, 0.9), ("saves", 0.9, 1.2),
             ("space", 1.2, 1.5), ("and", 1.5, 1.6), ("ASCII", 1.6, 2.0), ("stays", 2.0, 2.3)]
    assert cue_times(["Variable-length encoding", "ASCII compatible"], timed, 0.5) == [0.7, 2.1]
    assert cue_times(["Something unrelated"], timed, 0.0) is None


def test_dialogue_speaker_turns_and_lines():
    from shorts_factory.scripting import _lines
    from shorts_factory.visuals import speaker_at
    turns = [("guest", 0.5, 1.5), ("host", 1.72, 4.0)]
    assert speaker_at(turns, 0.0) == "guest"
    assert speaker_at(turns, 1.55) == "guest"      # the pause belongs to whoever just spoke
    assert speaker_at(turns, 2.0) == "host"
    assert speaker_at(turns, 9.0) == "host"
    assert speaker_at(None, 1.0) is None
    lines = _lines([{"who": "Sora", "text": "Wait,  really?"}, {"who": "host", "text": "Yes."}, {"text": ""}],
                   ["yuki", "sora"])
    assert lines == [{"who": "guest", "text": "Wait, really?"}, {"who": "host", "text": "Yes."}]


def test_cast_pairs_rotate_and_characters_render():
    from shorts_factory.characters import CAST, Host, pick_cast
    assert pick_cast(cfg, 0) != pick_cast(cfg, 1)
    for key in CAST:
        img = Host(key, (56, 189, 248), scale=1.12).sprite(0.3, "point", 2)
        assert img.mode == "RGBA" and img.getbbox() is not None


def test_tweet_length_counts_urls_as_23():
    from shorts_factory.tweets import compose, fits, tweet_length
    url = "https://example.com/" + "a" * 200
    assert tweet_length(compose("hello", url)) == len("hello\n\n") + 23
    assert fits("x" * 250, url)
    assert not fits("x" * 260, url)
    assert not fits("see https://other.example", url)   # the URL is appended by the code, never by the model


def test_handles_from_html_skips_share_links_and_dupes():
    from shorts_factory.tweets import github_owners_from_html, handles_from_html
    html = ('<a href="https://twitter.com/intent/tweet?text=x">share</a>'
            '<a href="https://x.com/simonw">Simon</a><a href="https://twitter.com/SimonW/status/1">post</a>'
            '<a href="https://x.com/home">home</a><a href="https://github.com/ggerganov/llama.cpp">repo</a>'
            '<a href="https://github.com/features">features</a>')
    assert handles_from_html(html) == ["simonw"]
    assert github_owners_from_html(html) == ["ggerganov"]


# ----------------------------------------------------------------------------- long-form, CTAs, analytics
def test_short_cta_adds_question_and_signoff_without_banned_phrases(tmp_path):
    from shorts_factory.scripting import add_short_cta
    s = template_script(_topic(), "neon", ["hana", "ren"])
    n_lines = len(s.scenes[-1]["lines"])
    add_short_cta(s, seed=5)
    last = s.scenes[-1]
    assert last["cta"] == "end" and last["cta_from"] == n_lines
    assert [x["who"] for x in last["lines"][n_lines:]] == ["guest", "host"]
    assert "comments" in last["lines"][n_lines]["text"].lower() and "subscribe" in last["narration"].lower()
    add_short_cta(s, seed=5)                                     # idempotent
    assert len(last["lines"]) == n_lines + 2
    qc = check_script(s, _topic(), State(tmp_path / "s.json"), cfg)
    assert not any("banned" in e for e in qc["errors"])


def _fake_long_chat(cfg, system, user, logger):
    """Stands in for the LLM: an outline, then chapters of the requested size."""
    import json
    import re
    if "Plan a" in user:
        return json.dumps({"title": "How hackers actually crack your passwords", "thumbnail_text": "Your password is weak",
                           "hook": "h", "subject": "Passwords", "description": "d", "hashtags": ["#Security"],
                           "comment_question": "Do you reuse passwords?", "share_with": "a friend who reuses passwords",
                           "chapters": [{"title": f"Part {i}", "question": f"What about part {i}?", "points": ["p"]}
                                        for i in range(1, 6)]}), "fake"
    k = int(re.search(r"Write chapter (\d+) of", user).group(1))
    lo = int(re.search(r"with (\d+)-(\d+) spoken words", user).group(1))
    line = " ".join(["word"] * 20) + "."
    scenes, words = [], 0
    while words < lo + 20:
        scenes.append({"type": "explain", "on_screen": f"Point {len(scenes)}", "bullets": ["a b"],
                       "lines": [{"who": "guest", "text": "So how does that work then?"},
                                 {"who": "host", "text": line + " " + line}]})
        words += 46
    if k == 1:
        scenes[0]["type"] = "title"
    return json.dumps({"scenes": scenes}), "fake"


def test_long_script_has_chapters_ctas_and_outro(monkeypatch):
    import logging
    from shorts_factory import longform
    from shorts_factory.config import cfg_for_format
    from shorts_factory.research import Topic
    monkeypatch.setattr(longform, "chat", _fake_long_chat)
    lcfg = cfg_for_format(cfg, "long")
    topic = Topic(id="ab12cd34ef", title="Password cracking", summary="angle", url="https://en.wikipedia.org/wiki/x",
                  source="Wikipedia", kind="longform", excerpt="text " * 800, verified=True)
    s = longform.write_long_script(topic, "neon", lcfg, logging.getLogger("t"), ["nova", "kai"], seed=1)
    types = [x["type"] for x in s.scenes]
    assert types[0] == "title" and types[-1] == "outro" and types.count("chapter") == 4
    assert s.scenes[-2]["type"] == "payoff"
    likes = [x for x in s.scenes if x.get("cta") == "like"]
    assert len(likes) == 1 and likes[0]["chapter_index"] == 3 and likes[0]["lines"][0]["who"] == "host"
    assert "subscribe" in s.scenes[-1]["narration"].lower() and "reuses passwords" in s.scenes[-1]["narration"]
    assert all(x.get("chapter_index") for x in s.scenes) and s.extra["format"] == "long"
    s.extra["chapter_times"] = longform.chapter_times(s, [i * 20.0 for i in range(len(s.scenes))])
    assert s.extra["chapter_times"][0]["start"] == 0.0 and len(s.extra["chapter_times"]) == 5
    body = build_metadata(s, lcfg, "private", None)
    assert "#Shorts" not in body["snippet"]["title"] and "\n0:00 Part 1\n" in body["snippet"]["description"]
    assert "sub_confirmation=1" in body["snippet"]["description"]


def test_format_config_and_long_publish_slots(tmp_path):
    from zoneinfo import ZoneInfo
    from shorts_factory.config import cfg_for_format
    lcfg = cfg_for_format(cfg, "long")
    assert (lcfg.video.width, lcfg.video.height) == (1920, 1080) and lcfg.video.max_seconds > 300
    assert (cfg.video.width, cfg.video.height) == (1080, 1920)            # base config untouched
    slot = next_publish_slot(lcfg, State(tmp_path / "s.json"))
    assert slot.astimezone(ZoneInfo(cfg.timezone)).strftime("%H:%M") in cfg.long_video.schedule_slots_local


def test_landscape_renderer_every_shot():
    from shorts_factory.config import cfg_for_format
    lcfg = cfg_for_format(cfg, "long")
    scenes = [{"type": t, "on_screen": "A headline for this scene", "bullets": ["one", "two"], "nodes": ["a", "b", "c"],
               "chapter": "Intro", "chapter_index": 1} for t in ("title", "explain", "diagram", "fact", "payoff")]
    scenes += [{"type": "chapter", "on_screen": "Part two", "chapter_index": 2, "cta": "like", "cta_from": 0,
                "_turns": [("host", 0.1, 3.0), ("guest", 3.1, 5.0)]},
               {"type": "outro", "on_screen": "See you", "chapter_index": 2, "cta": "end", "cta_from": 0,
                "_turns": [("host", 0.1, 3.0)]}]
    r = Renderer(lcfg, "sunset", seed=2, scenes=scenes, meta={"cast": ["nova", "kai"], "chapters": ["Intro", "Part two"]})
    cap = {"words": [("Hello", 0.0, 0.5), ("world", 0.5, 1.0)], "start": 0.0, "end": 1.0}
    for s in scenes:
        for t in (0.5, 2.5):
            assert r.frame(s, t, 6.0, 10 + t, 0.5, "Intro", cap).size == (1920, 1080)


def test_thumbnail(tmp_path):
    from PIL import Image
    from shorts_factory.thumbnail import make_thumbnail
    s = template_script(_topic(), "neon", ["nova", "kai"])
    s.extra["thumbnail_text"] = "Faster than you think"
    p = make_thumbnail(s, cfg, tmp_path / "t.jpg", seed=1)
    assert Image.open(p).size == (1280, 720) and p.stat().st_size < 2_000_000


def test_analytics_profile_boost_and_notes():
    from shorts_factory import analytics
    st = State.__new__(State)
    st.data = {"videos": [], "trends": {"videos": [
        {"title": "Which AI is smartest?", "views": 3_000_000, "seconds": 58, "channel": "c", "region": "US"},
        {"title": "AI cloning is worse than you think", "views": 500_000, "seconds": 1200, "channel": "c", "region": "GB"},
        {"title": "New AI phone tested", "views": 900_000, "seconds": 600, "channel": "c", "region": "IN"}]}}
    rows = [("Can AI tests beat human benchmarks?", 158), ("Why AI worms target fridges", 55),
            ("Can AI design experiments?", 19), ("Probe-and-Resume for agent execution", 0),
            ("EliteTable rewrites game strategies", 2), ("Magnetar proves programs ran", 0),
            ("How GPS satellites correct time", 12)]
    for i, (title, views) in enumerate(rows):
        st.data["videos"].append({"id": str(i), "title": title, "date": "2026-10-01",
                                  "stats": {"views": views, "likes": 0, "comments": 0}})
    prof = analytics.performance_profile(st)
    assert prof["top"][0]["views"] == 158 and prof["weights"]["ai"] > 0
    ins = {"profile": prof, "trends": analytics.trend_keywords(st), "trending": st.data["trends"]["videos"]}
    assert analytics.topic_boost("New AI model explained", ins) > analytics.topic_boost("Quarterly earnings call", ins)
    notes_long = analytics.prompt_notes(ins, "long")
    assert "Can AI tests beat human benchmarks? (158)" in notes_long and "AI cloning" in notes_long
    assert "smartest" not in notes_long and "smartest" in analytics.prompt_notes(ins, "short")
    assert analytics.iso_seconds("PT1H2M3S") == 3723 and analytics.tokens("Why AI #Shorts") == ["ai"]
    assert any("What's working" in line for line in analytics.report_lines(ins))


def test_moving_average_matches_convolution():
    import numpy as np
    from shorts_factory.audio import moving_average
    x = np.random.default_rng(1).random(5000).astype(np.float32)
    for w in (1, 2, 7, 64):
        ref = np.convolve(x, np.ones(w, np.float32) / w, mode="same")
        assert np.abs(ref - moving_average(x, w)).max() < 1e-5
