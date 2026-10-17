"""Daily orchestration with a hard time budget, retries over candidate topics, persistent state.

A day is Shorts (9:16, under a minute) plus long-form videos (16:9, 8-10 minutes). Shorts go first
because they are cheap and reliable; long-form uses whatever time is left, within its own reserve.
Before anything is produced, the channel's view counts and YouTube's trending tech videos are read
(analytics.py) so topic ranking and the script prompts follow what is working.
"""
from __future__ import annotations

import time
import traceback
from datetime import datetime
from pathlib import Path

from .config import cfg_for_format
from .qc import check_script, check_video
from .render import render_video
from .report import write_report
from .research import (Topic, evergreen_topics, long_candidates, longform_library, prepare_long_topic,
                       select_candidates)
from .scripting import VideoScript, add_short_cta, template_script, write_script
from .state import State
from .characters import pick_cast, voice_for
from .tts import speak_scene, tempo_adjust
from .utils import (OUTPUT_DIR, PipelineError, TimeBudgetExceeded, abs_path, now_local, rel_path, slugify,
                    today_str, write_json)


class Deadline:
    def __init__(self, minutes: float):
        self.start = time.monotonic()
        self.budget = minutes * 60.0

    def elapsed(self) -> float:
        return time.monotonic() - self.start

    def remaining(self) -> float:
        return self.budget - self.elapsed()

    def check(self, label: str = "") -> None:
        if self.remaining() <= 0:
            raise TimeBudgetExceeded(f"time budget exhausted during {label}")


MIN_SPEEDUP_FACTOR = 0.80   # never speed narration up by more than 20 %, it becomes hard to follow


def tts_speedup_factor(narration_seconds: float, n_scenes: int, cfg) -> float | None:
    """Return the duration factor needed to fit the squeezed timeline into max_seconds, or None
    when the narration already fits. Raises PipelineError when the required speed-up is too big."""
    v = cfg.video
    overhead = float(v.lead_in_seconds) + 0.15 * max(n_scenes - 1, 0) + 0.5   # squeezed gaps + tail
    allowed = float(v.max_seconds) - overhead
    if narration_seconds <= allowed:
        return None
    factor = allowed / narration_seconds * 0.98
    if factor < MIN_SPEEDUP_FACTOR:
        raise PipelineError(f"narration {narration_seconds:.1f}s is far too long for a {v.max_seconds}s video")
    return factor


def _with_length_scale(cfg, length_scale: float):
    import copy
    from .config import Cfg
    new = Cfg(copy.deepcopy(dict(cfg)))
    new["tts"]["piper_length_scale"] = round(length_scale, 3)
    return new


def produce_video(topic: Topic, index: int, date: str, cfg, logger, state: State, deadline: Deadline,
                  template_only: bool = False, insights: str = "") -> tuple[dict, VideoScript]:
    """Produce one video. `cfg` is already the format's config (config.cfg_for_format)."""
    long_form = cfg.get("format") == "long"
    # rotate characters and colours by day too, so a 2-video day does not always get the same pairs
    slot = index + datetime.fromisoformat(date).toordinal()
    themes = list(cfg.video.themes)
    theme = themes[slot % len(themes)]
    tag = f"L{index + 1}" if long_form else f"{index + 1:02d}"
    vdir = OUTPUT_DIR / date / f"{tag}-{slugify(topic.title)}"
    vdir.mkdir(parents=True, exist_ok=True)
    seed = int(topic.id[:6], 16)
    stage = "script"
    try:
        logger.info("[%s] topic: %s  (%s)", tag, topic.title, topic.source)
        cast = pick_cast(cfg, slot)
        if long_form:
            from .longform import write_long_script
            script = write_long_script(topic, theme, cfg, logger, cast=cast, insights=insights, seed=seed)
        else:
            script = (template_script(topic, theme, cast) if template_only
                      else write_script(topic, theme, cfg, logger, cast=cast, insights=insights))
            add_short_cta(script, seed)
        cast = script.extra.get("cast") or []
        voices = {role: voice_for(key) for role, key in zip(("host", "guest"), cast)} or None
        write_json(vdir / "script.json", script.to_dict())
        write_json(vdir / "topic.json", topic.to_dict())
        deadline.check(stage)

        stage = "script-qc"
        qc_s = check_script(script, topic, state, cfg)
        if qc_s["errors"]:
            raise PipelineError("script QC failed: " + "; ".join(qc_s["errors"]))
        for w in qc_s["warnings"]:
            logger.warning("script QC warning: %s", w)

        stage = "tts"
        audio = []
        for i, scene in enumerate(script.scenes):
            audio.append(speak_scene(scene, vdir / "scenes" / f"{i + 1:02d}.wav", cfg, logger, voices))
            deadline.check(stage)
        est = sum(a.duration for a in audio)
        logger.info("narration %.1fs via %s", est, audio[0].engine)
        factor = tts_speedup_factor(est, len(audio), cfg)
        if factor is not None:
            # narration runs past the length limit: speak a little faster instead of losing the topic
            logger.info("narration too long for %ss, re-synthesizing at %.0f%% duration", cfg.video.max_seconds, factor * 100)
            fast_cfg = _with_length_scale(cfg, float(cfg.tts.piper_length_scale) * factor)
            audio = []
            for i, scene in enumerate(script.scenes):
                audio.append(speak_scene(scene, vdir / "scenes" / f"{i + 1:02d}.wav", fast_cfg, logger, voices))
                deadline.check(stage)
            est = sum(a.duration for a in audio)
            logger.info("narration now %.1fs", est)
            # Piper shortens speech more than pauses, so the first pass can still be a little long.
            # Close the remaining gap with a pitch-preserving tempo change on the finished audio.
            factor2 = tts_speedup_factor(est, len(audio), cfg)
            if factor2 is not None:
                if factor * factor2 < MIN_SPEEDUP_FACTOR:
                    raise PipelineError(f"narration {est:.1f}s still too long after speeding up")
                tempo = 1.0 / factor2
                audio = [tempo_adjust(a, tempo, int(cfg.tts.sample_rate)) for a in audio]
                est = sum(a.duration for a in audio)
                logger.info("narration tempo x%.3f -> %.1fs", tempo, est)

        stage = "render"
        result = render_video(script, audio, vdir, cfg, logger, deadline=deadline, seed=seed)
        deadline.check(stage)
        thumb = None
        if long_form:
            from .longform import chapter_times
            from .thumbnail import make_thumbnail
            script.extra["chapter_times"] = chapter_times(script, result["scene_starts"])
            write_json(vdir / "script.json", script.to_dict())          # the upload reads chapters from here
            thumb = make_thumbnail(script, cfg, vdir / "thumbnail.jpg", seed)

        stage = "video-qc"
        qc_v = check_video(Path(result["path"]), cfg, result["duration"], result["narration"], result["captions"])
        write_json(vdir / "qc.json", {"script": qc_s, "video": {k: v for k, v in qc_v.items()}})
        if qc_v["errors"]:
            raise PipelineError("video QC failed: " + "; ".join(qc_v["errors"]))
        record = {
            "id": f"{date}-{tag}-{topic.id[:8]}", "date": date, "format": "long" if long_form else "short",
            "title": script.title, "topic_id": topic.id, "topic_title": topic.title, "source": topic.source,
            "sources": script.sources, "theme": theme, "provider": script.provider, "tts_engine": audio[0].engine,
            "duration": round(result["duration"], 2), "path": rel_path(result["path"]), "srt": rel_path(result["srt"]),
            "captions": result["captions"], "caption_method": result["caption_method"],
            "status": "rendered", "qc_warnings": qc_s["warnings"] + qc_v["warnings"],
            "created": datetime.now().isoformat(timespec="seconds"), "render_seconds": round(result["render_seconds"]),
        }
        if thumb:
            record["thumbnail"] = rel_path(thumb)
        return record, script
    except TimeBudgetExceeded:
        raise
    except PipelineError as e:
        raise PipelineError(f"{stage}: {e}") from e
    except Exception as e:  # unexpected -> still recoverable for the run
        logger.debug(traceback.format_exc())
        raise PipelineError(f"{stage}: {type(e).__name__}: {e}") from e


SYSTEMIC_FAILURE_RUN = 3


def _error_signature(err: str) -> str:
    import re
    return re.sub(r"\d+(\.\d+)?", "#", err)[:120]


def is_systemic_failure(failures: list[dict]) -> bool:
    """True when the last few failures (for different topics) share the same error."""
    last = failures[-SYSTEMIC_FAILURE_RUN:]
    if len(last) < SYSTEMIC_FAILURE_RUN:
        return False
    return len({_error_signature(f["error"]) for f in last}) == 1 and len({f["topic"] for f in last}) == len(last)


def upload_queue(state: State, produced: list[dict], demo: bool) -> list[dict]:
    """Videos to upload now. A normal run uploads every pending non-demo video (including ones
    rendered on earlier runs whose upload failed). `demo --upload` is a test and uploads only
    the videos this run rendered, never older demos."""
    if demo:
        produced_ids = {r["id"] for r in produced}
        return [v for v in state.pending_uploads(include_demo=True) if v["id"] in produced_ids]
    return state.pending_uploads()


def _youtube_service(cfg, logger):
    """(service, problem): the API client, or None and why not."""
    from . import youtube as yt
    try:
        creds = yt.load_credentials(logger)
    except PipelineError as e:
        return None, str(e)
    if creds is None:
        return None, "YouTube not authorized: videos were rendered but not uploaded. Run `python -m shorts_factory auth`."
    return yt.build_service(creds), None


def run_daily(cfg, logger, limit: int | None = None, no_upload: bool = False, demo: bool = False,
              dry_run: bool = False, long_limit: int | None = None) -> int:
    tz = cfg.timezone
    date = today_str(tz)
    started = now_local(tz)
    budget = float(cfg.max_runtime_minutes)
    deadline = Deadline(budget)
    state = State()
    lv = cfg.get("long_video") or {}
    targets = {"short": int(limit if limit is not None else cfg.daily_target),
               "long": int(long_limit if long_limit is not None else (0 if demo else lv.get("daily_target", 0)))}
    warnings: list[str] = []
    failures: list[dict] = []
    produced: list[dict] = []
    scripts: dict[str, VideoScript] = {}
    providers, engines = set(), set()
    logger.info("=== %s run for %s (budget %.0f min, target %d Shorts + %d long) ===", cfg.project_name, date,
                budget, targets["short"], targets["long"])

    existing = [v for v in state.videos_for_date(date) if v.get("status") in ("rendered", "uploaded") and not v.get("demo")]
    need = {}
    for fmt in ("short", "long"):
        have = sum(1 for v in existing if v.get("format", "short") == fmt)
        need[fmt] = targets[fmt] if demo else max(0, targets[fmt] - have)
        if have and not demo:
            logger.info("%d %s video(s) already produced today; need %d more", have, fmt, need[fmt])

    # ------------------------------------------------------------------ analytics (read-only, ~5 units)
    service, yt_problem = (None, None)
    if cfg.youtube.enabled and not (no_upload and demo):
        service, yt_problem = _youtube_service(cfg, logger)
    insights = None
    ac = cfg.get("analytics") or {}
    if ac.get("enabled") and not demo:
        from . import analytics
        if service is not None:
            try:
                analytics.refresh_stats(service, state, date, logger)
                analytics.fetch_trends(service, cfg, state, date, logger)
                state.save()
            except Exception as e:  # analytics must never cost the day's videos
                warnings.append(f"analytics skipped: {str(e)[:200]}")
                logger.warning(warnings[-1])
        insights = analytics.build_insights(state, cfg)
    notes = {}
    if insights:
        from .analytics import prompt_notes
        notes = {fmt: prompt_notes(insights, fmt) for fmt in ("short", "long")}

    # ------------------------------------------------------------------ research
    if demo:
        candidates = evergreen_topics(seed=f"demo-{datetime.now().isoformat()}")
        candidates = [c for c in candidates if not state.topic_used(c.id)] or candidates
    else:
        candidates = select_candidates(cfg, state, logger, date, insights) if (need["short"] or need["long"]) else []
    cand_dicts = [{"title": c.title, "source": c.source, "kind": c.kind, "url": c.url} for c in candidates]
    if dry_run:
        for c in candidates:
            logger.info("candidate: %s  [%s]", c.title, c.source)
        for c in long_candidates(cfg, state, candidates, date, insights)[:6]:
            logger.info("long-form candidate: %s  [%s]", c.title, c.source)
        return 0

    # ------------------------------------------------------------------ production
    fails = state.data.setdefault("failed_topics", {})
    upload_reserve = 0 if no_upload else float(cfg.upload_reserve_minutes)

    def produce_batch(fmt: str, topics: list[Topic], count: int, reserve_minutes: float) -> None:
        fcfg = cfg_for_format(cfg, fmt)
        index = sum(1 for v in existing if v.get("format", "short") == fmt)
        done, batch_failures = 0, []
        for topic in topics:
            if done >= count:
                break
            if deadline.remaining() < (reserve_minutes + upload_reserve) * 60:
                warnings.append(f"{fmt}: stopped early, {deadline.remaining() / 60:.1f} min left is below the reserve")
                logger.warning(warnings[-1])
                break
            if (fails.get(topic.id, 0) >= 2 and not demo) or (state.topic_used(topic.id) and not demo):
                continue
            if fmt == "long" and not prepare_long_topic(topic, fcfg, logger):
                continue
            try:
                record, script = produce_video(topic, index, date, fcfg, logger, state, deadline,
                                               insights=notes.get(fmt, ""))
                if demo:
                    record["demo"] = True
                produced.append(record)
                scripts[record["id"]] = script
                providers.update(script.provider.split("+"))
                engines.add(record["tts_engine"])
                state.add_video(record)
                if not demo:
                    state.mark_topic(topic.id, topic.title, topic.url, date)
                state.save()
                index += 1
                done += 1
                logger.info("[ok] %s (%.0fs, %s)", record["title"], record["duration"], record["theme"])
            except TimeBudgetExceeded as e:
                warnings.append(str(e))
                logger.error(str(e))
                break
            except PipelineError as e:
                failure = {"topic": topic.title, "stage": str(e).split(":")[0], "error": str(e)[:300], "format": fmt}
                failures.append(failure)
                batch_failures.append(failure)
                logger.error("[fail] %s -> %s", topic.title, str(e)[:300])
                if is_systemic_failure(batch_failures):
                    # the same error for several different topics is a bug or outage, not a bad topic:
                    # stop instead of burning every candidate, and do not mark these topics as failed
                    for f in batch_failures[-SYSTEMIC_FAILURE_RUN:]:
                        tid = next((c.id for c in topics if c.title == f["topic"]), None)
                        if tid and fails.get(tid):
                            fails[tid] -= 1
                    warnings.append(f"{fmt}: stopped, the same error hit {SYSTEMIC_FAILURE_RUN} topics in a row "
                                    f"({str(e)[:160]}). This is a pipeline problem, not a topic problem.")
                    logger.error(warnings[-1])
                    state.save()
                    break
                fails[topic.id] = fails.get(topic.id, 0) + 1
                state.save()

    if need["short"]:
        produce_batch("short", candidates, need["short"], float(cfg.per_video_reserve_minutes))
    if need["long"]:
        pool = (longform_library(seed=f"demo-{datetime.now().isoformat()}") if demo
                else long_candidates(cfg, state, candidates, date, insights))
        produce_batch("long", pool, need["long"], float(lv.get("reserve_minutes", 24)))

    # ------------------------------------------------------------------ upload
    uploaded = 0
    publish_mode = "disabled"
    if cfg.youtube.enabled and not no_upload:
        from . import youtube as yt
        if service is None:
            warnings.append(yt_problem or "YouTube not authorized")
            logger.warning(warnings[-1])
        else:
            publish_mode = yt.effective_publish_mode(cfg, logger)
            for v in upload_queue(state, produced, demo):
                if deadline.remaining() < 60:
                    warnings.append("time budget reached before all uploads finished")
                    break
                script = scripts.get(v["id"])
                if script is None:
                    sp = abs_path(v["path"]).parent / "script.json"
                    if not sp.exists():
                        continue
                    from .utils import read_json
                    script = VideoScript.from_dict(read_json(sp))
                try:
                    logger.info("uploading %s", v["title"])
                    fcfg = cfg_for_format(cfg, v.get("format", "short"))     # long-form has its own publish slots
                    res = yt.upload_video(service, v, script, fcfg, state, logger, date)
                    if res.get("skipped"):
                        warnings.append(f"upload skipped for {v['title']}: {res['skipped']}")
                    else:
                        state.update_video(v["id"], status="uploaded", **res)
                        uploaded += 1
                        logger.info("  -> %s (%s%s)", res["url"], res["privacy"],
                                    f", publishes {res['publish_at']}" if res.get("publish_at") else "")
                        if res.get("thumbnail_error"):
                            warnings.append(f"custom thumbnail not set for {v['title']}: verify the channel by phone "
                                            "(youtube.com/verify) to enable custom thumbnails")
                except Exception as e:
                    state.update_video(v["id"], upload_error=str(e)[:300])
                    failures.append({"topic": v["title"], "stage": "upload", "error": str(e)[:300]})
                    logger.error("upload failed: %s", str(e)[:300])
                state.save()
    elif no_upload:
        publish_mode = "skipped (--no-upload)"

    # ------------------------------------------------------------------ report
    finished = now_local(tz)
    todays = [v for v in state.videos_for_date(date) if not v.get("demo")]
    run = {"date": date, "started": started.isoformat(timespec="seconds"), "finished": finished.isoformat(timespec="seconds"),
           "elapsed_minutes": deadline.elapsed() / 60, "budget_minutes": budget, "produced": len(produced),
           "produced_long": sum(1 for r in produced if r.get("format") == "long"),
           "target": targets["short"] + targets["long"], "target_long": targets["long"], "uploaded": uploaded,
           "publish_mode": publish_mode, "providers": sorted(providers), "tts_engines": sorted(engines),
           "quota_used": state.quota_used(date), "failures": len(failures), "demo": demo}
    state.add_run(run)
    state.save()
    path = write_report(run, todays if not demo else produced, failures, cand_dicts, warnings, insights)
    logger.info("report: %s", rel_path(path))
    logger.info("=== done: produced %d (%d long), uploaded %d, failures %d, %.1f min ===", len(produced),
                run["produced_long"], uploaded, len(failures), deadline.elapsed() / 60)
    return 0 if (produced or (existing and not demo)) else 2
