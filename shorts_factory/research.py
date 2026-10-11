"""Daily research: pull RSS/Atom feeds, verify dates and URLs, rank, dedupe, pad with evergreen."""
from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone

import feedparser
import requests

from .config import load_evergreen, load_sources
from .state import State
from .utils import sha1, strip_html

UA = "TechShortsFactory/0.1 (+research bot; respects robots; contact: see repo)"


@dataclass
class Topic:
    id: str
    title: str
    summary: str
    url: str
    source: str
    kind: str                      # news | research | concepts | evergreen
    category: str = "Technology"
    published: str | None = None   # ISO date
    verified: bool = False
    excerpt: str = ""              # extra text fetched from the article page
    score: float = 0.0
    extra: dict = field(default_factory=dict)

    def source_text(self) -> str:
        return " ".join(x for x in [self.title, self.summary, self.excerpt] if x)

    def to_dict(self) -> dict:
        return asdict(self)


def _entry_date(entry, feed) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        val = entry.get(key)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    for key in ("updated_parsed", "published_parsed"):
        val = feed.feed.get(key) if hasattr(feed, "feed") else None
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def _clean_summary(entry) -> str:
    raw = ""
    if entry.get("content"):
        raw = entry["content"][0].get("value", "")
    raw = raw or entry.get("summary", "") or entry.get("description", "")
    text = strip_html(raw)
    text = re.sub(r"^(Article URL:|Comments URL:|Points:|# Comments:).*$", "", text, flags=re.M)
    text = re.sub(r"arXiv:\S+\s*Announce Type:\s*\w+\s*Abstract:\s*", "", text)
    return text.strip()


def fetch_feed(source: dict, timeout: int, logger) -> list[Topic]:
    topics: list[Topic] = []
    try:
        resp = requests.get(source["url"], headers={"User-Agent": UA}, timeout=timeout)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
    except Exception as e:  # feeds are best effort
        logger.warning("feed failed %s: %s", source["name"], e)
        return topics
    for entry in feed.entries[:60]:
        title = strip_html(entry.get("title", "")).strip()
        link = (entry.get("link") or "").strip()
        if not title or not link.startswith("http"):
            continue
        published = _entry_date(entry, feed)
        topics.append(Topic(
            id=sha1(link.lower().split("?")[0].rstrip("/")),
            title=title,
            summary=_clean_summary(entry),
            url=link,
            source=source["name"],
            kind=source.get("kind", "news"),
            category={"research": "AI research", "concepts": "Computer science"}.get(
                source.get("kind", "news"), "Tech news"),
            published=published.isoformat() if published else None,
            extra={"weight": float(source.get("weight", 1.0))},
        ))
    return topics


def verify_url(url: str, timeout: int) -> bool:
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout, stream=True, allow_redirects=True)
        ok = r.status_code < 400
        r.close()
        return ok
    except Exception:
        return False


def fetch_excerpt(url: str, timeout: int, max_chars: int = 1800) -> str:
    """Pull paragraph text from an article page to give the writer verifiable facts."""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
        if r.status_code >= 400 or "html" not in r.headers.get("content-type", ""):
            return ""
        paras = re.findall(r"<p[^>]*>(.*?)</p>", r.text, flags=re.S | re.I)
        out = []
        total = 0
        for p in paras:
            t = strip_html(p)
            if len(t) < 60:
                continue
            out.append(t)
            total += len(t)
            if total >= max_chars:
                break
        return " ".join(out)[:max_chars]
    except Exception:
        return ""


def _score(t: Topic, now: datetime) -> float:
    score = t.extra.get("weight", 1.0)
    if t.published:
        age_days = max(0.0, (now - datetime.fromisoformat(t.published)).total_seconds() / 86400)
        score *= max(0.3, 1.4 - 0.15 * age_days)
    richness = min(len(t.summary), 1200) / 1200
    score *= 0.6 + 0.8 * richness
    hooks = ("first", "new", "breakthrough", "faster", "open-source", "open source", "record",
             "surprising", "secret", "why", "how", "beats", "outperforms", "released", "launch")
    tl = t.title.lower()
    score *= 1.0 + 0.08 * sum(1 for h in hooks if h in tl)
    if re.search(r"\b(lawsuit|earnings|stock|shares|layoffs|hiring)\b", tl):
        score *= 0.6
    # newsletters, digests and roundups mix several stories and make poor single-idea Shorts
    if re.search(r"\b(the download|newsletter|digest|roundup|round-up|weekly|recap|podcast|episode|links)\b", tl):
        score *= 0.2
    return score


def evergreen_topics(seed: str) -> list[Topic]:
    items = []
    for e in load_evergreen():
        items.append(Topic(
            id=sha1("evergreen:" + e["title"].lower()),
            title=e["title"], summary=" ".join(e["summary"].split()), url=e["url"],
            source="Evergreen library", kind="evergreen", category=e.get("category", "Technology"),
            published=None, verified=True))
    rnd = random.Random(seed)
    rnd.shuffle(items)
    return items


def select_candidates(cfg, state: State, logger, date: str) -> list[Topic]:
    rc = cfg.research
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=rc.news_max_age_days)
    all_topics: list[Topic] = []
    for src in load_sources():
        fetched = fetch_feed(src, rc.request_timeout_seconds, logger)
        kept = 0
        for t in fetched:
            if t.published is None:
                continue  # cannot verify freshness -> skip news items without a date
            if datetime.fromisoformat(t.published) < cutoff:
                continue
            all_topics.append(t)  # thin summaries are enriched from the article page later
            kept += 1
        logger.info("feed %-38s fetched=%-3d fresh=%d", src["name"], len(fetched), kept)
        time.sleep(0.2)

    for t in all_topics:
        t.score = _score(t, now)
    all_topics.sort(key=lambda t: t.score, reverse=True)

    chosen: list[Topic] = []
    per_source: dict[str, int] = {}
    seen_titles: list[str] = []
    for t in all_topics:
        if len(chosen) >= rc.candidates:
            break
        if state.topic_used(t.id):
            continue
        if state.similar_title(t.title, rc.dedupe_days, rc.title_similarity_threshold):
            continue
        if any(_similar(t.title, s) for s in seen_titles):
            continue
        if per_source.get(t.source, 0) >= rc.max_per_source:
            continue
        if rc.verify_urls and not verify_url(t.url, rc.request_timeout_seconds):
            logger.info("url not reachable, skipping: %s", t.url)
            continue
        t.verified = True
        if rc.enrich_articles and len(t.summary) < rc.min_summary_chars * 2:
            t.excerpt = fetch_excerpt(t.url, rc.request_timeout_seconds)
        if len(t.source_text()) < rc.min_summary_chars:
            logger.info("too little source text, skipping: %s", t.title)
            continue
        chosen.append(t)
        per_source[t.source] = per_source.get(t.source, 0) + 1
        seen_titles.append(t.title)

    logger.info("fresh verified candidates: %d", len(chosen))
    # pad with evergreen topics so the day never runs dry
    for t in evergreen_topics(seed=date):
        if len(chosen) >= rc.candidates:
            break
        if state.topic_used(t.id):
            continue
        chosen.append(t)
    return chosen


def _similar(a: str, b: str) -> bool:
    import difflib
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() > 0.75
