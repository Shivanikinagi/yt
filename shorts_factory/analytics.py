"""Daily feedback loop: what is working on this channel and what is popular in tech on YouTube.

Two read-only YouTube Data API calls, about 5 quota units a day:
- videos.list(statistics) for every video this pipeline uploaded -> views/likes/comments in state
- videos.list(chart=mostPopular) in Science & Technology for a few regions -> trending titles

The result steers the day in two ways: topics whose words overlap with the channel's better
performers or with trending titles rank higher, and the script prompts show the model real titles
with real view counts (what worked, what flopped, what is trending) to learn the pattern from.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from .state import State

STOP = set("""a an and are as at be but by can do does for from has have how i if in into is it its just
like make more most new not now of on or our out over so than that the their them then there these
they this to up us was we what when where which who why will with without you your yours vs via
explained shorts short video videos first one two get got gets really actually inside about after
behind could would should every everything still why's here me my vs ok""".split())

LONG_MIN_SECONDS = 240     # mostPopular items at least this long count as long-form


def tokens(title: str) -> list[str]:
    title = re.sub(r"#\w+", " ", title.lower())
    words = re.findall(r"[a-z][a-z0-9+\-']*[a-z0-9+]|[a-z0-9]{2,}", title)
    return [w for w in dict.fromkeys(words) if len(w) >= 2 and w not in STOP and not w.isdigit()]   # keeps "ai"


def iso_seconds(duration: str) -> int:
    m = re.match(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


# ----------------------------------------------------------------------------- API reads
def refresh_stats(service, state: State, date: str, logger) -> int:
    """Store current view/like/comment counts on every uploaded video. 1 unit per 50 videos."""
    by_id = {v["youtube_id"]: v for v in state.data["videos"] if v.get("youtube_id")}
    ids = list(by_id)
    n = 0
    for i in range(0, len(ids), 50):
        state.add_quota(date, 1)
        resp = service.videos().list(part="statistics", id=",".join(ids[i:i + 50])).execute()
        for it in resp.get("items", []):
            s = it.get("statistics", {})
            by_id[it["id"]]["stats"] = {"views": int(s.get("viewCount", 0)), "likes": int(s.get("likeCount", 0)),
                                        "comments": int(s.get("commentCount", 0)), "fetched": date}
            n += 1
    logger.info("analytics: stats for %d uploaded videos", n)
    return n


def fetch_trends(service, cfg, state: State, date: str, logger) -> int:
    """YouTube's most-popular Science & Technology videos per region (1 unit each)."""
    ac = cfg.analytics
    items, seen = [], set()
    for region in ac.trend_regions:
        state.add_quota(date, 1)
        try:
            resp = service.videos().list(part="snippet,statistics,contentDetails", chart="mostPopular",
                                         regionCode=region, videoCategoryId=str(ac.trend_category_id),
                                         maxResults=50).execute()
        except Exception as e:  # a region without the chart must not stop the others
            logger.warning("analytics: trends for %s failed: %s", region, str(e)[:160])
            continue
        for it in resp.get("items", []):
            if it["id"] in seen:
                continue
            seen.add(it["id"])
            items.append({"title": it["snippet"]["title"], "channel": it["snippet"].get("channelTitle", ""),
                          "views": int(it.get("statistics", {}).get("viewCount", 0)),
                          "seconds": iso_seconds(it.get("contentDetails", {}).get("duration", "")),
                          "region": region})
    if items:
        items.sort(key=lambda x: -x["views"])
        state.data["trends"] = {"date": date, "videos": items[:150]}
    logger.info("analytics: %d trending tech videos", len(items))
    return len(items)


# ----------------------------------------------------------------------------- what is working
def _age_days(v: dict, now: datetime) -> float:
    when = v.get("publish_at") or v.get("date")
    try:
        dt = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).total_seconds() / 86400


def performance_profile(state: State, min_age_days: float = 2, now: datetime | None = None) -> dict:
    """Rank this channel's videos and learn which title words go with more views.

    Each word's weight is the average log-views advantage of the videos that use it, shrunk towards
    zero for rare words, so one lucky video does not decide the ranking."""
    now = now or datetime.now(timezone.utc)
    vids = [v for v in state.data["videos"] if v.get("stats") and not v.get("demo")
            and _age_days(v, now) >= min_age_days]
    prof = {"n": len(vids), "top": [], "bottom": [], "weights": {}, "formats": {}}
    if not vids:
        return prof
    ranked = sorted(vids, key=lambda v: -v["stats"]["views"])
    row = lambda v: {"title": v["title"], "views": v["stats"]["views"], "likes": v["stats"]["likes"],  # noqa: E731
                     "comments": v["stats"]["comments"], "format": v.get("format", "short")}
    prof["top"] = [row(v) for v in ranked[:5]]
    prof["bottom"] = [row(v) for v in ranked[-5:]] if len(ranked) >= 10 else []
    for fmt in ("short", "long"):
        views = [v["stats"]["views"] for v in vids if v.get("format", "short") == fmt]
        if views:
            prof["formats"][fmt] = {"videos": len(views), "avg_views": sum(views) / len(views)}
    if len(vids) < 6:
        return prof
    scores = {id(v): math.log1p(v["stats"]["views"]) for v in vids}
    mean = sum(scores.values()) / len(scores)
    acc: dict[str, list[float]] = {}
    for v in vids:
        for tok in tokens(v["title"]):
            acc.setdefault(tok, []).append(scores[id(v)] - mean)
    prof["weights"] = {t: round(sum(d) / (len(d) + 2), 3) for t, d in acc.items() if len(d) >= 2}
    return prof


def trend_keywords(state: State, limit: int = 40) -> dict[str, float]:
    """Words that recur across trending tech titles, weighted by how big those videos are."""
    vids = (state.data.get("trends") or {}).get("videos") or []
    acc: dict[str, list[float]] = {}
    for v in vids:
        for tok in tokens(v["title"]):
            acc.setdefault(tok, []).append(math.log10(max(v["views"], 10)))
    scored = {t: sum(w) for t, w in acc.items() if len(w) >= 2}
    best = sorted(scored.items(), key=lambda kv: -kv[1])[:limit]
    top = best[0][1] if best else 1.0
    return {t: round(s / top, 3) for t, s in best}


def topic_boost(title: str, insights: dict | None) -> float:
    """Ranking multiplier for a candidate topic, between about 0.75 and 1.6."""
    if not insights:
        return 1.0
    toks = tokens(title)
    weights = (insights.get("profile") or {}).get("weights") or {}
    perf = sum(weights.get(t, 0.0) for t in toks)
    trend = sum((insights.get("trends") or {}).get(t, 0.0) for t in toks)
    return math.exp(max(-0.3, min(0.35, perf))) * (1 + 0.12 * min(trend, 1.0))


def build_insights(state: State, cfg) -> dict:
    return {"profile": performance_profile(state, float(cfg.analytics.get("min_age_days", 2))),
            "trends": trend_keywords(state),
            "trending": (state.data.get("trends") or {}).get("videos") or []}


def prompt_notes(insights: dict | None, fmt: str = "short") -> str:
    """A short block for the script prompt: real titles with real numbers to learn the pattern from."""
    if not insights:
        return ""
    prof = insights.get("profile") or {}
    out = []
    if prof.get("top"):
        out.append("Best-performing titles on this channel (views):")
        out += [f"- {r['title']} ({r['views']})" for r in prof["top"][:4]]
    if prof.get("bottom"):
        out.append("Weakest titles on this channel (views):")
        out += [f"- {r['title']} ({r['views']})" for r in prof["bottom"][:3]]
    want_long = fmt == "long"
    trending = [v for v in insights.get("trending") or [] if (v["seconds"] >= LONG_MIN_SECONDS) == want_long]
    if trending:
        out.append(f"Most-viewed {'long-form' if want_long else 'short'} tech videos on YouTube right now:")
        out += [f"- {v['title'][:90]} ({v['views']:,} views)" for v in trending[:5]]
    if not out:
        return ""
    out.append("Learn the pattern, never copy a title: viewers click on things they already know, a concrete "
               "stake for them, and a clear question. Obscure paper or product names in the title do poorly.")
    return "\n\nWHAT VIEWERS RESPOND TO\n" + "\n".join(out)


def report_lines(insights: dict | None) -> list[str]:
    """Markdown for the daily report."""
    if not insights:
        return []
    prof = insights.get("profile") or {}
    lines = ["## What's working", ""]
    if prof.get("top"):
        lines += [f"Judged on {prof['n']} videos at least 2 days old.", "",
                  "| Views | Likes | Comments | Format | Title |", "|---|---|---|---|---|"]
        for r in prof["top"] + prof.get("bottom", []):
            lines.append(f"| {r['views']} | {r['likes']} | {r['comments']} | {r['format']} | {r['title']} |")
        lines.append("")
        for fmt, f in prof.get("formats", {}).items():
            lines.append(f"- {fmt}: {f['videos']} videos, {f['avg_views']:.0f} average views")
        w = prof.get("weights") or {}
        if w:
            good = [t for t, s in sorted(w.items(), key=lambda kv: -kv[1]) if s > 0][:8]
            bad = [t for t, s in sorted(w.items(), key=lambda kv: kv[1]) if s < 0][:8]
            lines.append(f"- Title words with more views: {', '.join(good) or '-'}")
            lines.append(f"- Title words with fewer views: {', '.join(bad) or '-'}")
    else:
        lines.append("No view counts yet (YouTube not connected, or no video is old enough).")
    trending = insights.get("trending") or []
    if trending:
        lines += ["", "Trending in Science & Technology on YouTube:", ""]
        for v in trending[:6]:
            kind = "long" if v["seconds"] >= LONG_MIN_SECONDS else "short"
            lines.append(f"- {v['title'][:100]} — {v['channel']} ({v['views']:,} views, {kind})")
    lines.append("")
    return lines
