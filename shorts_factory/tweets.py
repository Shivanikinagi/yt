"""Daily X (Twitter) content: one tweet, accounts to follow and draft replies. Posted by hand.

Nothing here talks to X. X's automation rules ban automated likes and proactive follows, and its
API is paid, so this only writes tweets/<date>.md for the owner to copy from. Account handles are
never guessed: they come from x.com / twitter.com links on the article pages themselves, or from
the `twitter_username` field of GitHub profiles those articles link to.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

import requests
import yaml

from .config import CONFIG_DIR, load_sources
from .llm import LLMError, chat
from .research import UA, Topic, _score, _similar, excerpt_from_html, fetch_feed
from .utils import ROOT, STATE_DIR, extract_json, read_json, today_str, write_json

TWEETS_DIR = ROOT / "tweets"
TWEET_STATE = STATE_DIR / "tweets.json"
URL_LEN = 23              # X counts every link as 23 characters
MAX_TWEET = 280
MAX_REPLY = 200
FOLLOW_COUNT = 5
SUGGEST_COOLDOWN_DAYS = 30

_HANDLE_RE = re.compile(r"https?://(?:www\.|mobile\.)?(?:twitter|x)\.com/(?:#!/)?@?([A-Za-z0-9_]{1,15})(?=[/?\"'#\s<]|$)")
_GITHUB_RE = re.compile(r"https?://github\.com/([A-Za-z0-9-]{1,39})(?:/[A-Za-z0-9_.-]+)?(?=[/?\"'#\s<]|$)")
_NOT_HANDLES = {"intent", "share", "home", "i", "search", "hashtag", "login", "signup", "explore",
                "settings", "privacy", "tos", "about", "messages", "notifications", "compose", "x",
                "twitter", "download", "en", "help", "account", "logout", "jobs"}
_NOT_GITHUB = {"about", "features", "pricing", "orgs", "topics", "sponsors", "marketplace", "login",
               "settings", "explore", "collections", "trending", "enterprise", "security", "site"}

OWNER = """You write for an independent tech creator on X (Twitter) who covers AI, developer tools, chips
and software news. The owner does not work for any company in the stories: never write "we", "our" or
"us" about a company, product or research team. Return ONLY a JSON object, no prose."""

TWEET_SYSTEM = OWNER + """
Shape: {"tweet": "...", "alternates": ["...", "..."]}
Rules for all three tweets:
- Do NOT include the URL; it is appended automatically. At most 250 characters.
- Line 1: a hook stating the concrete news in plain English, not the headline copied.
- Line 2: why it matters to developers or tech people, one short sentence.
- 0 to 2 relevant hashtags, at most 1 emoji. No @mentions.
- No hype words (game-changer, revolutionary, mind-blowing), no engagement bait (RT if, thoughts?).
- Every fact and number must come from the article text given. Never invent.
- The two alternates must use different angles (e.g. a number-led hook, a question-led hook)."""

REPLY_SYSTEM = OWNER + """
Shape: {"replies": [{"handle": "...", "reply": "..."}]}, one entry per account, same handle spelling.
Each reply is something the owner could post under that account's post about the story listed with
that account, written as an outside reader reacting to their work.
- At most 200 characters. About that account's own story only; do not bring in other stories.
- Specific: add a fact from that story, a sharp question, or a practical takeaway.
- Banned: "Great point", "Thanks for sharing", generic praise, links, hashtags, self-promotion.
- Never invent facts."""


# ------------------------------------------------------------------------------ length
def tweet_length(text: str) -> int:
    """Character count as X measures it: every URL counts as 23."""
    return len(re.sub(r"https?://\S+", "x" * URL_LEN, text))


def compose(text: str, url: str) -> str:
    return f"{text.strip()}\n\n{url}"


def fits(text: str, url: str) -> bool:
    return bool(text.strip()) and "http" not in text and tweet_length(compose(text, url)) <= MAX_TWEET


# ------------------------------------------------------------------------------ handles
def handles_from_html(html: str) -> list[str]:
    out: list[str] = []
    for h in _HANDLE_RE.findall(html):
        if h.lower() in _NOT_HANDLES or h.lower() in (x.lower() for x in out):
            continue
        out.append(h)
    return out


def github_owners_from_html(html: str) -> list[str]:
    out: list[str] = []
    for owner in _GITHUB_RE.findall(html):
        if owner.lower() in _NOT_GITHUB or owner.lower() in (x.lower() for x in out):
            continue
        out.append(owner)
    return out


def github_twitter(owner: str, timeout: int) -> str | None:
    headers = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.get(f"https://api.github.com/users/{owner}", headers=headers, timeout=timeout)
        if r.status_code >= 400:
            return None
        return (r.json().get("twitter_username") or "").lstrip("@") or None
    except Exception:
        return None


# ------------------------------------------------------------------------------ research
def gather_stories(cfg, tweeted: dict, logger, want: int = 8) -> list[Topic]:
    now = datetime.now(timezone.utc)
    fetched: list[Topic] = []
    for src in load_sources():
        fetched += [t for t in fetch_feed(src, cfg.research.request_timeout_seconds, logger) if t.published]
    for days in (1, 2, 4):
        cutoff = now - timedelta(days=days)
        fresh = [t for t in fetched if datetime.fromisoformat(t.published) >= cutoff and t.id not in tweeted]
        if len(fresh) >= 3:
            break
    for t in fresh:
        t.score = _score(t, now)
        if "arxiv" in t.source.lower():
            t.score *= 0.4  # preprints are a hard sell in a tweet; prefer real launches and news
    fresh.sort(key=lambda t: t.score, reverse=True)
    chosen: list[Topic] = []
    for t in fresh:
        if any(_similar(t.title, c.title) for c in chosen):
            continue
        chosen.append(t)
        if len(chosen) >= want:
            break
    return chosen


def read_page(url: str, timeout: int) -> str | None:
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
        if r.status_code >= 400 or "html" not in r.headers.get("content-type", ""):
            return None
        return r.text
    except Exception:
        return None


def find_accounts(stories: list[Topic], pages: dict[str, str], recent: set[str], timeout: int,
                  logger) -> list[dict]:
    """Verified accounts: handles linked from the article pages (builders via GitHub profiles first),
    topped up from config/x_accounts.yaml matched to the day's stories."""
    builders, publishers, seen = [], [], set(recent)
    github_lookups = 0
    for t in stories:
        html = pages.get(t.url)
        if not html:
            continue
        for owner in github_owners_from_html(html)[:3]:
            if github_lookups >= 8:
                break
            github_lookups += 1
            handle = github_twitter(owner, timeout)
            if handle and handle.lower() not in seen:
                seen.add(handle.lower())
                builders.append({"handle": handle, "story": t, "source": f"https://github.com/{owner}"})
        for handle in handles_from_html(html):
            if handle.lower() not in seen:
                seen.add(handle.lower())
                publishers.append({"handle": handle, "story": t, "source": t.url})
    accounts = builders[:FOLLOW_COUNT]
    # Several articles from one site all link the same site handle; spread across stories first.
    used_stories = {a["story"].id for a in accounts}
    for a in publishers:
        if len(accounts) >= FOLLOW_COUNT:
            break
        if a["story"].id not in used_stories:
            accounts.append(a)
            used_stories.add(a["story"].id)
    for a in publishers:
        if len(accounts) >= FOLLOW_COUNT:
            break
        if a not in accounts:
            accounts.append(a)
    article_count = len(accounts)
    for a in curated_accounts(stories, seen, FOLLOW_COUNT - len(accounts), timeout):
        accounts.append(a)
    logger.info("x accounts: %d from articles, %d from x_accounts.yaml", article_count,
                len(accounts) - article_count)
    return accounts


def curated_accounts(stories: list[Topic], seen: set[str], want: int, timeout: int) -> list[dict]:
    if want <= 0:
        return []
    try:
        entries = (yaml.safe_load((CONFIG_DIR / "x_accounts.yaml").read_text(encoding="utf-8")) or {}).get("accounts", [])
    except FileNotFoundError:
        return []
    texts = [(t, f"{t.title} {t.summary}".lower()) for t in stories]

    def match(entry):
        best, best_hits = None, 0
        for i, (t, text) in enumerate(texts):
            hits = sum(1 for tag in entry.get("tags", []) if re.search(rf"\b{re.escape(tag.lower())}\b", text))
            hits *= 2 if i == 0 else 1  # the story being tweeted counts double
            if hits > best_hits:
                best, best_hits = t, hits
        return best, best_hits

    ranked = sorted(((match(e), e) for e in entries if e.get("github")), key=lambda x: -x[0][1])
    out = []
    for (story, _), entry in ranked:
        if len(out) >= want:
            break
        handle = github_twitter(entry["github"], timeout)
        if not handle or handle.lower() in seen:
            continue
        seen.add(handle.lower())
        out.append({"handle": handle, "story": story, "source": f"https://github.com/{entry['github']}",
                    "about": ", ".join(entry.get("tags", []))})
    return out


# ------------------------------------------------------------------------------ writing
def write_tweets(cfg, story: Topic, logger) -> tuple[list[str], str]:
    user = (f"STORY: {story.title}\nSOURCE: {story.source}\n"
            f"ARTICLE TEXT: {story.source_text()[:2500]}")
    for _ in range(2):
        try:
            text, provider = chat(cfg, TWEET_SYSTEM, user, logger)
            data = extract_json(text)
        except (LLMError, ValueError) as e:
            logger.warning("x tweet: %s", e)
            continue
        options = [data.get("tweet", "")] + list(data.get("alternates") or [])
        options = [o.strip() for o in options if isinstance(o, str) and fits(o, story.url)]
        if options:
            return options, provider
        user += "\n\nYour last answer broke the length or URL rule. Keep every tweet under 250 characters, no URL."
    title = story.title if tweet_length(compose(story.title, story.url)) <= MAX_TWEET else story.title[:240] + "…"
    return [title], "template"


def write_replies(cfg, accounts: list[dict], logger) -> dict[str, str]:
    """Draft replies for accounts found on an article page (curated accounts have no story to reply to)."""
    lines = [f"- {a['handle']}: STORY: {a['story'].title} ({a['story'].source}). "
             f"TEXT: {a['story'].source_text()[:600]}" for a in accounts if not a.get("about")]
    if not lines:
        return {}
    try:
        text, _ = chat(cfg, REPLY_SYSTEM, "ACCOUNTS:\n" + "\n".join(lines), logger)
        data = extract_json(text)
    except (LLMError, ValueError) as e:
        logger.warning("x replies: %s", e)
        return {}
    replies = {str(r.get("handle", "")).lstrip("@").lower(): str(r.get("reply", "")).strip()
               for r in data.get("replies") or [] if isinstance(r, dict)}
    return {k: v for k, v in replies.items() if v and len(v) <= MAX_REPLY and "http" not in v}


def render_markdown(date: str, story: Topic, content: dict, accounts: list[dict], provider: str) -> str:
    out = [f"# X content for {date}", "",
           f"Story: [{story.title}]({story.url}) ({story.source}). Written by: {provider}.", "",
           "## Tweet to post", ""]
    for i, text in enumerate(content["tweets"]):
        if i == 1:
            out += ["### Alternatives", ""]
        full = compose(text, story.url)
        out += ["```", full, "```", f"{tweet_length(full)}/{MAX_TWEET} characters", ""]
    out += [f"## Accounts to follow ({len(accounts)})", ""]
    if not accounts:
        out += ["No handles could be verified from today's articles. Follow people you see replying",
                "thoughtfully under today's story instead.", ""]
    for i, a in enumerate(accounts, 1):
        reply = content["replies"].get(a["handle"].lower())
        out.append(f"{i}. **@{a['handle']}** - https://x.com/{a['handle']}")
        if a.get("about"):
            out.append(f"   - Posts about: {a['about']}")
        if a["story"] is not None:
            out.append(f"   - Why today: [{a['story'].title}]({a['story'].url})")
        out.append(f"   - Handle verified on: {a['source']}")
        if reply:
            out += [f"   - Draft reply for their post on this story:", "", "     ```", f"     {reply}", "     ```"]
        out.append("")
    out += ["## Your 5 minutes", "",
            "1. Post the tweet above (or an alternative).",
            "2. Follow the accounts and reply to their latest post on the story; like posts you actually find useful.",
            "3. Paste comments on your tweets into `/x-engage` in Claude Code for reply drafts.", ""]
    return "\n".join(out)


def run_tweet(cfg, logger, force: bool = False) -> int:
    date = today_str(cfg.timezone)
    path = TWEETS_DIR / f"{date}.md"
    if path.exists() and not force:
        print(path.read_text(encoding="utf-8"))
        return 0
    state = read_json(TWEET_STATE, default=None) or {}
    state.setdefault("tweeted", {})
    state.setdefault("suggested", {})
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SUGGEST_COOLDOWN_DAYS)).strftime("%Y-%m-%d")
    recent = {h for h, d in state["suggested"].items() if d >= cutoff}
    timeout = cfg.research.request_timeout_seconds
    stories = gather_stories(cfg, state["tweeted"], logger)
    pages: dict[str, str] = {}
    story = None
    for t in stories:
        html = read_page(t.url, timeout)
        if html is None:
            continue
        pages[t.url] = html
        if len(t.summary) < cfg.research.min_summary_chars:
            t.excerpt = excerpt_from_html(html)
        story = story or t
    if story is None:
        logger.error("x content: no reachable fresh story today")
        return 1
    accounts = find_accounts([story] + [t for t in stories if t is not story], pages, recent, timeout, logger)
    tweets, provider = write_tweets(cfg, story, logger)
    content = {"tweets": tweets, "replies": write_replies(cfg, accounts, logger)}
    TWEETS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(date, story, content, accounts, provider), encoding="utf-8")
    state["tweeted"][story.id] = {"title": story.title, "url": story.url, "date": date}
    for a in accounts:
        state["suggested"][a["handle"].lower()] = date
    write_json(TWEET_STATE, state)
    print(path.read_text(encoding="utf-8"))
    return 0
