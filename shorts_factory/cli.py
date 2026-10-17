"""Command line: run | demo | research | auth | upload | doctor | report | insights | tweet."""
from __future__ import annotations

import argparse
import os
import platform
import sys
from pathlib import Path

from . import __version__
from .config import load_settings
from .utils import REPORT_DIR, ROOT, ffmpeg_exe, now_local, setup_logging


def cmd_doctor(cfg, logger) -> int:
    from .llm import available_providers
    from .tts import _piper_voice_paths
    from .visuals import Fonts
    from .state import State
    print(f"Tech Shorts Factory v{__version__}  root={ROOT}")
    print(f"python {platform.python_version()} on {platform.system()} {platform.machine()}")
    try:
        import subprocess
        ver = subprocess.run([ffmpeg_exe(), "-version"], capture_output=True, text=True, timeout=20).stdout.splitlines()[0]
        print(f"ffmpeg: {ffmpeg_exe()}\n        {ver}")
    except Exception as e:
        print(f"ffmpeg: MISSING ({e})")
    fonts = Fonts(cfg)
    for k, v in fonts.paths.items():
        print(f"font {k:8s}: {v or 'Pillow default (install a TTF for nicer text)'}")
    print(f"timezone {cfg.timezone}: now {now_local(cfg.timezone).isoformat(timespec='seconds')}")
    print(f"LLM providers configured: {cfg.llm.providers}")
    print(f"LLM providers usable now : {available_providers(cfg)}")
    onnx, _ = _piper_voice_paths(cfg.tts.piper_voice)
    print(f"TTS engines: {cfg.tts.engines}; piper voice {'present' if onnx.exists() else 'will download on first run'}")
    print(f"GROQ_API_KEY: {'set' if os.environ.get('GROQ_API_KEY') else 'not set'}  "
          f"GEMINI_API_KEY: {'set' if os.environ.get('GEMINI_API_KEY') else 'not set'}  "
          f"GITHUB_TOKEN: {'set' if os.environ.get('GITHUB_TOKEN') else 'not set'}")
    from .youtube import CLIENT_FILE, TOKEN_FILE
    has_client = CLIENT_FILE.exists() or bool(os.environ.get("YOUTUBE_CLIENT_SECRET_JSON"))
    has_token = TOKEN_FILE.exists() or bool(os.environ.get("YOUTUBE_TOKEN_JSON"))
    print(f"YouTube: client secret {'present' if has_client else 'MISSING'}, token {'present' if has_token else 'MISSING'}, "
          f"publish_mode={cfg.youtube.publish_mode}, ALLOW_PUBLIC_SCHEDULING={os.environ.get('ALLOW_PUBLIC_SCHEDULING', 'unset')}")
    st = State()
    print(f"state: {len(st.data['videos'])} videos, {len(st.data['topics'])} topics used, {len(st.data['runs'])} runs")
    return 0


def cmd_insights(cfg, logger) -> int:
    from . import analytics
    from .runner import _youtube_service
    from .state import State
    from .utils import today_str
    state, date = State(), today_str(cfg.timezone)
    service, problem = _youtube_service(cfg, logger)
    if service is None:
        print(problem)
    else:
        analytics.refresh_stats(service, state, date, logger)
        analytics.fetch_trends(service, cfg, state, date, logger)
        state.save()
    print("\n".join(analytics.report_lines(analytics.build_insights(state, cfg))))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="shorts_factory", description="Automated YouTube tech Shorts factory")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="full daily run: research -> produce -> upload -> report")
    r.add_argument("--limit", type=int, help="override number of Shorts")
    r.add_argument("--long", type=int, dest="long_limit", help="override number of long-form videos")
    r.add_argument("--no-upload", action="store_true")
    r.add_argument("--dry-run", action="store_true", help="only list candidate topics")
    d = sub.add_parser("demo", help="produce one video from the evergreen library, no upload")
    d.add_argument("--limit", type=int, default=1, help="number of Shorts")
    d.add_argument("--long", type=int, dest="long_limit", default=0, help="number of long-form videos")
    d.add_argument("--upload", action="store_true", help="also upload the demo (private)")
    sub.add_parser("research", help="list today's candidate topics")
    sub.add_parser("auth", help="one-time YouTube OAuth authorization (opens browser)")
    sub.add_parser("upload", help="upload videos that were rendered but not yet uploaded")
    sub.add_parser("doctor", help="check environment, credentials and providers")
    sub.add_parser("report", help="print the latest report")
    sub.add_parser("insights", help="refresh view counts and trends, print what is working (~5 quota units)")
    tw = sub.add_parser("tweet", help="write today's X content (tweet, accounts to follow, draft replies)")
    tw.add_argument("--force", action="store_true", help="regenerate even if today's file exists")
    a = p.parse_args(argv)

    cfg = load_settings()
    logger = setup_logging(a.cmd)
    if a.cmd == "doctor":
        return cmd_doctor(cfg, logger)
    if a.cmd == "report":
        path = REPORT_DIR / "latest.md"
        print(path.read_text(encoding="utf-8") if path.exists() else "no report yet")
        return 0
    if a.cmd == "auth":
        from .youtube import run_auth_flow
        run_auth_flow(logger)
        return 0
    if a.cmd == "tweet":
        from .tweets import run_tweet
        return run_tweet(cfg, logger, force=a.force)
    if a.cmd == "insights":
        return cmd_insights(cfg, logger)
    from .runner import run_daily
    if a.cmd == "research":
        return run_daily(cfg, logger, dry_run=True, no_upload=True)
    if a.cmd == "demo":
        return run_daily(cfg, logger, limit=a.limit, no_upload=not a.upload, demo=True, long_limit=a.long_limit)
    if a.cmd == "upload":
        return run_daily(cfg, logger, limit=0, no_upload=False, long_limit=0)
    return run_daily(cfg, logger, limit=a.limit, no_upload=a.no_upload, dry_run=a.dry_run, long_limit=a.long_limit)


if __name__ == "__main__":
    sys.exit(main())
