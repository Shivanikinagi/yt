"""Real photos and footage for documentary-style long-form videos.

Sources, all free and licensed for reuse:
- images used in the Wikipedia articles the script is based on (most relevant, e.g. the actual
  centrifuge hall for Stuxnet), filtered to free licenses (non-free "fair use" files are skipped)
- Wikimedia Commons search for each scene's visual query
- Pexels stock video and photos when PEXELS_API_KEY is set (free key; no attribution required,
  credited anyway). Real footage is what makes a video look filmed rather than generated.

Every asset carries a credit line (title, author, license) that is shown on screen while it is
visible and listed in the description, which is what CC BY / CC BY-SA require.
"""
from __future__ import annotations

import concurrent.futures as cf
import os
import re
from pathlib import Path

import requests
from PIL import Image

from .research import UA
from .utils import strip_html

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
WIKI_API = "https://en.wikipedia.org/w/api.php"
PEXELS_API = "https://api.pexels.com"
HEADERS = {"User-Agent": UA}
IMAGE_PROPS = {"prop": "imageinfo", "iiprop": "url|size|extmetadata|mime", "iiurlwidth": 1920, "format": "json"}
# interface icons, flags and logos that articles embed but that look cheap full screen
_JUNK = re.compile(r"(icon|logo|symbol|flag of|emoji|commons-|wiki|padlock|ambox|question[_ ]book|edit-|crystal|"
                   r"nuvola|oojs|portal|stub|disambig|folder|gnome|tango|signature|seal of|coat of arms|"
                   r"increase|decrease|steady|red pog|location map|blank map|loudspeaker)", re.I)


def _license_ok(lic: str) -> bool:
    lic = (lic or "").lower().replace("-", " ")
    if not lic or " nc" in f" {lic}" or " nd" in f" {lic}" or "fair use" in lic or "non free" in lic:
        return False
    return lic.startswith(("cc0", "public domain", "pd", "cc by", "attribution", "no restrictions"))


def _asset_from_imageinfo(title: str, ii: dict, source: str) -> dict | None:
    meta = ii.get("extmetadata") or {}
    val = lambda k: strip_html(str((meta.get(k) or {}).get("value", "")))  # noqa: E731
    lic = val("LicenseShortName")
    if val("NonFree").lower() == "true" or not _license_ok(lic) or _JUNK.search(title):
        return None
    mime = ii.get("mime", "")
    w, h = int(ii.get("width") or 0), int(ii.get("height") or 0)
    if mime == "image/svg+xml":
        kind = "graphic"
    elif mime in ("image/jpeg", "image/png", "image/webp", "image/tiff"):
        if w < 900 or h < 500 or h > w * 1.35:          # too small or a tall portrait crop for 16:9
            return None
        kind = "photo" if mime == "image/jpeg" else "graphic"
    else:
        return None
    name = re.sub(r"\.\w+$", "", title.replace("File:", "")).replace("_", " ")
    artist = re.sub(r"\s+", " ", val("Artist"))[:60] or "Unknown author"
    return {"url": ii.get("thumburl") or ii.get("url"), "page": ii.get("descriptionurl", ""), "kind": kind,
            "name": name, "words": set(re.findall(r"[a-z0-9]{3,}", name.lower() + " " + val("ImageDescription").lower()[:300])),
            "credit": f"{artist} / {lic}", "title": name[:80], "source": source}


def _imageinfo_query(api: str, params: dict, source: str, timeout: int) -> list[dict]:
    try:
        r = requests.get(api, params={**params, **IMAGE_PROPS, "action": "query"}, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        pages = (r.json().get("query") or {}).get("pages") or {}
    except Exception:
        return []
    out = []
    for p in sorted(pages.values(), key=lambda p: p.get("index", 0)):
        ii = (p.get("imageinfo") or [None])[0]
        a = ii and _asset_from_imageinfo(p.get("title", ""), ii, source)
        if a:
            out.append(a)
    return out


def article_images(pages: list[str], timeout: int = 20) -> list[dict]:
    out = []
    for page in pages:
        out += _imageinfo_query(WIKI_API, {"generator": "images", "titles": page, "gimlimit": 60}, "wikipedia", timeout)
    return out


_STOP = {"the", "and", "for", "with", "from", "of", "in", "on", "at", "a", "an", "to", "photo", "image", "picture",
         "diagram", "chart", "illustration", "close", "view", "shot"}


def query_words(query: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{2,}", query.lower()) if w not in _STOP}


def relevant(asset: dict, query: str) -> bool:
    """Commons search is fuzzy ("home router" finds a politician); keep files whose own title or
    description mentions the query words: all of them for 1-2 words, at least two for longer ones."""
    want = query_words(query)
    hit = len(want & asset["words"])
    return bool(want) and hit >= min(len(want), 2)


def commons_search(query: str, limit: int = 12, timeout: int = 20) -> list[dict]:
    found = _imageinfo_query(COMMONS_API, {"generator": "search", "gsrnamespace": 6, "gsrlimit": limit,
                                           "gsrsearch": query}, "commons", timeout)
    return [a for a in found if relevant(a, query)]


def commons_search_relaxed(query: str, want: int = 3) -> list[dict]:
    """The full query first, then shorter versions of it, until enough relevant files turn up."""
    words = query.split()
    variants = [query] + ([" ".join(words[:2]), " ".join(words[-2:])] if len(words) > 2 else []) +         ([words[0]] if len(words) == 2 and len(words[0]) > 3 else [])
    out, seen = [], set()
    for v in dict.fromkeys(variants):
        for a in commons_search(v):
            if a["url"] not in seen:
                seen.add(a["url"])
                out.append(a)
        if len(out) >= want:
            break
    return out


def pexels_videos(query: str, key: str, limit: int = 4, timeout: int = 20) -> list[dict]:
    try:
        r = requests.get(f"{PEXELS_API}/videos/search", params={"query": query, "per_page": limit,
                                                                 "orientation": "landscape", "size": "medium"},
                         headers={"Authorization": key, **HEADERS}, timeout=timeout)
        r.raise_for_status()
        videos = r.json().get("videos") or []
    except Exception:
        return []
    out = []
    for v in videos:
        files = [f for f in v.get("video_files") or [] if f.get("file_type") == "video/mp4"
                 and 1280 <= int(f.get("width") or 0) <= 1920 and int(f.get("height") or 0) < int(f.get("width") or 1)]
        if not files or int(v.get("duration") or 0) < 4:
            continue
        f = max(files, key=lambda f: int(f.get("width") or 0))
        who = (v.get("user") or {}).get("name", "Pexels")
        out.append({"url": f["link"], "page": v.get("url", ""), "kind": "video", "name": query,
                    "words": set(query.lower().split()), "credit": f"Video: {who} / Pexels", "source": "pexels",
                    "duration": float(v.get("duration") or 0)})
    return out


def pexels_photos(query: str, key: str, limit: int = 4, timeout: int = 20) -> list[dict]:
    try:
        r = requests.get(f"{PEXELS_API}/v1/search", params={"query": query, "per_page": limit, "orientation": "landscape"},
                         headers={"Authorization": key, **HEADERS}, timeout=timeout)
        r.raise_for_status()
        photos = r.json().get("photos") or []
    except Exception:
        return []
    return [{"url": p["src"].get("large2x") or p["src"]["original"], "page": p.get("url", ""), "kind": "photo",
             "name": query, "words": set(query.lower().split()),
             "credit": f"Photo: {p.get('photographer', 'Pexels')} / Pexels", "source": "pexels"} for p in photos]


def _download(asset: dict, path: Path, timeout: int = 60) -> dict | None:
    try:
        with requests.get(asset["url"], headers=HEADERS, timeout=timeout, stream=True) as r:
            r.raise_for_status()
            data = b""
            for chunk in r.iter_content(1 << 16):
                data += chunk
                if len(data) > 60 * 1024 * 1024:
                    return None
        if asset["kind"] == "video":
            path = path.with_suffix(".mp4")
            path.write_bytes(data)
        else:
            import io
            img = Image.open(io.BytesIO(data))
            img = img.convert("RGBA") if img.mode in ("RGBA", "LA", "P") else img.convert("RGB")
            if img.mode == "RGBA":                     # diagrams with transparency: put them on white
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.getchannel("A"))
                img = bg
            img.thumbnail((2600, 2600))
            path = path.with_suffix(".jpg")
            img.save(path, "JPEG", quality=90)
        return {**{k: v for k, v in asset.items() if k != "words"}, "path": str(path)}
    except Exception:
        return None


def _score(asset: dict, words: set[str]) -> float:
    return len(asset["words"] & words) + (0.5 if asset["kind"] in ("video", "photo") else 0.0)


def gather(script, topic, durations: list[float], out_dir: Path, logger, cfg=None) -> dict:
    """Find and download visuals for every scene. Returns {"scenes": [[asset...]...], "pool": [...]}.

    About one asset per 5 seconds of a scene, the best matches for its visual query first; the
    pool (the article's own images and the subject) fills scenes that found nothing."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    key = os.environ.get("PEXELS_API_KEY", "").strip()
    pages = list((topic.extra or {}).get("pages") or [])
    if not pages and "wikipedia.org/wiki/" in (topic.url or ""):
        pages = [requests.utils.unquote(topic.url.rsplit("/", 1)[-1]).replace("_", " ")]
    pool = article_images(pages)
    subject = (script.extra or {}).get("subject") or script.topic_title
    for q in dict.fromkeys([subject] + pages):
        pool += commons_search(q, 20)
    if key:
        pool = pexels_videos(subject, key, 3) + pool
    queries = []
    for scene, dur in zip(script.scenes, durations):
        q = " ".join(str(scene.get("search") or "").split())[:60]
        queries.append((q, max(1, min(4, round(dur / 5)))))
    unique = list(dict.fromkeys(q for q, _ in queries if q))

    def search(q):
        found = []
        if key:
            found += pexels_videos(q, key, 3) + pexels_photos(q, key, 2)
        return q, found + commons_search_relaxed(q)
    with cf.ThreadPoolExecutor(6) as ex:
        results = dict(ex.map(search, unique))

    used: set[str] = set()
    picks: list[list[dict]] = []
    for q, n in queries:
        words = set(re.findall(r"[a-z0-9]{3,}", q.lower()))
        cands = results.get(q, []) + sorted(pool, key=lambda a: -_score(a, words))[:4]
        chosen = []
        for a in cands:
            if a["url"] not in used and len(chosen) < n:
                chosen.append(a)
                used.add(a["url"])
        picks.append(chosen)
    pool_rest = [a for a in pool if a["url"] not in used][:12]

    todo = {a["url"]: a for a in [a for p in picks for a in p] + pool_rest}
    done: dict[str, dict] = {}
    with cf.ThreadPoolExecutor(8) as ex:
        futs = {ex.submit(_download, a, out_dir / f"{i:03d}"): u for i, (u, a) in enumerate(todo.items())}
        for f in cf.as_completed(futs):
            res = f.result()
            if res:
                done[futs[f]] = res
    scenes = [[done[a["url"]] for a in p if a["url"] in done] for p in picks]
    pool_ok = [done[a["url"]] for a in pool_rest if a["url"] in done]
    n_assets = len(done)
    logger.info("media: %d assets (%d video) for %d scenes, %d scenes without their own visuals",
                n_assets, sum(1 for a in done.values() if a["kind"] == "video"), len(scenes),
                sum(1 for s in scenes if not s))
    return {"scenes": scenes, "pool": pool_ok or [a for s in scenes for a in s][:12]}


def credits(media: dict) -> list[str]:
    seen, out = set(), []
    for a in [a for s in media.get("scenes", []) for a in s] + media.get("pool", []):
        line = " / ".join(x for x in (a.get("title"), a["credit"], a.get("page")) if x)
        if line not in seen:
            seen.add(line)
            out.append(line)
    return out
