"""Documentary-style renderer for long-form videos: real photos and footage, cut every few seconds.

Built like the explainer documentaries that get millions of views: slow camera moves over real
photos (Ken Burns), stock footage, highlighted article clippings, animated timelines and charts,
big number reveals, full-screen chapter cards, sound effects on transitions. The two characters
keep a small role (the intro, short reactions, the like ask and the outro), drawn by the regular
Renderer. Captions sit at the bottom like film subtitles; every photo credits its author on screen.

plan_shots() turns scenes into a list of shots on the global timeline; DocuRenderer.frame() draws
whichever shot is live at time t, so it plugs into render.py exactly like Renderer.
"""
from __future__ import annotations

import bisect
import math
import re
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

from .utils import ffmpeg_exe
from .visuals import (AMBER, INK, MUTED, Renderer, _NUMBER, clamp01, clean, ease_in_out, ease_out, lerp_rgb,
                      source_meta, wrap_text)

SHOT_LEN = 4.6           # seconds of b-roll per shot
GRAPHIC = {"diagram": "panel", "compare": "panel", "code": "panel", "quote": "quote", "timeline": "timeline",
           "chart": "chart", "fact": "stat"}
PAPER = (243, 239, 229)
PAPER_INK = (32, 32, 36)
HIGHLIGHT = (255, 222, 89)


# ----------------------------------------------------------------------------- shot planning
def _subtract(a: float, b: float, cuts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out, cur = [], a
    for c0, c1 in sorted(cuts):
        if c0 > cur:
            out.append((cur, min(c0, b)))
        cur = max(cur, c1)
    if cur < b:
        out.append((cur, b))
    return [(x, y) for x, y in out if y - x > 0.05]


def _main_kind(v: dict) -> str:
    kind = GRAPHIC.get(v.get("type"), "broll")
    if kind == "quote" and not v.get("quote"):
        return "broll"
    if kind == "timeline" and len(v.get("timeline") or []) < 2:
        return "broll"
    if kind == "chart" and len((v.get("chart") or {}).get("bars") or []) < 2:
        return "broll"
    return kind


def plan_shots(views: list[dict], timeline: list[dict], media: dict, characters: bool = True) -> list[dict]:
    """Shots covering the whole video, in order: {start, end, kind, scene, asset, overlay, move}."""
    pool = list(media.get("pool") or [])
    per_scene = list(media.get("scenes") or [])
    shots: list[dict] = []
    reacts = 0
    state = {"last": None, "pool": 0}

    def asset_for(i: int, k: int):
        """The scene's own visuals in order, then the shared pool in rotation; never the same twice in a row."""
        own = per_scene[i] if i < len(per_scene) else []
        for _ in range(len(own) + len(pool) + 1):
            if k < len(own):
                a = own[k]
                k += 1
            elif pool:
                a = pool[state["pool"] % len(pool)]
                state["pool"] += 1
            else:
                a = own[k % len(own)] if own else None
                return a
            if a.get("path") != state["last"] or len(own) + len(pool) <= 1:
                state["last"] = a.get("path")
                return a
        return None

    for i, (v, tl) in enumerate(zip(views, timeline)):
        s0, s1 = tl["start"], tl["end"]
        typ = v.get("type")
        turns = [(w, s0 + a, s0 + b) for w, a, b in v.get("_turns") or []]

        def add(a, b, kind, **kw):
            if b - a > 0.05:
                shots.append({"start": a, "end": b, "kind": kind, "scene": i, "s0": s0, "s1": s1, **kw})
        if typ == "outro" and characters:
            add(s0, s1, "wide")
            continue
        if typ == "chapter":
            cut = s0
            if v.get("cta") and turns and characters:
                cut = turns[0][2] + 0.15
                add(s0, cut, "wide")
            add(cut, s1, "chapter", asset=asset_for(min(i + 1, len(views) - 1), 0))
            continue
        specials = []
        if characters and typ == "title" and len(turns) >= 2:
            specials.append((turns[1][1] - 0.1, min(turns[1][1] + 3.0, s1), "wide"))
        if characters and typ != "title":
            for w, a, b in turns:
                if w == "guest" and b - a <= 3.2:
                    if reacts % 2 == 0:
                        specials.append((max(s0, a - 0.05), min(s1, b + 0.12), "close"))
                    reacts += 1
        specials = [sp for sp in specials if sp[1] - sp[0] >= 1.0]
        free = _subtract(s0, s1, [(a, b) for a, b, _ in specials])
        for a, b, kind in specials:
            add(a, b, kind)
        main = _main_kind(v)
        k = 0
        total_free = sum(b - a for a, b in free)
        for n, (a, b) in enumerate(free):
            if main != "broll":
                if n == 0 and total_free > 12 and b - a > 7:
                    add(a, a + 3.5, "broll", asset=asset_for(i, k), overlay="headline", move=i % 4)
                    k += 1
                    a += 3.5
                add(a, b, main, asset=asset_for(i, 0))
                continue
            pieces = max(1, round((b - a) / SHOT_LEN))
            step = (b - a) / pieces
            for p in range(pieces):
                overlay = {"title": "title", "explain": "explain", "payoff": "payoff"}.get(typ, "headline")
                if typ == "title" and (n or p):
                    overlay = None
                elif overlay == "headline" and (n or p):
                    overlay = None
                add(a + p * step, a + (p + 1) * step, "broll", asset=asset_for(i, k), overlay=overlay,
                    move=(i + k) % 4)
                k += 1
    shots.sort(key=lambda s: s["start"])
    # close tiny gaps and overlaps so exactly one shot is live at any time
    for a, b in zip(shots, shots[1:]):
        a["end"] = b["start"]
    if shots:
        shots[0]["start"] = 0.0
        shots[-1]["end"] = max(shots[-1]["end"], timeline[-1]["end"])
    return shots


def sfx_events(shots: list[dict]) -> list[tuple[float, str]]:
    """Sound design: whoosh into graphics and chapter cards, a riser and hit for each chapter."""
    out: list[tuple[float, str]] = [(0.25, "impact")]
    prev = None
    for s in shots:
        k = s["kind"]
        if k == "chapter":
            out += [(max(0.0, s["start"] - 1.3), "riser"), (s["start"], "impact"), (s["start"] - 0.1, "whoosh")]
        elif k in ("panel", "timeline", "chart", "stat", "wide") and prev != k:
            out.append((max(0.0, s["start"] - 0.2), "whoosh"))
        elif k == "quote":
            out += [(max(0.0, s["start"] - 0.2), "whoosh"), (s["start"] + 0.7, "swish")]
        prev = k
    return out


# ----------------------------------------------------------------------------- rendering
class _Clip:
    """Sequential frames of a stock video, scaled and cropped to the frame by ffmpeg."""

    def __init__(self, path: str, W: int, H: int, fps: int):
        self.args = [ffmpeg_exe(), "-loglevel", "error", "-stream_loop", "-1", "-i", path, "-an",
                     "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={fps}",
                     "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
        self.W, self.H, self.n = W, H, 0
        self.proc = subprocess.Popen(self.args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.last: Image.Image | None = None

    def frame(self, index: int) -> Image.Image | None:
        size = self.W * self.H * 3
        while self.n <= index:
            data = self.proc.stdout.read(size) if self.proc.stdout else b""
            if len(data) < size:
                return self.last
            self.last = Image.frombytes("RGB", (self.W, self.H), data)
            self.n += 1
        return self.last

    def close(self):
        try:
            self.proc.kill()
            self.proc.wait(timeout=5)
        except Exception:
            pass


class DocuRenderer:
    def __init__(self, cfg, script, views: list[dict], shots: list[dict], seed: int = 0):
        self.cfg = cfg
        self.views = views
        self.base = Renderer(cfg, script.theme, seed=seed, scenes=script.scenes, meta=source_meta(script))
        b = self.base
        self.W, self.H, self.fps, self.fonts = b.W, b.H, b.fps, b.fonts
        self.accent, self.accent2 = b.accent, b.accent2
        self.shots = shots
        self.starts = [s["start"] for s in shots]
        self.meta = source_meta(script)
        self.chapters = [clean(c) for c in (script.extra or {}).get("chapters") or []]
        self._prep: dict[tuple, Image.Image] = {}
        self._clips: dict[int, _Clip] = {}
        self._paper: dict[int, tuple] = {}
        self._fits: dict[tuple, tuple] = {}
        self.shades = {k: self._shade(k) for k in ("base", "top", "left", "center")}

    # ------------------------------------------------------------------ helpers
    def _shade(self, kind: str) -> Image.Image:
        """Multiply mask: vignette and bottom fade for captions, plus a darker area behind text."""
        W, H = self.W, self.H
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        r = np.sqrt(((xx - W / 2) / (W * 0.62)) ** 2 + ((yy - H / 2) / (H * 0.62)) ** 2)
        m = 1 - 0.45 * np.clip(r - 0.55, 0, 1) ** 1.2
        m *= 1 - 0.55 * np.clip((yy - H * 0.72) / (H * 0.28), 0, 1) ** 1.5
        if kind == "top":
            m *= 1 - 0.6 * np.clip(1 - yy / (H * 0.42), 0, 1) ** 1.3
        elif kind == "left":
            m *= 1 - 0.68 * np.clip(1 - xx / (W * 0.62), 0, 1) ** 1.1
        elif kind == "center":
            m *= 0.55
        g = np.clip(m * 255, 0, 255).astype(np.uint8)
        return Image.fromarray(g, "L").convert("RGB")

    def _cover(self, asset: dict, scale: float = 1.15, blur: float = 0.0, dim: float = 1.0,
               side: str = "center") -> Image.Image:
        key = (asset["path"], scale, blur, dim, side)
        if key not in self._prep:
            if len(self._prep) > 24:
                self._prep.pop(next(iter(self._prep)))
            W, H = int(self.W * scale), int(self.H * scale)
            img = Image.open(asset["path"]).convert("RGB")
            if asset.get("kind") == "graphic" and not blur:
                img = self._graphic_card(img, W, H, side)
            else:
                k = max(W / img.width, H / img.height)
                img = img.resize((max(W, int(img.width * k + 1)), max(H, int(img.height * k + 1))), Image.LANCZOS)
                x0, y0 = (img.width - W) // 2, (img.height - H) // 2
                img = img.crop((x0, y0, x0 + W, y0 + H))
            if blur:
                small = img.resize((W // 6, H // 6), Image.BILINEAR).filter(ImageFilter.GaussianBlur(blur / 6))
                img = small.resize((W, H), Image.BILINEAR)
            if dim != 1.0:
                img = img.point(lambda v: int(v * dim))
            self._prep[key] = img
        return self._prep[key]

    def _graphic_card(self, img: Image.Image, W: int, H: int, side: str = "center") -> Image.Image:
        """A diagram or chart from an article, on a white card over a dark blurred copy of itself."""
        bg = img.resize((W // 8, H // 8), Image.BILINEAR).filter(ImageFilter.GaussianBlur(6)).resize((W, H))
        bg = bg.point(lambda v: int(v * 0.22))
        k = min(W * (0.42 if side == "right" else 0.62) / img.width, H * (0.5 if side == "right" else 0.66) / img.height)
        pic = img.resize((int(img.width * k), int(img.height * k)), Image.LANCZOS)
        pad = 36
        card = Image.new("RGB", (pic.width + 2 * pad, pic.height + 2 * pad), (255, 255, 255))
        card.paste(pic, (pad, pad))
        cx = W * 0.71 if side == "right" else W / 2          # right: leaves room for bullet points
        cy = H * 0.58 if side == "right" else H * 0.47                # right: below the headline
        x, y = int(cx - card.width / 2), int(cy - card.height / 2)
        shadow = Image.new("L", (W, H), 0)
        ImageDraw.Draw(shadow).rounded_rectangle([x + 10, y + 16, x + card.width + 10, y + card.height + 16], 18, fill=150)
        shadow = shadow.filter(ImageFilter.GaussianBlur(18))
        bg.paste((0, 0, 0), (0, 0), shadow)
        mask = Image.new("L", card.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, card.width - 1, card.height - 1], 16, fill=255)
        bg.paste(card, (x, y), mask)
        return bg

    def _kenburns(self, img: Image.Image, p: float, move: int, scale: float = 1.15) -> Image.Image:
        BW, BH = img.size
        e = ease_in_out(p)
        if move == 0:      # push in
            z, cx, cy = 1.0 + 0.11 * e, 0.5, 0.5
        elif move == 1:    # pull out
            z, cx, cy = 1.11 - 0.11 * e, 0.5, 0.48
        elif move == 2:    # pan right
            z, cx, cy = 1.08, 0.38 + 0.24 * e, 0.5
        else:              # pan left and up
            z, cx, cy = 1.08, 0.62 - 0.24 * e, 0.54 - 0.06 * e
        w, h = BW / z, BH / z
        x0 = min(max(cx * BW - w / 2, 0), BW - w)
        y0 = min(max(cy * BH - h / 2, 0), BH - h)
        return img.resize((self.W, self.H), Image.BILINEAR, box=(x0, y0, x0 + w, y0 + h))

    def _pan(self, img: Image.Image, p: float, move: int) -> Image.Image:
        e = ease_in_out(p)
        fx = e if move == 2 else 1 - e
        x = int(round((img.width - self.W) * fx))
        y = int(round((img.height - self.H) * (0.5 if move == 2 else 0.6 - 0.2 * e)))
        return img.crop((x, y, x + self.W, y + self.H))

    def _bg(self, shot: dict, t: float, dim: float = 0.42) -> Image.Image:
        a = shot.get("asset")
        if a and a.get("kind") != "video":
            img = self._cover(a, 1.08, blur=22, dim=dim)
            return self._kenburns(img, clamp01((t - shot["start"]) / max(shot["end"] - shot["start"], 0.1)), 0, 1.08)
        return self.base.background(t).copy()

    def _tracked(self, d, x, y, text, font, fill, tr=3.0, anchor="lm"):
        return self.base._tracked(d, x, y, text, font, fill, tr)

    def _fit(self, text, kind, size, max_w, min_size, max_lines=1):
        key = (text, kind, size, max_w, min_size, max_lines)
        if key not in self._fits:
            if len(self._fits) > 4000:
                self._fits.clear()
            self._fits[key] = self.base._fit(text, kind, size, max_w, min_size, max_lines)
        return self._fits[key]

    def _wrap(self, text, font, max_w):
        key = (text, font.size, id(font), max_w)
        if key not in self._fits:
            self._fits[key] = wrap_text(text, font, max_w)
        return self._fits[key]

    # ------------------------------------------------------------------ shot kinds
    def _broll(self, shot, scene, t):
        a = shot.get("asset")
        p = clamp01((t - shot["start"]) / max(shot["end"] - shot["start"], 0.1))
        if a is None:
            img = self.base.background(t).copy()
        elif a["kind"] == "video":
            clip = self._clips.get(id(shot))
            if clip is None:
                for k in list(self._clips):              # one decoder at a time
                    self._clips.pop(k).close()
                clip = self._clips[id(shot)] = _Clip(a["path"], self.W, self.H, self.fps)
            img = clip.frame(int((t - shot["start"]) * self.fps)) or self.base.background(t).copy()
        else:
            move = shot.get("move", 0)
            side = "right" if shot.get("overlay") == "explain" else "center"
            if move >= 2:                       # pans: a pixel window sliding over a larger copy, no resize
                img = self._pan(self._cover(a, 1.24, side=side), p, move)
            else:
                img = self._kenburns(self._cover(a, side=side), p, move)
        overlay = shot.get("overlay")
        shade = {"explain": "left", "headline": "top", "title": "center", "payoff": "center"}.get(overlay, "base")
        img = ImageChops.multiply(img, self.shades[shade])
        d = ImageDraw.Draw(img)
        lt = t - shot["start"]
        if overlay == "title" or overlay == "payoff":
            self._center_text(d, scene, lt, big=overlay == "title")
        elif overlay == "headline":
            self._headline(d, scene, lt)
        elif overlay == "explain":
            self._headline(d, scene, lt, small=True, max_w=800)
            self._bullets(d, scene, t - shot["s0"])
        if a is not None:
            self._credit(d, a)
        return img

    def _headline(self, d, scene, lt, small=False, max_w: int = 980):
        text = clean(scene.get("on_screen")).upper()
        if not text:
            return
        a = ease_out((lt - 0.15) / 0.5)
        kick = clean(scene.get("kicker") or scene.get("chapter") or "").upper()[:34]
        x, y = 96, 110
        if kick:
            d.rectangle([x, y - 14, x + 6, y + 14], fill=lerp_rgb((0, 0, 0), self.accent, a))
            self._tracked(d, x + 22, y, kick, self.fonts.get("mono_bold", 28), lerp_rgb((0, 0, 0), self.accent, a), 4)
        font, lines = self._fit(text, "headline", 58 if small else 72, max_w, 34, max_lines=2)
        for i, line in enumerate(lines):
            d.text((x, y + 44 + i * font.size * 1.06 + (1 - a) * 18), line, font=font, anchor="la",
                   fill=lerp_rgb((0, 0, 0), INK, a))

    def _center_text(self, d, scene, lt, big=True):
        text = clean(scene.get("on_screen")).upper()
        a = ease_out((lt - 0.2) / 0.7)
        if not text or a <= 0:
            return
        font, lines = self._fit(text, "headline", 124 if big else 96, int(self.W * 0.8), 56, max_lines=3)
        lh = font.size * 1.04
        y0 = self.H / 2 - (len(lines) - 1) * lh / 2 - 20
        for i, line in enumerate(lines):
            col = AMBER if i == len(lines) - 1 and len(lines) > 1 else INK
            d.text((self.W / 2, y0 + i * lh + (1 - a) * 26), line, font=font, anchor="mm",
                   fill=lerp_rgb((0, 0, 0), col, a))
        w = int(220 * ease_out((lt - 0.5) / 0.6))
        if w > 0:
            yb = y0 + len(lines) * lh - lh / 2 + 22
            d.rectangle([self.W / 2 - w, yb, self.W / 2 + w, yb + 5], fill=self.accent)

    def _bullets(self, d, scene, scene_t):
        items = [clean(b) for b in scene.get("bullets") or []][:4]
        if not items:
            return
        times = self.base._reveal_times(scene, len(items), 99)
        f, fn = self.fonts.get("bold", 46), self.fonts.get("mono_bold", 34)
        y = 330
        for i, item in enumerate(items):
            a = ease_out((scene_t - times[i]) / 0.45)
            if a <= 0:
                break
            lines = self._wrap(item, f, 760)[:2]
            x = 96 - (1 - a) * 40
            d.text((x, y), f"{i + 1:02d}", font=fn, fill=lerp_rgb((0, 0, 0), self.accent, a), anchor="la")
            for j, line in enumerate(lines):
                d.text((x + 86, y - 4 + j * 56), line, font=f, fill=lerp_rgb((0, 0, 0), INK, a), anchor="la")
            y += 56 * len(lines) + 46

    def _credit(self, d, asset):
        text = clean(asset.get("credit"))[:90]
        if text:
            f = self.fonts.get("mono", 18)
            d.text((self.W - 40, self.H - 26), text, font=f, fill=(150, 156, 162), anchor="rs")

    def _stat(self, shot, scene, t):
        img = self._bg(shot, t, 0.35)
        d = ImageDraw.Draw(img)
        lt = t - shot["start"]
        text = clean(scene.get("on_screen"))
        m = _NUMBER.search(text)
        a = ease_out(lt / 0.5)
        cy = self.H * 0.44
        if m:
            raw = m.group(0).strip()
            num = re.search(r"\d[\d,]*(?:\.\d+)?", raw).group(0)
            dec = len(num.split(".")[1]) if "." in num else 0
            year = re.fullmatch(r"(1[89]|20)\d\d", num) is not None      # a year lands, it does not count up
            val = float(num.replace(",", "")) * (1.0 if year else ease_out((lt - 0.2) / 1.3))
            shown = raw.replace(num, f"{val:,.{dec}f}" if "," in num else f"{val:.{dec}f}")
            font, _ = self._fit(raw, "headline", 230, int(self.W * 0.8), 80)
            d.text((self.W / 2, cy), shown, font=font, anchor="mm", fill=lerp_rgb((0, 0, 0), AMBER, a))
            rest = text if len(text) > len(raw) + 3 else clean(scene.get("kicker"))
        else:
            rest = text
        if rest:
            font, lines = self._fit(rest.upper(), "headline", 64, int(self.W * 0.75), 36, max_lines=2)
            b = ease_out((lt - 0.9) / 0.5)
            for i, line in enumerate(lines):
                d.text((self.W / 2, cy + (150 if m else 0) + i * font.size * 1.1), line, font=font, anchor="mm",
                       fill=lerp_rgb((0, 0, 0), INK, b))
        return ImageChops.multiply(img, self.shades["base"])

    def _quote(self, shot, scene, t):
        img = self._bg(shot, t, 0.3)
        lt = t - shot["start"]
        key = id(shot)
        if key not in self._paper:
            fq, fh, fm = self.fonts.get("regular", 46), self.fonts.get("bold", 40), self.fonts.get("mono_bold", 24)
            pw = 1300
            lines = wrap_text(clean(scene.get("quote")), fq, pw - 160)[:7]
            head = wrap_text(clean(self.meta.get("topic_title")), fh, pw - 160)[:2]
            ph = 150 + len(head) * 50 + 40 + len(lines) * 66 + 70
            self._paper[key] = (pw, ph, lines, head, fq, fh, fm)
        pw, ph, lines, head, fq, fh, fm = self._paper[key]
        paper = Image.new("RGB", (pw, ph), PAPER)
        d = ImageDraw.Draw(paper)
        dom = clean(self.meta.get("domain") or self.meta.get("source")).upper()
        d.text((80, 70), dom, font=fm, fill=(120, 110, 100), anchor="lm")
        d.line([(80, 100), (pw - 80, 100)], fill=(200, 192, 178), width=2)
        y = 130
        for line in head:
            d.text((80, y), line, font=fh, fill=PAPER_INK, anchor="la")
            y += 50
        y += 40
        total = sum(fq.getlength(l) for l in lines) or 1
        done = total * ease_in_out((lt - 0.7) / max(1.2 + 0.25 * len(lines), 0.5))
        for line in lines:
            w = fq.getlength(line)
            take = max(0.0, min(w, done))
            if take > 0:
                d.rectangle([76, y + 4, 84 + take, y + 58], fill=HIGHLIGHT)
            done -= w
            d.text((80, y), line, font=fq, fill=PAPER_INK, anchor="la")
            y += 66
        z = 1.0 + 0.04 * ease_in_out(lt / max(shot["end"] - shot["start"], 1))
        paper = paper.resize((int(pw * z), int(ph * z)), Image.BILINEAR)
        x, y0 = (self.W - paper.width) // 2, int((self.H - paper.height) / 2 - 30)
        a = ease_out(lt / 0.4)
        y0 += int((1 - a) * 60)
        sh = Image.new("L", img.size, 0)
        ImageDraw.Draw(sh).rectangle([x + 14, y0 + 22, x + paper.width + 14, y0 + paper.height + 22], fill=170)
        img.paste((0, 0, 0), (0, 0), sh.filter(ImageFilter.GaussianBlur(20)))
        img.paste(paper, (x, y0))
        return ImageChops.multiply(img, self.shades["base"])

    def _timeline(self, shot, scene, t):
        img = self._bg(shot, t, 0.25)
        d = ImageDraw.Draw(img)
        lt, dur = t - shot["start"], shot["end"] - shot["start"]
        ev = (scene.get("timeline") or [])[:5]
        self._headline(d, scene, lt, small=True)
        x0, x1, y = 330, self.W - 330, self.H * 0.58
        n = len(ev)
        span = max(dur * 0.75 - 0.6, 1.0)
        line_p = ease_out(lt / 0.8)
        d.line([(x0, y), (x0 + (x1 - x0) * line_p, y)], fill=(120, 128, 136), width=4)
        fy, fl = self.fonts.get("headline", 66), self.fonts.get("bold", 32)
        col_w = (x1 - x0) / max(n - 1, 1)
        for i, e in enumerate(ev):
            at = 0.6 + i * span / n
            a = ease_out((lt - at) / 0.45)
            if a <= 0:
                continue
            cx = x0 + i * col_w if n > 1 else (x0 + x1) / 2
            now = i == max(j for j in range(n) if lt >= 0.6 + j * span / n)
            r = 16 if now else 11
            d.ellipse([cx - r, y - r, cx + r, y + r], fill=lerp_rgb((0, 0, 0), self.accent if now else INK, a))
            d.text((cx, y - 50 - (1 - a) * 20), clean(e.get("when")), font=fy, anchor="ms",
                   fill=lerp_rgb((0, 0, 0), AMBER if now else INK, a))
            for j, line in enumerate(self._wrap(clean(e.get("label")), fl, int(min(col_w, 420) - 20))[:3]):
                d.text((cx, y + 52 + j * 40), line, font=fl, anchor="ma", fill=lerp_rgb((0, 0, 0), INK, a))
        return ImageChops.multiply(img, self.shades["base"])

    def _chart(self, shot, scene, t):
        img = self._bg(shot, t, 0.25)
        d = ImageDraw.Draw(img)
        lt = t - shot["start"]
        ch = scene.get("chart") or {}
        bars = (ch.get("bars") or [])[:5]
        self._headline(d, scene, lt, small=True)
        vmax = max((float(b["value"]) for b in bars), default=1) or 1
        x0, width = 520, self.W - 520 - 300
        n = len(bars)
        bh = min(90, (self.H * 0.55) / max(n, 1) - 26)
        y = self.H * 0.36
        fl, fv = self.fonts.get("bold", 38), self.fonts.get("mono_bold", 40)
        unit = clean(ch.get("unit"))[:12]
        for i, b in enumerate(bars):
            p = ease_out((lt - 0.4 - 0.25 * i) / 1.0)
            yy = y + i * (bh + 26)
            d.text((x0 - 28, yy + bh / 2), clean(b["label"])[:24], font=fl, anchor="rm", fill=INK)
            w = width * float(b["value"]) / vmax * p
            col = AMBER if i == 0 else lerp_rgb((40, 44, 52), self.accent, 0.85)
            d.rounded_rectangle([x0, yy, x0 + max(w, 2), yy + bh], radius=10, fill=col)
            if p > 0.05:
                v = float(b["value"]) * p
                txt = (f"{v:,.0f}" if float(b["value"]) >= 100 else f"{v:.1f}".rstrip("0").rstrip(".")) + \
                      (f" {unit}" if unit and unit not in ("%",) else unit)
                d.text((x0 + w + 18, yy + bh / 2), txt, font=fv, anchor="lm", fill=INK)
        return ImageChops.multiply(img, self.shades["base"])

    def _panel(self, shot, scene, t, t_local, scene_dur):
        img = self._bg(shot, t, 0.3)
        b = self.base
        layer = b.panel_base.copy()
        b.SCENES.get(scene.get("type"), Renderer._s_explain)(b, layer, ImageDraw.Draw(layer), scene, t_local, scene_dur)
        s = 0.8
        layer = layer.resize((int(layer.width * s), int(layer.height * s)), Image.BILINEAR)
        mask = b.panel_mask.resize(layer.size)
        x, y = (self.W - layer.width) // 2, 316                 # below a two-line headline
        a = ease_out((t - shot["start"]) / 0.45)
        y += int((1 - a) * 40)
        img.paste(layer, (x, y), mask)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([x, y, x + layer.width, y + layer.height], radius=18, outline=b.edge, width=2)
        self._headline(d, scene, t - shot["start"], small=True, max_w=1500)
        return ImageChops.multiply(img, self.shades["base"])

    def _chapter(self, shot, scene, t):
        lt = t - shot["start"]
        img = self._bg(shot, t, 0.32)
        d = ImageDraw.Draw(img)
        k = scene.get("chapter_index") or 1
        n = max(len(self.chapters), k)
        a = ease_out((lt - 0.1) / 0.5)
        lab = f"CHAPTER {k:02d}"
        fm = self.fonts.get("mono_bold", 34)
        self._tracked(d, self.W / 2 - self.base._tracked_len(lab, fm, 8) / 2, self.H * 0.36, lab, fm,
                      lerp_rgb((0, 0, 0), self.accent, a), 8)
        font, lines = self._fit(clean(scene.get("on_screen")).upper(), "headline", 130, int(self.W * 0.8), 60, 2)
        b = ease_out((lt - 0.3) / 0.6)
        for i, line in enumerate(lines):
            d.text((self.W / 2, self.H * 0.47 + i * font.size * 1.05 + (1 - b) * 30), line, font=font, anchor="mm",
                   fill=lerp_rgb((0, 0, 0), INK, b))
        w = int(self.W * 0.3 * ease_out((lt - 0.2) / 0.8))
        yl = self.H * 0.47 + len(lines) * font.size * 1.05
        d.rectangle([self.W / 2 - w, yl, self.W / 2 + w, yl + 4], fill=self.accent)
        for i in range(n):                         # where we are in the video
            cx = self.W / 2 + (i - (n - 1) / 2) * 44
            on = i + 1 == k
            r = 9 if on else 6
            d.ellipse([cx - r, yl + 50 - r, cx + r, yl + 50 + r],
                      fill=self.accent if on else ((150, 150, 160) if i + 1 < k else (70, 74, 82)))
        img = ImageChops.multiply(img, self.shades["base"])
        if lt < 0.14:                              # flash cut into the chapter
            img = Image.blend(img, Image.new("RGB", img.size, (255, 255, 255)), 0.55 * (1 - lt / 0.14))
        return img

    # ------------------------------------------------------------------ frame
    def shot_at(self, t: float) -> int:
        return max(0, bisect.bisect_right(self.starts, t) - 1)

    def _draw(self, shot, t):
        scene = self.views[shot["scene"]]
        t_local, scene_dur = t - shot["s0"], shot["s1"] - shot["s0"]
        k = shot["kind"]
        if k == "broll":
            return self._broll(shot, scene, t)
        if k == "stat":
            return self._stat(shot, scene, t)
        if k == "quote":
            return self._quote(shot, scene, t)
        if k == "timeline":
            return self._timeline(shot, scene, t)
        if k == "chart":
            return self._chart(shot, scene, t)
        if k == "panel":
            return self._panel(shot, scene, t, t_local, scene_dur)
        if k == "chapter":
            return self._chapter(shot, scene, t)
        return self.base.background(t).copy()

    def frame(self, scene: dict, t_local: float, scene_dur: float, t_global: float, progress: float,
              label: str, caption: dict | None, index: int | None = None) -> Image.Image:
        i = self.shot_at(t_global)
        shot = self.shots[i] if self.shots else {"kind": "broll", "start": 0, "end": 1, "s0": 0}
        if shot["kind"] in ("wide", "close"):
            return self.base.frame(scene, t_local, scene_dur, t_global, progress, label, caption, index=index,
                                   shot=shot["kind"])
        img = self._draw(shot, t_global)
        fade = 0.25                                 # short dissolve between shots, except into a chapter flash
        if 0 < i and t_global - shot["start"] < fade and shot["kind"] != "chapter":
            prev = self.shots[i - 1]
            is_video = lambda s: (s.get("asset") or {}).get("kind") == "video"  # noqa: E731
            if prev["kind"] not in ("wide", "close") and not is_video(prev) and not is_video(shot):
                img = Image.blend(self._draw(prev, t_global), img, ease_in_out((t_global - shot["start"]) / fade))
        d = ImageDraw.Draw(img)
        speaker = 1 if caption and caption.get("who") == "guest" else 0
        self._caption(d, caption, speaker)
        mark = clean(self.cfg.brand.channel_name).upper()
        self._tracked(d, self.W - 40 - self.base._tracked_len(mark, self.fonts.get("mono_bold", 20), 4), 46, mark,
                      self.fonts.get("mono_bold", 20), (200, 204, 210), 4)
        return img

    def _caption(self, d, chunk, speaker):
        if not chunk:
            return
        text = " ".join(clean(w) for w, _, _ in chunk["words"])
        font, lines = self._fit(text, "bold", 48, int(self.W * 0.7), 34, max_lines=2)
        y = self.H - 96 - (len(lines) - 1) * font.size * 1.2
        who = self.base.cast[speaker] if len(self.base.cast) > speaker else None
        for i, line in enumerate(lines):
            d.text((self.W / 2, y + i * font.size * 1.2), line, font=font, anchor="mm", fill=(250, 250, 250),
                   stroke_width=4, stroke_fill=(8, 8, 12))
        if who is not None:                         # a small coloured name tag: who is talking
            f = self.fonts.get("mono_bold", 20)
            w = self.base._tracked_len(who.name, f, 3)
            x0 = self.W / 2 - w / 2
            d.rounded_rectangle([x0 - 12, y - 64, x0 + w + 12, y - 36], radius=8, fill=(12, 14, 18))
            self.base._tracked(d, x0, y - 50, who.name, f, who.color, 3)

    def close(self):
        for c in self._clips.values():
            c.close()
        self._clips.clear()
