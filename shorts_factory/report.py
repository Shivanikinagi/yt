"""Daily markdown + JSON report written to reports/ (committed back by the cloud runner)."""
from __future__ import annotations

from pathlib import Path

from .utils import REPORT_DIR, write_json


def write_report(run: dict, videos: list[dict], failures: list[dict], candidates: list[dict], warnings: list[str],
                 insights: dict | None = None) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    date = run["date"]
    demo = bool(run.get("demo"))
    stem = f"demo-{date}" if demo else date   # a demo must not overwrite the real daily report
    lines = [f"# {'Demo' if demo else 'Daily'} run {date}", ""]
    lines += [f"- Started: {run['started']}  ", f"- Finished: {run['finished']}  ",
              f"- Elapsed: {run['elapsed_minutes']:.1f} min (budget {run['budget_minutes']:.0f} min)  ",
              f"- Produced: {run['produced']} / target {run['target']} "
              f"(long-form {run.get('produced_long', 0)} / {run.get('target_long', 0)})  ",
              f"- Uploaded: {run['uploaded']}  ", f"- Publish mode: {run.get('publish_mode', 'n/a')}  ",
              f"- LLM providers used: {', '.join(sorted(run.get('providers', []))) or 'none'}  ",
              f"- TTS engines used: {', '.join(sorted(run.get('tts_engines', []))) or 'none'}  ",
              f"- YouTube quota units used today: {run.get('quota_used', 0)}  ",
              f"- Recurring cost of this run: ₹0 (free tiers / local compute only)", ""]
    if warnings:
        lines += ["## Warnings", ""] + [f"- {w}" for w in warnings] + [""]
    lines += ["## Videos", "", "| # | Format | Title | Duration | Theme | Status | YouTube | Publish at | QC warnings |",
              "|---|---|---|---|---|---|---|---|---|"]
    for i, v in enumerate(videos, 1):
        yt = f"[{v['youtube_id']}]({v['url']})" if v.get("youtube_id") else "-"
        qcw = "; ".join(v.get("qc_warnings", [])) or "-"
        d = int(v.get("duration", 0))
        lines.append(f"| {i} | {v.get('format', 'short')} | {v['title']} | {d // 60}:{d % 60:02d} | {v.get('theme', '')} | {v.get('status', '')} | "
                     f"{yt} | {v.get('publish_at') or '-'} | {qcw} |")
    if not videos:
        lines.append("| - | | no videos | | | | | | |")
    lines.append("")
    from .analytics import report_lines
    lines += report_lines(insights)
    if failures:
        lines += ["## Failures", ""] + [f"- **{f['topic']}** — {f['stage']}: {f['error']}" for f in failures] + [""]
    lines += ["## Topics considered", ""] + [f"- {c['title']} ({c['source']}, {c['kind']})" for c in candidates] + [""]
    lines += ["## Sources used", ""]
    for v in videos:
        for s in v.get("sources", []):
            lines.append(f"- {v['title']}: {s}")
    lines.append("")
    md = "\n".join(lines)
    path = REPORT_DIR / f"{stem}.md"
    path.write_text(md, encoding="utf-8")
    (REPORT_DIR / "latest.md").write_text(md, encoding="utf-8")
    write_json(REPORT_DIR / f"{stem}.json", {"run": run, "videos": videos, "failures": failures,
                                             "candidates": candidates, "warnings": warnings})
    return path
