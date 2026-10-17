"""Long-form scripts (8-10 minutes, 16:9): one outline call, then one call per chapter.

Writing chapter by chapter keeps every request small enough for free-tier token-per-minute limits
and lets each chapter be validated (and retried) on its own. The chapters are stitched into one
VideoScript: chapter cards between chapters (the guest asks the question the chapter answers), a
like ask about halfway, and an outro with the subscribe, share and comment asks. Same characters,
same fact rules as the Shorts. There is no template fallback: a weak 10-minute video is worse
than none, so a failure here just means the day has fewer long videos.
"""
from __future__ import annotations

import json
import re
import time

from .llm import LLMError, chat
from .research import Topic
from .scripting import (BANNED, LONG_END_ASKS, LONG_MID_ASKS, ScriptError, VideoScript, _clip, _names,
                        comment_prompt, normalize_scene, pick)
from .utils import extract_json, word_count

SYSTEM = """You write scripts for original, educational long-form YouTube videos about technology.
You MUST use only facts present in the SOURCE MATERIAL. Never invent numbers, names, dates or quotes.
If the source lacks a detail, explain the idea conceptually instead of guessing.
Write in plain spoken English for two characters having a real conversation. No emojis.
Never copy sentences verbatim from the source; paraphrase in your own words.
Output ONLY one JSON object, no markdown, no commentary."""

OUTLINE_TEMPLATE = """SOURCE MATERIAL
Title: {title}
Source: {source} ({url})
Angle: {angle}
Text: {text}{insights}

TASK
Plan a {minutes}-minute YouTube video (16:9, not a Short) as a conversation between {host} (the host,
who knows the topic) and {guest} (the curious guest, who asks what a viewer would ask).
Model it on the most-watched explainers: open with the most surprising concrete fact or question,
promise what the viewer will understand by the end, then tell it as a story with rising stakes.
Every chapter should leave an open question that the next chapter answers.
Plan {cmin} to {cmax} chapters. Use only facts from the source material.

Return exactly this JSON shape:
{{
  "title": "YouTube title, 35-70 characters, concrete and curiosity-driven, names something people know; no clickbait lies, no ALL CAPS",
  "thumbnail_text": "2-4 punchy words for the thumbnail, not the title repeated",
  "hook": "one-sentence hook",
  "subject": "main subject in 1-2 words",
  "description": "3-4 sentence YouTube description in your own words",
  "hashtags": ["#Tech", "..."],
  "comment_question": "a question viewers will want to answer in the comments, max 14 words",
  "share_with": "who a viewer would send this video to, e.g. a friend who still reuses passwords (max 10 words)",
  "chapters": [
    {{"title": "2-5 word chapter title", "question": "the guest's question that opens the chapter, max 14 words",
      "points": ["a fact or idea from the source this chapter covers", "..."]}}
  ]
}}"""

CHAPTER_TEMPLATE = """SOURCE MATERIAL
Title: {title}
Text: {text}

VIDEO PLAN
Video title: {video_title}
Chapters: {chapter_list}

TASK
Write chapter {k} of {n}: "{ctitle}". {position}
Cover these points: {points}
The video is a conversation between {host} (the host: explains and carries the facts) and {guest} (the guest:
asks short questions, reacts, sums up in their own words, sometimes pushes back).
Write {smin} to {smax} scenes with {wmin}-{wmax} spoken words in total for this chapter.
This is a documentary: the viewer sees real photos and footage, so every scene needs "search": a 2-5 word
query for real photos or video of something concrete and visible (objects, places, machines, named people
or companies), e.g. "nuclear centrifuge hall", "server room cables", "Garry Kasparov". Never abstract ideas.
Scene types: {types}"photo" (real footage carries it; headline only), "explain" (headline + 2-4 short bullets),
"quote" (one exact sentence copied word for word from the SOURCE MATERIAL, max 40 words, shown as a highlighted
article clipping), "timeline" (2-5 dated events from the source), "chart" (2-5 numbers from the source to
compare as bars), "fact" (one striking number from the source in on_screen as digits, animated as a counter),
"diagram" (3-5 node labels showing a flow), "compare" (left vs right, 2-3 items each), "code" (3-6 short
lines, only if natural).
Mix the types: mostly "photo" and "explain", plus a "quote", "timeline", "chart" or "fact" whenever the
source supports one; at most 1 "compare" and 1 "diagram" per chapter. Every scene must fill its fields.
Each scene has "lines": 1-4 spoken lines in order, each {{"who": "host" or "guest", "text": "..."}}, 15-85 words
per scene. The guest speaks in at least half of the scenes. Vary sentence length; it must sound like two
people talking, not a lecture.
on_screen is short (max 55 characters). Bullets, nodes and items are max 36 characters each and reuse a key
word the lines say, so they can appear as it is spoken. Optional per scene: "kicker" (2-4 word label) and
"highlight" (1-3 words copied exactly from on_screen).
{ending}
Do not greet the viewer, do not ask them to like, comment or subscribe (that is added separately), and do not
mention chapter numbers.

Return exactly this JSON shape (scene shapes by type; "search" and "lines" in every scene):
{{"scenes": [
  {{"type": "photo", "on_screen": "...", "kicker": "...", "search": "...", "lines": [{{"who": "guest", "text": "..."}}, {{"who": "host", "text": "..."}}]}},
  {{"type": "explain", "on_screen": "...", "search": "...", "bullets": ["...", "..."], "lines": [...]}},
  {{"type": "quote", "on_screen": "...", "search": "...", "quote": "exact sentence from the source", "lines": [...]}},
  {{"type": "timeline", "on_screen": "...", "search": "...", "timeline": [{{"when": "2010", "label": "..."}}, {{"when": "2012", "label": "..."}}], "lines": [...]}},
  {{"type": "chart", "on_screen": "...", "search": "...", "chart": {{"unit": "%", "bars": [{{"label": "...", "value": 42}}, {{"label": "...", "value": 17}}]}}, "lines": [...]}},
  {{"type": "fact", "on_screen": "...", "search": "...", "highlight": "...", "lines": [...]}},
  {{"type": "diagram", "on_screen": "...", "search": "...", "nodes": ["...", "...", "..."], "lines": [...]}},
  {{"type": "compare", "on_screen": "...", "search": "...", "left_title": "...", "right_title": "...", "left": ["..."], "right": ["..."], "lines": [...]}}
]}}"""

SCENE_MIN_WORDS, SCENE_MAX_WORDS = 8, 95


def _llm_json(cfg, user: str, logger, check, attempts: int = 3):
    """Ask, parse and validate with `check(data)`; feed the rejection reason back on a retry."""
    last = None
    for attempt in range(attempts):
        try:
            text, provider = chat(cfg, SYSTEM, user, logger)
        except LLMError as e:                     # a timeout or rate limit mid-video: wait, then try again
            last = e
            logger.warning("long-form attempt %d: no provider answered, retrying in 20 s", attempt + 1)
            time.sleep(20)
            continue
        try:
            data = extract_json(text)
            return check(data), provider
        except (ScriptError, ValueError, json.JSONDecodeError, KeyError, TypeError) as e:
            last = e
            logger.warning("long-form attempt %d rejected: %s", attempt + 1, str(e)[:200])
            user += f"\n\nYour previous answer was rejected because: {e}. Fix it and return valid JSON only."
    raise ScriptError(f"long-form step failed: {last}")


def _check_outline(data: dict, cmin: int, cmax: int) -> dict:
    chapters = [c for c in data.get("chapters") or [] if isinstance(c, dict) and str(c.get("title", "")).strip()]
    if not cmin <= len(chapters) <= cmax + 1:
        raise ScriptError(f"{len(chapters)} chapters, need {cmin}-{cmax}")
    title = _clip(data.get("title", ""), 95)
    if len(title) < 15:
        raise ScriptError("title too short")
    data["chapters"] = chapters[:cmax]
    data["title"] = title.title() if title.isupper() else title
    return data


def _norm_text(x: str) -> str:
    x = str(x).translate(str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "‑": "-"}))
    return re.sub(r"[^a-z0-9%$.,' -]", "", " ".join(x.lower().split()))


def _number_in(value, source_norm: str) -> bool:
    v = float(value)
    forms = {f"{v:g}", f"{v:,.0f}" if v.is_integer() else f"{v:,}", str(int(v)) if v.is_integer() else f"{v}"}
    return any(f.lower() in source_norm or f.replace(",", "") in source_norm.replace(",", "") for f in forms)


def _documentary_fields(raw: dict, ns: dict, source_norm: str) -> None:
    """Search query, quote, timeline and chart; anything not backed by the source becomes a photo scene."""
    ns["search"] = _clip(raw.get("search") or ns.get("on_screen") or "", 60)
    kind = ns["type"]
    if kind == "quote":
        q = " ".join(str(raw.get("quote") or "").split()).strip(' "')
        if q and 6 <= len(q.split()) <= 45 and _norm_text(q) in source_norm:
            ns["quote"] = q
        else:
            ns["type"] = "photo"
    elif kind == "timeline":
        ev = [{"when": _clip(e.get("when", ""), 14), "label": _clip(e.get("label", ""), 44)}
              for e in raw.get("timeline") or [] if isinstance(e, dict) and str(e.get("when", "")).strip()]
        if len(ev) >= 2 and all(_norm_text(e["when"]) in source_norm for e in ev):
            ns["timeline"] = ev[:5]
        else:
            ns["type"] = "photo"
    elif kind == "chart":
        ch = raw.get("chart") if isinstance(raw.get("chart"), dict) else {}
        bars = []
        for b in ch.get("bars") or []:
            try:
                bars.append({"label": _clip(b.get("label", ""), 24), "value": float(str(b.get("value")).replace(",", ""))})
            except (TypeError, ValueError, AttributeError):
                continue
        if len(bars) >= 2 and all(b["value"] > 0 and _number_in(b["value"], source_norm) for b in bars):
            ns["chart"] = {"unit": _clip(ch.get("unit") or "", 12), "bars": bars[:5]}
        else:
            ns["type"] = "photo"


def _check_chapter(data: dict, cast, k: int, n: int, smin: int, smax: int, wmin: int, wmax: int,
                   source: str = "") -> list[dict]:
    scenes, problems = [], []
    source_norm = _norm_text(source)
    for i, raw in enumerate(data.get("scenes") or []):
        if not isinstance(raw, dict):
            continue
        ns, errs = normalize_scene(raw, cast, SCENE_MIN_WORDS, SCENE_MAX_WORDS)
        _documentary_fields(raw, ns, source_norm)
        kind = str(raw.get("type", "")).lower()
        need = {"explain": ("bullets",), "diagram": ("nodes",), "compare": ("left", "right"), "code": ("code",)}
        missing = [f for f in need.get(kind, ()) if not [x for x in raw.get(f) or [] if str(x).strip()]]
        if missing:                              # an empty panel on screen for 20 seconds looks broken
            errs.append(f"{kind} scene without {' and '.join(missing)}")
        if ns["type"] in ("chapter", "outro", "title", "payoff"):
            ns["type"] = "explain" if ns["type"] in ("chapter", "outro") else ns["type"]
        problems += [f"scene {i + 1} {e}" for e in errs]
        scenes.append(ns)
    if not smin <= len(scenes) <= smax + 2:
        problems.append(f"{len(scenes)} scenes, need {smin}-{smax}")
    words = sum(word_count(s["narration"]) for s in scenes)
    if not int(wmin * 0.8) <= words <= int(wmax * 1.25):
        problems.append(f"chapter has {words} words, need {wmin}-{wmax}")
    if _names(cast):
        guest = sum(any(x["who"] == "guest" for x in s.get("lines", [])) for s in scenes)
        if guest < max(1, len(scenes) // 3):
            problems.append(f"the guest speaks in only {guest} of {len(scenes)} scenes")
    low = " ".join(s["narration"] for s in scenes).lower()
    problems += [f"banned phrase: {b}" for b in BANNED if b in low]
    if problems:
        raise ScriptError("; ".join(problems[:6]))
    for s in scenes:
        if s["type"] in ("title", "payoff"):
            s["type"] = "explain" if "bullets" in s else "fact"
    if k == 1:
        scenes[0]["type"] = "title"
        scenes[0].pop("bullets", None)
    if k == n:
        scenes[-1]["type"] = "payoff"
    return scenes


def _line(who: str, text: str) -> dict:
    return {"who": who, "text": text}


def _card(scene_type: str, on_screen: str, kicker: str, lines: list[dict], narrate: bool) -> dict:
    s = {"type": scene_type, "on_screen": _clip(on_screen, 58), "kicker": kicker,
         "narration": " ".join(x["text"] for x in lines)}
    if narrate:
        s["lines"] = lines
    return s


def write_long_script(topic: Topic, theme: str, cfg, logger, cast: list[str] | None = None,
                      insights: str = "", seed: int = 0) -> VideoScript:
    lv = cfg.long_video
    cmin, cmax = (int(x) for x in lv.chapters)
    wlo, whi = (int(x) for x in lv.target_words)
    names = _names(cast) or ("Host", "Guest")
    text = topic.source_text()
    outline, provider = _llm_json(cfg, OUTLINE_TEMPLATE.format(
        title=topic.title, source=topic.source, url=topic.url, angle=topic.summary[:300], text=text[:6500],
        insights=insights, minutes=f"{wlo // 160}-{whi // 160}", host=names[0], guest=names[1], cmin=cmin, cmax=cmax),
        logger, lambda d: _check_outline(d, cmin, cmax))
    chapters = outline["chapters"]
    n = len(chapters)
    logger.info("long-form outline via %s: %r, %d chapters", provider, outline["title"], n)
    per = (wlo + whi) / 2 / n
    wmin, wmax = int(per * 0.8), int(per * 1.25)
    smin, smax = max(3, round(per / 60)), max(5, round(per / 32))
    chapter_list = "; ".join(f"{i + 1}. {c['title']}" for i, c in enumerate(chapters))
    providers = {provider}
    scenes: list[dict] = []
    mid = n // 2 + 1                                 # the like ask opens this chapter, after real value
    q_default = "So what happens next?"
    comment_q = comment_prompt(outline.get("comment_question")) or "What should we break down next?"
    share_with = _clip(re.sub(r"[.!?]+$", "", str(outline.get("share_with") or "").strip()), 70) \
        or "a friend who'd find this interesting"
    duo = bool(_names(cast))
    for k, ch in enumerate(chapters, 1):
        ctitle = _clip(ch["title"], 40)
        question = _clip(str(ch.get("question") or q_default), 110)
        if k == 1:
            position = ('This chapter opens the video. Scene 1 must be type "title": the hook, the most surprising '
                        'concrete fact or question, then the host promises what the viewer will understand by the end. '
                        'Keep scene 1 under 45 words.')
        else:
            position = (f'The guest has just asked: "{question}" (already spoken, do not repeat it). '
                        'Your first scene answers it.')
        if k == n:
            ending = ('This is the final chapter. The last scene must be type "payoff": the big takeaway and what it '
                      'means for the viewer, in a satisfying final line.')
        else:
            ending = (f'End the chapter on a line that makes the viewer want the next chapter, "{chapters[k]["title"]}", '
                      'without answering it.')
        points = "; ".join(_clip(p, 160) for p in (ch.get("points") or [])[:6]) or ctitle
        user = CHAPTER_TEMPLATE.format(
            title=topic.title, text=text[:5000], video_title=outline["title"], chapter_list=chapter_list, k=k, n=n,
            ctitle=ctitle, position=position, points=points, host=names[0], guest=names[1], smin=smin, smax=smax,
            wmin=wmin, wmax=wmax, ending=ending, types='"title" (only the first scene), ' if k == 1 else "")
        body, prov = _llm_json(cfg, user, logger, lambda d: _check_chapter(d, cast, k, n, smin, smax, wmin, wmax, text))
        providers.add(prov)
        logger.info("  chapter %d/%d %r: %d scenes, %d words", k, n, ctitle, len(body),
                    sum(word_count(s["narration"]) for s in body))
        if k > 1:
            lines = [_line("guest", question)]
            card = _card("chapter", ctitle, "Chapter", lines, duo)
            if k == mid:
                card = _card("chapter", ctitle, "Chapter", [_line("host", pick(LONG_MID_ASKS, seed))] + lines, duo)
                card["cta"], card["cta_from"] = "like", 0
            body.insert(0, card)
        for s in body:
            s["chapter"], s["chapter_index"] = ctitle, k
        scenes += body
    outro_lines = [_line("host", pick(LONG_END_ASKS, seed // 3)),
                   _line("guest", f"And send this to {share_with}."),
                   _line("host", f"{comment_q} Tell us in the comments. See you in the next one.")]
    outro = _card("outro", "New explainer every day", "Before you go", outro_lines, duo)
    outro.update(cta="end", cta_from=0, chapter=scenes[-1]["chapter"], chapter_index=n)
    scenes.append(outro)

    total = sum(word_count(s["narration"]) for s in scenes)
    if total < int(wlo * 0.8):
        raise ScriptError(f"long-form script has only {total} words")
    hashtags = []
    for h in outline.get("hashtags") or []:
        h = re.sub(r"[^A-Za-z0-9_]", "", str(h))
        if h and h.lower() != "shorts" and "#" + h not in hashtags:
            hashtags.append("#" + h)
    for default in ("#Tech", "#Explained"):
        if default not in hashtags and len(hashtags) < 6:
            hashtags.append(default)
    sources = list(dict.fromkeys([topic.url] + list(topic.extra.get("sources") or [])))
    logger.info("long-form script ok: %d chapters, %d scenes, %d words", n, len(scenes), total)
    return VideoScript(
        title=outline["title"], hook=_clip(outline.get("hook") or scenes[0]["narration"], 180), scenes=scenes,
        description=_clip(outline.get("description") or topic.summary, 900), hashtags=hashtags[:6],
        sources=sources, topic_id=topic.id, topic_title=topic.title, provider="+".join(sorted(providers)),
        theme=theme, category=topic.category,
        extra={"format": "long", "source": topic.source, "published": topic.published or "",
               "subject": _clip(outline.get("subject") or "", 18), "cast": list(cast or []),
               "chapters": [_clip(c["title"], 40) for c in chapters],
               "thumbnail_text": _clip(outline.get("thumbnail_text") or "", 32),
               "comment_prompt": comment_q, "share_with": share_with})


def chapter_times(script: VideoScript, scene_starts: list[float]) -> list[dict]:
    """YouTube chapter markers: the first starts at 0:00, each lasts 10 s or more."""
    out: list[dict] = []
    for scene, start in zip(script.scenes, scene_starts):
        k = scene.get("chapter_index")
        if k and (not out or out[-1]["index"] != k):
            out.append({"index": k, "title": scene.get("chapter") or f"Part {k}", "start": 0.0 if not out else start})
    keep = [c for i, c in enumerate(out) if i + 1 >= len(out) or out[i + 1]["start"] - c["start"] >= 10]
    return keep if len(keep) >= 3 else []


def fmt_timestamp(seconds: float) -> str:
    s = int(seconds)
    h, m, s = s // 3600, s // 60 % 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
