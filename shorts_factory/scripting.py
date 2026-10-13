"""Script writing: prompt an LLM for a structured Short, validate hard, fall back to a template."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict

from .characters import CAST
from .llm import LLMError, chat
from .research import Topic
from .utils import extract_json, word_count

SCENE_TYPES = {"title", "explain", "diagram", "compare", "code", "fact", "payoff"}
MIN_SCENES, MAX_SCENES = 5, 8
MIN_WORDS, MAX_WORDS = 95, 165          # ~35-60 s at 165 wpm
DIALOGUE_MIN_WORDS = 128                # LLM dialogues shorter than this come out under ~48 s
BANNED = ("as an ai", "i cannot", "i can't", "as a language model", "as an ai language", "subscribe now", "smash that",
          "http://", "https://", "www.", "like and subscribe")

# Every theme shares one HUD-style layout now, so the prompt asks for the same visual register.
STYLE = "fast-paced, visual explainer"


class ScriptError(Exception):
    pass


@dataclass
class VideoScript:
    title: str
    hook: str
    scenes: list[dict]
    description: str
    hashtags: list[str]
    sources: list[str]
    topic_id: str
    topic_title: str
    provider: str
    theme: str
    category: str = "Technology"
    extra: dict = field(default_factory=dict)

    def narration(self) -> str:
        return " ".join(s["narration"].strip() for s in self.scenes)

    def total_words(self) -> int:
        return word_count(self.narration())

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "VideoScript":
        return VideoScript(**{k: d[k] for k in VideoScript.__dataclass_fields__ if k in d})


SYSTEM_PROMPT = """You write scripts for original, educational YouTube Shorts about technology.
You MUST use only facts present in the SOURCE MATERIAL. Never invent numbers, names, dates or quotes.
If the source lacks a detail, explain the idea conceptually instead of guessing.
Write in plain spoken English, short sentences, present tense, friendly expert tone. No emojis.
Never copy sentences verbatim from the source; paraphrase in your own words.
Output ONLY one JSON object, no markdown, no commentary."""

USER_TEMPLATE = """SOURCE MATERIAL
Title: {title}
Source: {source} ({url})
Text: {text}

TASK
Write a {style} YouTube Short (35-58 seconds, {min_w}-{max_w} narration words total).
Structure: 5 to 7 scenes. Scene 1 must be type "title" (a bold hook, 12-22 words). The last scene
must be type "payoff" (a satisfying conclusion or twist, no call to action). Middle scenes mix
these types: "explain" (headline + 2-4 short bullets), "diagram" (3-5 node labels showing a flow),
"compare" (left vs right, 2-3 items each), "code" (3-6 short lines of pseudo-code or commands,
only if natural), "fact" (one striking phrase).
Each scene narration is 12-40 words and must flow when read aloud in order.
on_screen text is short (max 55 characters). Bullets/nodes/items max 36 characters each.
Bullets and nodes should reuse a key word the narration says, so they can appear as it is spoken.
For a "fact" scene, if the source gives a striking number, put it in on_screen as digits
(e.g. "98% of websites"); it is animated as a counter. Never invent one.
Every scene may add "kicker" (2-4 word label shown above the headline, e.g. "The catch",
"Why it matters") and "highlight" (1-3 words copied exactly from on_screen to show in colour).
"subject" is the main subject in 1-2 words (e.g. "GPT-5", "Rust", "UTF-8").

Return exactly this JSON shape:
{{
  "title": "YouTube title, max 90 chars, curiosity-driven, no clickbait lies, no ALL CAPS",
  "hook": "one-sentence hook",
  "subject": "...",
  "scenes": [
    {{"type": "title", "on_screen": "...", "highlight": "...", "narration": "..."}},
    {{"type": "explain", "on_screen": "...", "kicker": "...", "bullets": ["...", "..."], "narration": "..."}},
    {{"type": "diagram", "on_screen": "...", "nodes": ["...", "...", "..."], "narration": "..."}},
    {{"type": "compare", "on_screen": "...", "left_title": "...", "right_title": "...",
      "left": ["..."], "right": ["..."], "narration": "..."}},
    {{"type": "code", "on_screen": "...", "code": ["...", "..."], "narration": "..."}},
    {{"type": "fact", "on_screen": "...", "narration": "..."}},
    {{"type": "payoff", "on_screen": "...", "narration": "..."}}
  ],
  "description": "2-3 sentence YouTube description in your own words",
  "hashtags": ["#AI", "#Tech", "..."]
}}"""


DIALOGUE_TEMPLATE = """

DIALOGUE
This Short is a conversation between two characters: {host} (the host, who knows the topic and
explains it) and {guest} (the guest, who is curious, asks short questions and reacts).
Replace "narration" in every scene with "lines": 1-3 spoken lines in order, each
{{"who": "host" or "guest", "text": "..."}}. The host carries the facts. The guest speaks in at least
3 scenes with short lines (3-14 words): a question, surprise, or a quick recap in their own words.
They may use each other's names now and then. The scene and total word limits count all lines.
Aim for 135-155 words in total: pauses between speakers make dialogue slower, and a Short under
48 seconds feels thin.
Example scene: {{"type": "explain", "on_screen": "...", "bullets": ["..."], "lines": [
  {{"who": "guest", "text": "Wait, so how does it actually work?"}},
  {{"who": "host", "text": "..."}}]}}"""


def _names(cast: list[str] | None) -> tuple[str, str] | None:
    if not cast or len(cast) < 2:
        return None
    return CAST[cast[0]]["name"].title(), CAST[cast[1]]["name"].title()


def build_prompt(topic: Topic, theme: str, cast: list[str] | None = None) -> tuple[str, str]:
    text = topic.source_text()[:2600]
    user = USER_TEMPLATE.format(title=topic.title, source=topic.source, url=topic.url, text=text,
                                style=STYLE,
                                min_w=MIN_WORDS + 15, max_w=MAX_WORDS - 20)   # ~145 words ≈ 53 s of Piper speech
    names = _names(cast)
    if names:
        user += DIALOGUE_TEMPLATE.format(host=names[0], guest=names[1])
    return SYSTEM_PROMPT, user


def _lines(raw, cast: list[str] | None) -> list[dict]:
    """Normalise a scene's dialogue lines; speakers may come back as roles or character names."""
    names = _names(cast)
    alias = {"host": "host", "guest": "guest"}
    if names:
        alias.update({names[0].lower(): "host", names[1].lower(): "guest"})
    out = []
    for line in raw or []:
        if not isinstance(line, dict):
            continue
        text = " ".join(str(line.get("text", "")).split())
        if text:
            out.append({"who": alias.get(str(line.get("who", "host")).strip().lower(), "host"), "text": text})
    return out


# ----------------------------------------------------------------------------- validation
def _clip(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def validate_and_normalize(data: dict, topic: Topic, provider: str, theme: str,
                           cast: list[str] | None = None) -> VideoScript:
    problems = []
    title = _clip(data.get("title", ""), 95)
    if len(title) < 10:
        problems.append("title too short")
    if title.isupper():
        title = title.title()
    scenes = data.get("scenes") or []
    if not (MIN_SCENES <= len(scenes) <= MAX_SCENES):
        problems.append(f"scene count {len(scenes)} not in {MIN_SCENES}-{MAX_SCENES}")
    norm_scenes = []
    for i, s in enumerate(scenes):
        st = str(s.get("type", "")).lower().strip()
        if st not in SCENE_TYPES:
            st = "explain"
        lines = _lines(s.get("lines"), cast) if _names(cast) else []
        narration = " ".join(x["text"] for x in lines) if lines else " ".join(str(s.get("narration", "")).split())
        wc = word_count(narration)
        if wc < 6 or wc > 48:
            problems.append(f"scene {i + 1} narration has {wc} words")
        ns = {"type": st, "on_screen": _clip(s.get("on_screen", ""), 58), "narration": narration}
        if lines:
            ns["lines"] = lines
        for key, limit in (("kicker", 28), ("highlight", 40)):
            if str(s.get(key) or "").strip():
                ns[key] = _clip(s[key], limit)
        if st == "explain":
            bullets = [_clip(b, 40) for b in (s.get("bullets") or []) if str(b).strip()][:4]
            if not bullets:
                bullets = [ns["on_screen"] or _clip(narration, 40)]
            ns["bullets"] = bullets
        elif st == "diagram":
            nodes = [_clip(n, 30) for n in (s.get("nodes") or []) if str(n).strip()][:5]
            if len(nodes) < 2:
                st = ns["type"] = "explain"
                ns["bullets"] = [ns["on_screen"] or _clip(narration, 40)]
            else:
                ns["nodes"] = nodes
        elif st == "compare":
            ns["left_title"] = _clip(s.get("left_title", "Before"), 20)
            ns["right_title"] = _clip(s.get("right_title", "After"), 20)
            ns["left"] = [_clip(x, 34) for x in (s.get("left") or [])][:3] or ["—"]
            ns["right"] = [_clip(x, 34) for x in (s.get("right") or [])][:3] or ["—"]
        elif st == "code":
            code = [str(c).rstrip()[:40] for c in (s.get("code") or []) if str(c).strip()][:6]
            if not code:
                st = ns["type"] = "fact"
            else:
                ns["code"] = code
        norm_scenes.append(ns)
    if norm_scenes:
        norm_scenes[0]["type"] = "title"
        norm_scenes[-1]["type"] = "payoff"
        if not norm_scenes[0]["on_screen"]:
            norm_scenes[0]["on_screen"] = _clip(title, 58)
    if _names(cast):
        guest_scenes = sum(any(x["who"] == "guest" for x in sc.get("lines", [])) for sc in norm_scenes)
        if guest_scenes < 2:
            problems.append(f"the guest speaks in only {guest_scenes} scenes, needs at least 2")
    total = word_count(" ".join(s["narration"] for s in norm_scenes))
    if _names(cast) and provider != "template" and total < DIALOGUE_MIN_WORDS:
        problems.append(f"dialogue has {total} words, needs at least {DIALOGUE_MIN_WORDS} (about 45 s)")
    if not (MIN_WORDS <= total <= MAX_WORDS):
        problems.append(f"total narration {total} words not in {MIN_WORDS}-{MAX_WORDS}")
    low = " ".join(s["narration"] for s in norm_scenes).lower()
    for b in BANNED:
        if b in low:
            problems.append(f"banned phrase: {b}")
    hashtags = []
    for h in data.get("hashtags") or []:
        h = re.sub(r"[^A-Za-z0-9_]", "", str(h))
        if h:
            hashtags.append("#" + h)
    for default in ("#Shorts", "#Tech", "#AI"):
        if default not in hashtags and len(hashtags) < 8:
            hashtags.append(default)
    hashtags = hashtags[:8]
    description = _clip(data.get("description", "") or topic.summary, 600)
    if problems:
        raise ScriptError("; ".join(problems))
    return VideoScript(title=title, hook=_clip(data.get("hook", norm_scenes[0]["narration"]), 180),
                       scenes=norm_scenes, description=description, hashtags=hashtags,
                       sources=[topic.url], topic_id=topic.id, topic_title=topic.title,
                       provider=provider, theme=theme, category=topic.category,
                       extra={"source": topic.source, "published": topic.published or "",
                              "subject": _clip(data.get("subject") or "", 18), "cast": list(cast or [])})


# ----------------------------------------------------------------------------- template writer
def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", " ".join(text.split()))
    return [p.strip() for p in parts if len(p.split()) >= 4]


def _trim_words(s: str, n: int) -> str:
    words = s.split()
    if len(words) <= n:
        return s
    cut = " ".join(words[:n]).rstrip(",;:")
    return cut + "."


GUEST_QUESTIONS = {"explain": "Okay, so what is the core idea?", "diagram": "How does it actually work?",
                   "fact": "Wait, is that really true?", "compare": "So what is different?",
                   "payoff": "So what is the takeaway?"}


def template_script(topic: Topic, theme: str, cast: list[str] | None = None) -> VideoScript:
    """Deterministic writer that only rearranges verified source sentences. Always succeeds."""
    sents = _sentences(" ".join(x for x in [topic.summary, topic.excerpt] if x)) or [topic.title + "."]
    sents = [_trim_words(s, 32) for s in sents]
    hook = f"Here is something most people get wrong about {topic.title.rstrip('.')}."
    if topic.kind in ("news", "research"):
        hook = f"{topic.title.rstrip('.')}. Here is why it matters in under a minute."
    body = sents[:6]
    while word_count(hook + " " + " ".join(body)) < MIN_WORDS + 10 and len(body) < len(sents):
        body.append(sents[len(body)])
    scenes = [{"type": "title", "on_screen": _clip(topic.title, 58), "narration": hook}]
    chunk1 = body[:2] or [sents[0]]
    scenes.append({"type": "explain", "on_screen": "The core idea", "narration": " ".join(chunk1),
                   "bullets": [_clip(_key_phrase(s), 40) for s in chunk1]})
    chunk2 = body[2:4]
    if chunk2:
        scenes.append({"type": "diagram", "on_screen": "How it works", "narration": " ".join(chunk2),
                       "nodes": [_clip(_key_phrase(s), 30) for s in chunk2] + ["Result"]})
    chunk3 = body[4:6]
    if chunk3:
        scenes.append({"type": "fact", "on_screen": _clip(_key_phrase(chunk3[0]), 58),
                       "narration": " ".join(chunk3)})
    fillers = [("Why it matters", "The key point is simple: understanding this idea explains a lot of modern technology."),
               ("The big picture", "Once you see this pattern, you start noticing it everywhere in the tools you use daily.")]
    while len(scenes) < 4 and fillers:
        on_screen, narration = fillers.pop(0)
        scenes.append({"type": "fact", "on_screen": on_screen, "narration": narration})
    scenes.append({"type": "payoff", "on_screen": "Now you know",
                   "narration": f"So next time someone mentions {_short_title(topic.title)}, you know exactly what is going on behind the scenes."})
    # enforce word budget
    def total():
        return word_count(" ".join(s["narration"] for s in scenes))
    pads = ["That single idea shaped how we build systems today.",
            "It sounds small, but it changes everything downstream."]
    while total() < MIN_WORDS and pads:
        scenes[-2]["narration"] += " " + pads.pop(0)
    while total() > MAX_WORDS and len(scenes) > 5:
        scenes.pop(-2)
    for s in scenes:
        if word_count(s["narration"]) > 44:
            s["narration"] = _trim_words(s["narration"], 40)
    if _names(cast):
        # the guest asks a short question before the host's part, while the word budget allows
        for s in scenes:
            s["lines"] = [{"who": "host", "text": s["narration"]}]
            q = GUEST_QUESTIONS.get(s["type"])
            if q and total() + word_count(q) <= MAX_WORDS and word_count(s["narration"] + " " + q) <= 48:
                s["lines"].insert(0, {"who": "guest", "text": q})
                s["narration"] = q + " " + s["narration"]
    data = {"title": _clip(topic.title, 90), "hook": hook, "scenes": scenes,
            "description": _clip(" ".join(sents[:2]), 500),
            "hashtags": ["#Shorts", "#Tech", "#AI", "#ComputerScience"]}
    try:
        return validate_and_normalize(data, topic, "template", theme, cast)
    except ScriptError:
        return validate_and_normalize(data, topic, "template", theme)      # too long for a dialogue: narrate


def _key_phrase(sentence: str, max_chars: int = 38) -> str:
    words = re.sub(r"[^A-Za-z0-9 \-'%]", "", sentence).split()
    out = []
    for w in words:
        if len(" ".join(out + [w])) > max_chars:
            break
        out.append(w)
    phrase = " ".join(out) or sentence[:max_chars]
    return phrase[0].upper() + phrase[1:]


def _short_title(title: str) -> str:
    t = re.split(r"[:\-–]", title)[0].strip()
    return t[:60]


# ----------------------------------------------------------------------------- entry point
def write_script(topic: Topic, theme: str, cfg, logger, cast: list[str] | None = None) -> VideoScript:
    system, user = build_prompt(topic, theme, cast)
    attempts = int(cfg.llm.max_attempts_per_topic)
    last_err = None
    for attempt in range(attempts):
        try:
            text, provider = chat(cfg, system, user, logger)
            data = extract_json(text)
            script = validate_and_normalize(data, topic, provider, theme, cast)
            logger.info("script ok via %s: %d scenes, %d words", provider, len(script.scenes), script.total_words())
            return script
        except LLMError as e:
            logger.warning("LLM unavailable: %s", str(e)[:200])
            break
        except (ScriptError, ValueError, json.JSONDecodeError) as e:
            last_err = e
            logger.warning("script attempt %d rejected: %s", attempt + 1, str(e)[:200])
            user = user + f"\n\nYour previous answer was rejected because: {e}. Fix it and return valid JSON only."
    logger.info("falling back to template writer%s", f" ({last_err})" if last_err else "")
    return template_script(topic, theme, cast)
