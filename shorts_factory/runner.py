"""Daily orchestration with a hard time budget, retries over candidate topics, persistent state."""
from __future__ import annotations

import time
import traceback
from datetime import datetime
from pathlib import Path

from .qc import check_script, check_video
from .render import render_video
from .report import write_report
from .research import Topic, evergreen_topics, select_candidates
from .scripting import VideoScript, template_script, write_script
from .state import State
from .tts import synthesize
from .utils import OUTPUT_DIR, PipelineError, TimeBudgetExceeded, now_local, slugify, today_str, write_json


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


def produce_video(topic: Topic, index: int, date: str, cfg, logger, state: State, deadline: Deadline,
                  template_only: bool = False) -> tuple[dict, VideoScript]:
    themes = list(cfg.video.themes)
    theme = themes[index % len(themes)]
    vdir = OUTPUT_DIR / date / f"{index + 1:02d}-{slugify(topic.title)}"
    vdir.mkdir(parents=True, exist_ok=True)
    stage = "script"
    try:
        logger.info("[%d] topic: %s  (%s)", index + 1, topic.title, topic.source)
        script = template_script(topic, theme) if template_only else write_script(topic, theme, cfg, logger)
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
            audio.append(synthesize(scene["narration"], vdir / "scenes" / f"{i + 1:02d}.wav", cfg, logger))
            deadline.check(stage)
        est = sum(a.duration for a in audio)
        logger.info("narration %.1fs via %s", est, audio[0].engine)

        stage = "render"
        seed = int(topic.id[:6], 16)
        result = render_video(script, audio, vdir, cfg, logger, deadline=deadline, seed=seed)
        deadline.check(stage)

        stage = "video-qc"
        qc_v = check_video(Path(result["path"]), cfg, result["duration"], result["narration"], result["captions"])
        write_json(vdir / "qc.json", {"script": qc_s, "video": {k: v for k, v in qc_v.items()}})
        if qc_v["errors"]:
            raise PipelineError("video QC failed: " + "; ".join(qc_v["errors"]))
        record = {
            "id": f"{date}-{index + 1:02d}-{topic.id[:8]}", "date": date, "title": script.title,
            "topic_id": topic.id, "topic_title": topic.title, "source": topic.source, "sources": script.sources,
            "theme": theme, "provider": script.provider, "tts_engine": audio[0].engine,
            "duration": round(result["duration"], 2), "path": result["path"], "srt": result["srt"],
            "captions": result["captions"], "caption_method": result["caption_method"],
            "status": "rendered", "qc_warnings": qc_s["warnings"] + qc_v["warnings"],
            "created": datetime.now().isoformat(timespec="seconds"), "render_seconds": round(result["render_seconds"]),
        }
        return record, script
    except TimeBudgetExceeded:
        raise
    except PipelineError as e:
        raise PipelineError(f"{stage}: {e}") from e
    except Exception as e:  # unexpected -> still recoverable for the run
        logger.debug(traceback.format_exc())
        raise PipelineError(f"{stage}: {type(e).__name__}: {e}") from e


def run_daily(cfg, logger, limit: int | None = None, no_upload: bool = False, demo: bool = False,
              dry_run: bool = False) -> int:
    tz = cfg.timezone
    date = today_str(tz)
    started = now_local(tz)
    budget = float(cfg.max_runtime_minutes)
    deadline = Deadline(budget)
    state = State()
    target = int(limit if limit is not None else cfg.daily_target)
    warnings: list[str] = []
    failures: list[dict] = []
    produced: list[dict] = []
    scripts: dict[str, VideoScript] = {}
    providers, engines = set(), set()
    logger.info("=== %s run for %s (budget %.0f min, target %d) ===", cfg.project_name, date, budget, target)

    existing = [v for v in state.videos_for_date(date) if v.get("status") in ("rendered", "uploaded") and not v.get("demo")]
    need = max(0, target - len(existing)) if not demo else target
    if existing and not demo:
        logger.info("%d video(s) already produced today; need %d more", len(existing), need)

    # ------------------------------------------------------------------ research
    if demo:
        candidates = evergreen_topics(seed=f"demo-{datetime.now().isoformat()}")
        candidates = [c for c in candidates if not state.topic_used(c.id)] or candidates
    else:
        candidates = select_candidates(cfg, state, logger, date) if need else []
    cand_dicts = [{"title": c.title, "source": c.source, "kind": c.kind, "url": c.url} for c in candidates]
    if dry_run:
        for c in candidates:
            logger.info("candidate: %s  [%s]", c.title, c.source)
        return 0

    # ------------------------------------------------------------------ production
    fails = state.data.setdefault("failed_topics", {})
    index = len(existing)
    reserve = (float(cfg.per_video_reserve_minutes) + (0 if no_upload else float(cfg.upload_reserve_minutes))) * 60
    for topic in candidates:
        if len(produced) >= need:
            break
        if deadline.remaining() < reserve:
            warnings.append(f"stopped early: {deadline.remaining() / 60:.1f} min left is below the reserve")
            logger.warning(warnings[-1])
            break
        if fails.get(topic.id, 0) >= 2 and not demo:
            continue
        try:
            record, script = produce_video(topic, index, date, cfg, logger, state, deadline, template_only=False)
            if demo:
                record["demo"] = True
            produced.append(record)
            scripts[record["id"]] = script
            providers.add(script.provider)
            engines.add(record["tts_engine"])
            state.add_video(record)
            if not demo:
                state.mark_topic(topic.id, topic.title, topic.url, date)
            state.save()
            index += 1
            logger.info("[ok] %s (%.0fs, %s)", record["title"], record["duration"], record["theme"])
        except TimeBudgetExceeded as e:
            warnings.append(str(e))
            logger.error(str(e))
            break
        except PipelineError as e:
            fails[topic.id] = fails.get(topic.id, 0) + 1
            failures.append({"topic": topic.title, "stage": str(e).split(":")[0], "error": str(e)[:300]})
            logger.error("[fail] %s -> %s", topic.title, str(e)[:300])
            state.save()

    # ------------------------------------------------------------------ upload
    uploaded = 0
    publish_mode = "disabled"
    if cfg.youtube.enabled and not no_upload:
        from . import youtube as yt
        try:
            creds = yt.load_credentials(logger)
        except PipelineError as e:
            creds = None
            warnings.append(str(e))
        if creds is None:
            warnings.append("YouTube not authorized: videos were rendered but not uploaded. Run `python -m shorts_factory auth`.")
            logger.warning(warnings[-1])
        else:
            publish_mode = yt.effective_publish_mode(cfg, logger)
            service = yt.build_service(creds)
            for v in state.pending_uploads(include_demo=demo):
                if deadline.remaining() < 60:
                    warnings.append("time budget reached before all uploads finished")
                    break
                script = scripts.get(v["id"])
                if script is None:
                    sp = Path(v["path"]).parent / "script.json"
                    if not sp.exists():
                        continue
                    from .utils import read_json
                    script = VideoScript.from_dict(read_json(sp))
                try:
                    logger.info("uploading %s", v["title"])
                    res = yt.upload_video(service, v, script, cfg, state, logger, date)
                    if res.get("skipped"):
                        warnings.append(f"upload skipped for {v['title']}: {res['skipped']}")
                    else:
                        state.update_video(v["id"], status="uploaded", **res)
                        uploaded += 1
                        logger.info("  -> %s (%s%s)", res["url"], res["privacy"],
                                    f", publishes {res['publish_at']}" if res.get("publish_at") else "")
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
           "target": target, "uploaded": uploaded, "publish_mode": publish_mode, "providers": sorted(providers),
           "tts_engines": sorted(engines), "quota_used": state.quota_used(date), "failures": len(failures), "demo": demo}
    state.add_run(run)
    state.save()
    path = write_report(run, todays if not demo else produced, failures, cand_dicts, warnings)
    logger.info("report: %s", path)
    logger.info("=== done: produced %d, uploaded %d, failures %d, %.1f min ===", len(produced), uploaded,
                len(failures), deadline.elapsed() / 60)
    return 0 if (produced or (existing and not demo)) else 2
