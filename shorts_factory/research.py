"""Daily research: pull RSS/Atom feeds, verify dates and URLs, rank, dedupe, pad with evergreen."""
from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone

import feedparser
import requests

from .config import load_evergreen, load_longform, load_sources
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
        return excerpt_from_html(r.text, max_chars)
    except Exception:
        return ""


def excerpt_from_html(html: str, max_chars: int = 1800) -> str:
    paras = re.findall(r"<p[^>]*>(.*?)</p>", html, flags=re.S | re.I)
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


# What a general audience already cares about. A story about these gets watched; the same idea framed
# as a paper or an internal product name gets swiped. The script writer reframes the title, so this only
# picks the story, and jargon is a mild penalty rather than a filter (some arXiv topics do very well).
APPEAL = re.compile(
    r"\b(hack\w*|breach\w*|leak\w*|scam\w*|spy\w*|surveillance|password\w*|privacy|stol\w+|steal\w*|bans?|banned|"
    r"crash\w*|outage|broke|bug|virus|malware|ransomware|wallet|crypto|bitcoin|money|price\w*|free|jobs?|"
    r"iphone|android|apple|google|samsung|whatsapp|instagram|youtube|chatgpt|openai|gemini|claude|"
    r"musk|tesla|nvidia|microsoft|meta|amazon|phones?|laptops?|battery|wi-?fi|5g|games?|gaming|"
    r"robots?|space|moon|mars|nasa|isro|india\w*|upi|brain|implant|sight|cancer|health|cars?|self-driving)\b",
    re.I)
JARGON = re.compile(r"\b(position paper|empirical study|survey|benchmark\w*|framework|towards|via|quantization|"
                    r"metrics?|ablation|optimi[sz]ation|stochastic|theorem|heuristic\w*|paradigm)\b", re.I)


def appeal(title: str) -> float:
    """Multiplier for how broadly interesting a story is, judged from its title."""
    hits = len({m.lower() for m in APPEAL.findall(title)})
    return (1.0 + 0.15 * min(3, hits)) * (0.8 if JARGON.search(title) else 1.0)


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
    score *= appeal(t.title)
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
    items.sort(key=lambda t: -appeal(t.title))      # stable: ties keep the daily shuffle
    return items


def select_candidates(cfg, state: State, logger, date: str, insights: dict | None = None) -> list[Topic]:
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

    from .analytics import topic_boost
    for t in all_topics:
        t.score = _score(t, now) * topic_boost(t.title, insights)
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
    # Alternate news and evergreen, so a day's Shorts are one story from the news and one "how it works"
    # topic that keeps being found through search for months. Evergreen also pads a thin news day.
    evergreen = [t for t in evergreen_topics(seed=date) if not state.topic_used(t.id)]
    mixed: list[Topic] = []
    for i in range(max(len(chosen), len(evergreen))):
        mixed += [pool[i] for pool in (chosen, evergreen) if i < len(pool)]
    return mixed[:max(rc.candidates, len(chosen))]


def _similar(a: str, b: str) -> bool:
    import difflib
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() > 0.75


# ----------------------------------------------------------------------------- long-form topics
WIKI_API = "https://en.wikipedia.org/w/api.php"
_WIKI_SKIP = re.compile(r"^==+\s*(See also|References|External links|Further reading|Notes|Bibliography|"
                        r"Sources|Citations|Footnotes)\s*==+", re.M | re.I)


def wiki_extract(page: str, timeout: int, max_chars: int = 6000) -> tuple[str, str]:
    """Plain text of a Wikipedia article (without reference sections) and its canonical URL."""
    try:
        r = requests.get(WIKI_API, params={"action": "query", "prop": "extracts|info", "explaintext": 1,
                                           "inprop": "url", "titles": page, "redirects": 1, "format": "json"},
                         headers={"User-Agent": UA}, timeout=timeout)
        r.raise_for_status()
        info = next(iter(r.json()["query"]["pages"].values()))
    except Exception:
        return "", ""
    text = info.get("extract") or ""
    m = _WIKI_SKIP.search(text)
    if m:
        text = text[:m.start()]
    text = re.sub(r"\n+==+\s*(.*?)\s*==+\n*", r" \1: ", text)   # keep headings as short labels
    text = " ".join(text.split())
    return text[:max_chars], info.get("fullurl", "")


def longform_library(seed: str) -> list[Topic]:
    items = []
    for e in load_longform():
        pages = list(e.get("pages") or [])
        items.append(Topic(
            id=sha1("long:" + e["title"].lower()), title=e["title"], summary=" ".join(e.get("angle", "").split()),
            url="https://en.wikipedia.org/wiki/" + pages[0].replace(" ", "_") if pages else "",
            source="Wikipedia", kind="longform", category=e.get("category", "Technology"),
            extra={"pages": pages}))
    random.Random(seed).shuffle(items)
    return items


def long_candidates(cfg, state: State, news: list[Topic], date: str, insights: dict | None = None) -> list[Topic]:
    """Topics for today's long-form videos: evergreen deep dives first (they keep being found through
    search for months, which is where watch hours come from), alternating with the strongest news
    stories. The evergreen ones are ranked by what is working."""
    from .analytics import topic_boost
    # paper abstracts are too thin for 10 minutes; they stay Shorts
    fresh = [t for t in news if t.kind in ("news", "concepts") and not state.topic_used(t.id)]
    library = [t for t in longform_library(seed=date) if not state.topic_used(t.id)
               and not state.similar_title(t.title, int(cfg.research.dedupe_days), 0.7)]
    library.sort(key=lambda t: -topic_boost(t.title, insights))       # stable: ties keep the daily shuffle
    out: list[Topic] = []
    for i in range(max(len(fresh), len(library))):
        for pool in (library, fresh):
            if i < len(pool):
                out.append(pool[i])
    return out


def prepare_long_topic(topic: Topic, cfg, logger) -> bool:
    """Gather enough source text for an 8-10 minute video. False when there is not enough."""
    rc, need = cfg.research, int(cfg.long_video.min_source_chars)
    if topic.kind == "longform":
        parts, urls = [], []
        for page in topic.extra.get("pages", []):
            text, url = wiki_extract(page, int(rc.request_timeout_seconds), max_chars=6500)
            if text:
                parts.append(f"[{page}] {text}")
                urls.append(url)
        topic.excerpt = " ".join(parts)[:9000]
        if urls:
            topic.url = urls[0]
            topic.extra["sources"] = urls
        topic.verified = bool(urls)
    else:
        full = fetch_excerpt(topic.url, int(rc.request_timeout_seconds), max_chars=9000)
        if len(full) > len(topic.excerpt):
            topic.excerpt = full
    ok = topic.verified and len(topic.source_text()) >= need
    logger.info("long-form source for %r: %d chars%s", topic.title, len(topic.source_text()),
                "" if ok else f" (needs {need}, skipping)")
    return ok
