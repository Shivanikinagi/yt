"""Procedural motion-graphics renderer (Pillow). Produces one 1080x1920 RGB frame per call.

One consistent "field notes" HUD design instead of rotating looks: the header, progress bar, framed
visual panel, caption bar and footer stay put for the whole video, so it reads as one edited piece.
Only the panel contents change between scenes, and they keep moving (slow camera push, orbiting
labels, pulses travelling along connectors, typing, count-ups, ticking checklists) rather than
fading in and sitting still. Light film grain and a vignette take the edge off the flat synthetic
look. No GPU and no assets besides the OFL fonts bundled in assets/fonts. Laid out for 1080x1920.
"""
from __future__ import annotations

import math
import random
import re
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

from .characters import CAST, POSES, Host, pick_cast
from .utils import ROOT

# Themes only pick the accent colours now; layout and neutrals are shared so the channel looks consistent.
THEMES = {
    "neon": dict(accent="#38bdf8", accent2="#4ade80", glow="#0d5c47"),
    "voxel": dict(accent="#4ade80", accent2="#facc15", glow="#1d5a1f"),
    "blueprint": dict(accent="#60a5fa", accent2="#fbbf24", glow="#173a7a"),
    "sunset": dict(accent="#fb923c", accent2="#f472b6", glow="#6b2410"),
    "terminal": dict(accent="#22c55e", accent2="#67e8f9", glow="#0f5130"),
}

INK = (238, 242, 245)
MUTED = (125, 140, 148)
DIM = (62, 76, 84)
BG_TOP = (5, 8, 10)
BG_BOTTOM = (8, 14, 16)
PANEL = (7, 13, 16)
CARD = (13, 23, 28)
LINE = (28, 44, 51)
CAP_BG = (6, 10, 12)
AMBER = (251, 191, 36)

DEFAULT_KICKERS = {"title": "Today in tech", "explain": "The core idea", "diagram": "How it works",
                   "compare": "What changed", "code": "Try it", "fact": "Worth knowing", "payoff": "The takeaway"}
PANEL_LABELS = {"title": "TODAY'S TOPIC", "explain": "THE BREAKDOWN", "diagram": "HOW IT FLOWS",
                "compare": "SIDE BY SIDE", "code": "IN PRACTICE", "fact": "KEY FACT", "payoff": "RECAP"}
CODE_KEYWORDS = {"def", "return", "if", "else", "for", "while", "in", "import", "from", "print", "const", "let",
                 "var", "function", "class", "await", "async", "pip", "npm", "git", "curl", "true", "false",
                 "none", "null", "new", "with", "as", "and", "or", "not", "sudo", "docker", "python"}
_CODE_TOKEN = re.compile(r"(#.*$|//.*$|\"[^\"]*\"?|'[^']*'?|0x[0-9A-Fa-f]+|\d+(?:\.\d+)?|[A-Za-z_][A-Za-z_0-9]*|\s+|.)")
_NUMBER = re.compile(r"[$~]?\d[\d,]*(?:\.\d+)?\s?[%x×+]?")

FONT_KINDS = ("headline", "bold", "regular", "mono", "mono_bold")
FONT_FALLBACK = {"headline": "bold", "mono_bold": "mono", "bold": "regular", "mono": "regular"}

# The bundled display fonts cover Latin only; map the typographic punctuation LLMs like to use.
_PUNCT = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "−": "-", "‘": "'", "’": "'",
                        "“": '"', "”": '"', "…": "...", " ": " ", " ": " "})


def clean(text) -> str:
    return " ".join(str(text or "").translate(_PUNCT).split())


def hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def lerp_rgb(a, b, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(round(lerp(a[i], b[i], t))) for i in range(3))


def clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def ease_out(t: float) -> float:
    t = clamp01(t)
    return 1 - (1 - t) ** 3


def ease_in_out(t: float) -> float:
    t = clamp01(t)
    return 0.5 - 0.5 * math.cos(math.pi * t)


def turn_at(turns, t: float) -> int | None:
    """Index of the dialogue turn at scene-local time t; pauses belong to whoever just spoke."""
    if not turns:
        return None
    for k, (_, _, end) in enumerate(turns):
        if t < end + 0.11:
            return k
    return len(turns) - 1


def speaker_at(turns, t: float) -> str | None:
    k = turn_at(turns, t)
    return None if k is None else turns[k][0]


def subject_from_title(title: str, fallback: str = "Tech") -> str:
    """Pick a short subject for the title scene's core badge, e.g. 'UTF-8' or 'OpenAI'."""
    words = [w.strip(".,:;!?\"'()") for w in clean(title).split()]
    for w in words:
        if w and (any(c.isdigit() for c in w) or sum(c.isupper() for c in w) >= 2):
            return w
    for w in words[1:]:
        if len(w) > 3 and w[0].isupper():
            return w
    return fallback


class Fonts:
    def __init__(self, cfg):
        brand = cfg.brand
        self.paths = {k: self._find(brand.get(f"font_candidates_{k}") or []) for k in FONT_KINDS}
        self.cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}

    @staticmethod
    def _find(candidates) -> str | None:
        for c in candidates:
            p = Path(c)
            if not p.is_absolute():
                p = ROOT / p
            if p.exists():
                return str(p)
        return None

    def get(self, kind: str, size: int):
        key = (kind, size)
        if key not in self.cache:
            path, k = self.paths.get(kind), kind
            while not path and k in FONT_FALLBACK:
                k = FONT_FALLBACK[k]
                path = self.paths.get(k)
            self.cache[key] = ImageFont.truetype(path, size) if path else ImageFont.load_default(size)
        return self.cache[key]


def wrap_text(text: str, font, max_w: int) -> list[str]:
    words = text.split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if font.getlength(trial) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


class Renderer:
    def __init__(self, cfg, theme_name: str, seed: int = 0, scenes: list[dict] | None = None,
                 meta: dict | None = None):
        self.cfg = cfg
        self.W, self.H = int(cfg.video.width), int(cfg.video.height)
        self.fps = int(cfg.video.fps)
        self.name = theme_name if theme_name in THEMES else "neon"
        pal = THEMES[self.name]
        self.accent, self.accent2, self.glow = (hex_to_rgb(pal[k]) for k in ("accent", "accent2", "glow"))
        self.fonts = Fonts(cfg)
        self.rng = random.Random(seed)
        self.scenes = list(scenes or [])
        self.meta = dict(meta or {})
        self.intro = max(float(cfg.video.get("transition_seconds", 0.4)), 0.2)
        self.margin = 64
        self.content_w = self.W - 2 * self.margin
        cast = [k for k in (self.meta.get("cast") or []) if k in CAST] or pick_cast(cfg, seed)
        scale = 1.12 if len(cast) >= 2 else 1.3
        self.cast = [Host(k, self.accent, seed + i, scale=scale) for i, k in enumerate(cast[:2])]
        self.host = self.cast[0] if self.cast else None
        if len(self.cast) == 2:
            # two characters below the panel; the caption bubble points at whoever is talking.
            # The right one stays clear of the Shorts like/comment buttons.
            self.panel_box = (self.margin, 470, self.W - self.margin, 1236)
            self.cap_box = (self.margin, 1258, self.W - self.margin, 1446)
            w = self.cast[0].size[0]
            self.cast_pos = [(30, 1470), (952 - w, 1470)]
        elif self.host:
            # host stands bottom-left in front of the panel; captions become their speech bubble
            self.panel_box = (self.margin, 470, self.W - self.margin, 1250)
            self.cap_box = (436, 1296, self.W - self.margin, 1536)
            self.cast_pos = [(14, 1712 - self.host.size[1])]
        else:
            self.panel_box = (self.margin, 470, self.W - self.margin, 1410)
            self.cap_box = (self.margin, 1446, self.W - self.margin, 1606)
        # Cinematic mix: guest lines, the opening and the sign-off are filmed as close-ups and wide
        # shots of the characters; the host's explanations cut to the info panel.
        self.cinematic = len(self.cast) == 2 and bool(cfg.video.get("cinematic", True))
        if self.cinematic:
            keys = [h.key for h in self.cast]
            self.close = [Host(k, self.accent, seed + i, scale=3.3, cache_size=16, tall=True) for i, k in enumerate(keys)]
            self.wide = [Host(k, self.accent, seed + i, scale=1.9, cache_size=40) for i, k in enumerate(keys)]
            self.bokeh = self._bokeh(seed)
        self.edge = lerp_rgb(LINE, self.accent, 0.45)
        self.base = self._base()
        self.glows = [(self.rng.uniform(0, 6.28), 0.35, 0.80, 0.78, 0.50, 0.30, tuple(int(v * 0.5) for v in self.glow)),
                      (self.rng.uniform(0, 6.28), 0.25, 0.15, 0.22, 0.40, 0.18, tuple(int(v * 0.10) for v in self.accent))]
        self._bg_cache: tuple[int | None, Image.Image | None] = (None, None)
        self._sprites: dict[tuple, Image.Image] = {}
        self._head_cache: dict[str, tuple] = {}
        self.panel_base, self.panel_mask = self._panel_base()
        self.grain = self._grain_frames(seed)
        self.subject = clean(self.meta.get("subject")) or subject_from_title(
            self.meta.get("topic_title", ""), clean(self.meta.get("category")) or "Tech")

    # ------------------------------------------------------------------ static layers
    def _base(self) -> Image.Image:
        t = np.linspace(0, 1, self.H, dtype=np.float32)[:, None, None]
        col = np.array(BG_TOP, np.float32) * (1 - t) + np.array(BG_BOTTOM, np.float32) * t
        arr = np.repeat(col, self.W, axis=1)
        yy, xx = np.mgrid[0:self.H, 0:self.W].astype(np.float32)
        r = np.sqrt(((xx - self.W / 2) / (self.W * 0.62)) ** 2 + ((yy - self.H / 2) / (self.H * 0.58)) ** 2)
        arr *= (1 - 0.55 * np.clip(r - 0.45, 0, 1) ** 1.4)[..., None]
        img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")
        d = ImageDraw.Draw(img)
        for y in range(36, self.H, 54):            # faint dot grid
            for x in range(36, self.W, 54):
                d.point((x, y), fill=(20, 30, 35))
        return img

    def _panel_base(self) -> tuple[Image.Image, Image.Image]:
        x0, y0, x1, y1 = self.panel_box
        pw, ph = x1 - x0, y1 - y0
        img = Image.new("RGB", (pw, ph), PANEL)
        d = ImageDraw.Draw(img)
        for x in range(0, pw, 48):
            d.line([(x, 0), (x, ph)], fill=(10, 18, 22))
        for y in range(0, ph, 48):
            d.line([(0, y), (pw, y)], fill=(10, 18, 22))
        self._screen(img, self._glow_sprite(int(pw * 0.55), self.glow, 0.45), pw / 2, ph * 0.45)
        mask = Image.new("L", (pw, ph), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, pw - 1, ph - 1], radius=20, fill=255)
        return img, mask

    GRAIN_BIAS = 8

    def _grain_frames(self, seed: int) -> list[Image.Image]:
        """Monochrome noise stored around GRAIN_BIAS so one ImageChops.add(offset=-bias) applies it."""
        rng = np.random.default_rng(seed)
        frames = []
        for _ in range(6):
            n = rng.normal(self.GRAIN_BIAS, 2.6, (self.H, self.W))
            frames.append(Image.fromarray(np.clip(np.rint(n), 0, 2 * self.GRAIN_BIAS).astype(np.uint8), "L").convert("RGB"))
        return frames

    def _glow_sprite(self, radius: int, color, strength: float) -> Image.Image:
        key = (radius, color, strength)
        if key not in self._sprites:
            r = max(int(radius), 2)
            yy, xx = np.mgrid[-r:r, -r:r].astype(np.float32)
            a = np.clip(1 - np.sqrt(xx * xx + yy * yy) / r, 0, 1) ** 2 * strength
            arr = a[..., None] * np.array(color, np.float32)[None, None, :]
            self._sprites[key] = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")
        return self._sprites[key]

    @staticmethod
    def _screen(layer: Image.Image, sprite: Image.Image, cx: float, cy: float):
        w, h = sprite.size
        x0, y0 = int(cx - w / 2), int(cy - h / 2)
        box = (max(0, x0), max(0, y0), min(layer.width, x0 + w), min(layer.height, y0 + h))
        if box[0] >= box[2] or box[1] >= box[3]:
            return
        spr = sprite.crop((box[0] - x0, box[1] - y0, box[2] - x0, box[3] - y0))
        layer.paste(ImageChops.screen(layer.crop(box), spr), box[:2])

    # ------------------------------------------------------------------ cinematic shots
    def _bokeh(self, seed: int) -> Image.Image:
        """Out-of-focus night city: lit windows and soft light discs, slightly larger than the frame for drift."""
        rng = random.Random(seed + 99)
        w, h = (self.W + 120) // 2, (self.H + 120) // 2
        t = np.linspace(0, 1, h, dtype=np.float32)[:, None, None]
        col = np.array((6, 9, 24), np.float32) * (1 - t) + np.array((18, 14, 28), np.float32) * t
        base = Image.fromarray(np.repeat(col, w, axis=1).astype(np.uint8), "RGB")
        warm, cool = ((255, 196, 120), (255, 160, 90), (255, 225, 160)), ((130, 190, 255), (90, 130, 255))
        windows = Image.new("RGB", (w, h))
        d = ImageDraw.Draw(windows)
        for _ in range(10):
            bx, bw, top = rng.randint(-40, w), rng.randint(50, 140), rng.randint(int(h * 0.02), int(h * 0.45))
            for wy in range(top, int(h * 0.8), 20):
                for wx in range(bx, bx + bw, 15):
                    if rng.random() < 0.3:
                        c = rng.choice(warm + cool)
                        d.rectangle([wx, wy, wx + 6, wy + 10], fill=tuple(int(v * 0.45) for v in c))
        discs = Image.new("RGB", (w, h))
        d = ImageDraw.Draw(discs)
        for _ in range(42):
            r = rng.uniform(10, 36)
            x, y = rng.uniform(0, w), rng.uniform(0, h * 0.75)
            c = rng.choice(warm + cool)
            d.ellipse([x - r, y - r, x + r, y + r], fill=tuple(int(v * rng.uniform(0.25, 0.55)) for v in c))
        img = ImageChops.add(base, windows.filter(ImageFilter.GaussianBlur(6)))
        img = ImageChops.add(img, discs.filter(ImageFilter.GaussianBlur(2.5)))
        arr = np.asarray(img.resize((self.W + 120, self.H + 120), Image.BILINEAR), np.float32)
        yy, xx = np.mgrid[0:arr.shape[0], 0:arr.shape[1]].astype(np.float32)
        r = np.sqrt(((xx - arr.shape[1] / 2) / (arr.shape[1] * 0.6)) ** 2 + ((yy - arr.shape[0] / 2) / (arr.shape[0] * 0.55)) ** 2)
        arr *= (1 - 0.6 * np.clip(r - 0.4, 0, 1) ** 1.3)[..., None]
        return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")

    def _bokeh_frame(self, t: float) -> Image.Image:
        dx = 60 + 50 * math.sin(t * 0.09)
        dy = 60 + 40 * math.cos(t * 0.07)
        return self.bokeh.crop((int(dx), int(dy), int(dx) + self.W, int(dy) + self.H))

    def _shot(self, scene: dict, t: float, who: str) -> str:
        if not self.cinematic:
            return "panel"
        kind, turns = scene.get("type"), scene.get("_turns") or []
        if kind == "title":
            return "wide" if t < 2.4 else "close"
        if kind == "payoff":
            done = bool(turns) and t > turns[-1][2] + 0.2
            return "wide" if done or who == "guest" else "close"
        return "close" if who == "guest" else "panel"

    def _subtitle(self, draw, chunk: dict | None, y: float = 300):
        if not chunk:
            return
        text = " ".join(clean(w) for w, _, _ in chunk["words"])
        font, lines = self._fit(text, "bold", 52, self.content_w - 120, 38, max_lines=2)
        for i, line in enumerate(lines):
            draw.text((self.W / 2, y + i * font.size * 1.22), line, font=font, fill=(250, 250, 250), anchor="mm",
                      stroke_width=4, stroke_fill=(8, 8, 12))

    def _progress(self, draw, progress: float):
        m = self.margin
        draw.rectangle([m, 124, self.W - m, 128], fill=(40, 44, 56))
        draw.rectangle([m, 124, m + int(self.content_w * clamp01(progress)), 128], fill=self.accent)

    @staticmethod
    def _expression(text: str, talking_pose: str = "idle") -> str:
        text = (text or "").strip()
        if text.endswith("?"):
            return "think"
        if text.endswith("!"):
            return "wow"
        return talking_pose

    def _cine_close(self, scene, t, caption, speaker, line):
        img = self._bokeh_frame(t)
        mouth = self.mouth_level(caption, t)
        host = self.close[speaker]
        sprite = host.sprite(t + speaker * 1.3, self._expression(line), mouth, look=(0.7, 0.05))
        if speaker == 1:
            sprite = ImageOps.mirror(sprite)
        face_x = 440 if speaker == 0 else self.W - 440
        side = 1 if speaker == 0 else -1                # which way the speaker faces
        x0 = face_x - sprite.width / 2
        y0 = self.H - sprite.height + 10 + 5 * math.sin(t * 1.8) + (3 * math.sin(t * 9) if mouth else 0)
        face_y = y0 + sprite.height * 160 / host.h
        self._screen(img, self._glow_sprite(520, (255, 150, 70), 0.20), face_x + side * 240, face_y + 200)  # screen light
        img.paste(sprite, (int(x0), int(y0)), sprite)
        self._screen(img, self._glow_sprite(420, (90, 130, 255), 0.12), face_x - side * 320, face_y - 260)  # cool rim
        back = self.close[1 - speaker].back()
        head_x = self.W + 70 if speaker == 0 else -70
        img.paste(back, (int(head_x - back.width / 2), int(1640 - back.height * 160 / self.close[1 - speaker].h)), back)
        draw = ImageDraw.Draw(img)
        self._subtitle(draw, caption)
        return img

    def _cine_wide(self, scene, t, t_local, caption, speaker, index):
        img = self._bokeh_frame(t)
        kind = scene.get("type")
        mouth = self.mouth_level(caption, t)
        for i, host in enumerate(self.wide):
            talking = i == speaker
            pose = "wave" if kind == "payoff" or (kind == "title" and talking) else "idle"
            sprite = host.sprite(t + i * 1.3, pose, mouth if talking else 0, look=(1.0, 0.1))
            if i == 1:
                sprite = ImageOps.mirror(sprite)
            x = 6 if i == 0 else self.W - 6 - sprite.width
            y = self.H - sprite.height + 36 + 4 * math.sin(t * 2.2 + i * 1.7) + (2 * math.sin(t * 9) if talking and mouth else 0)
            if talking:
                self._screen(img, self._glow_sprite(320, host.color, 0.28), x + sprite.width / 2, y + sprite.height * 0.45)
            img.paste(sprite, (int(x), int(y)), sprite)
        draw = ImageDraw.Draw(img)
        self._kicker(draw, index, scene, t_local, y=560)
        self._headline(draw, scene, t_local, y=600)
        if kind == "payoff":
            a = ease_out((t_local - 0.4) / 0.5)
            fh = self.fonts.get("mono_bold", 30)
            handle = clean(self.cfg.brand.handle).upper()
            self._tracked(draw, self.margin, 900, handle, fh, lerp_rgb((10, 12, 24), self.accent, a), 3)
        else:
            domain = clean(self.meta.get("domain")).upper()
            if domain:
                self._tracked(draw, self.margin, 900, f"SOURCE: {domain}"[:40], self.fonts.get("mono", 24), MUTED, 2)
        self._subtitle(draw, caption)
        return img

    # ------------------------------------------------------------------ background
    def background(self, t: float) -> Image.Image:
        key = int(t * 15)              # glows drift slowly; recomputing every other frame is invisible
        if self._bg_cache[0] == key:
            return self._bg_cache[1]
        sw, sh = self.W // 8, self.H // 8
        layer = Image.new("RGB", (sw, sh))
        d = ImageDraw.Draw(layer)
        for phase, speed, cx, cy, rx, ry, col in self.glows:
            x = sw * (cx + 0.07 * math.sin(phase + t * speed))
            y = sh * (cy + 0.04 * math.cos(phase * 1.7 + t * speed * 0.8))
            d.ellipse([x - rx * sw, y - ry * sh, x + rx * sw, y + ry * sh], fill=col)
        layer = layer.filter(ImageFilter.GaussianBlur(14)).resize((self.W, self.H), Image.BILINEAR)
        img = ImageChops.add(self.base, layer)
        self._bg_cache = (key, img)
        return img

    # ------------------------------------------------------------------ text helpers
    def _tracked(self, draw, x, y, text, font, fill, tracking=2.0):
        for ch in text:
            draw.text((x, y), ch, font=font, fill=fill, anchor="lm")
            x += font.getlength(ch) + tracking
        return x

    @staticmethod
    def _tracked_len(text, font, tracking=2.0):
        return sum(font.getlength(c) for c in text) + tracking * len(text)

    def _fit(self, text, kind, size, max_w, min_size, max_lines=1):
        while True:
            font = self.fonts.get(kind, size)
            lines = wrap_text(text, font, max_w)
            if size <= min_size or (len(lines) <= max_lines and all(font.getlength(l) <= max_w for l in lines)):
                return font, lines[:max_lines]
            size -= 2

    def _fit_headline(self, text):
        if text not in self._head_cache:
            for size in range(84, 50, -4):
                font = self.fonts.get("headline", size)
                lines = wrap_text(text, font, self.content_w)
                if (len(lines) <= 3 and len(lines) * size * 1.04 <= 236
                        and all(font.getlength(l) <= self.content_w for l in lines)):
                    break
            self._head_cache[text] = (font, lines[:3])
        return self._head_cache[text]

    @staticmethod
    def _highlight(scene, lines) -> set:
        words = [(li, wi, w) for li, line in enumerate(lines) for wi, w in enumerate(line.split())]

        def norm(s):
            return re.sub(r"[^A-Z0-9]", "", s.upper())
        want = [norm(w) for w in clean(scene.get("highlight")).split() if norm(w)]
        if want:
            keys = [norm(w) for _, _, w in words]
            for i in range(len(keys) - len(want) + 1):
                if keys[i:i + len(want)] == want:
                    return {(li, wi) for li, wi, _ in words[i:i + len(want)]}
        if len(lines) >= 2:
            return {(len(lines) - 1, wi) for wi in range(len(lines[-1].split()))}
        if len(words) >= 2:
            return {words[-1][:2]}
        return set()

    def _reveal_times(self, scene, n: int, dur: float) -> list[float]:
        cues = scene.get("_cues")
        if cues and len(cues) == n:
            return [max(0.2, c - 0.15) for c in cues]
        span = max(dur * 0.72 - 0.25, 0.6)
        return [0.25 + i * span / max(n, 1) for i in range(n)]

    # ------------------------------------------------------------------ persistent chrome
    def _header(self, draw, progress: float, label: str, t: float):
        m, y = self.margin, 72
        f, fr = self.fonts.get("mono_bold", 26), self.fonts.get("mono", 26)
        x = self._tracked(draw, m, y, clean(self.cfg.brand.channel_name).upper(), f, INK, 3)
        x = self._tracked(draw, x + 8, y, "/", fr, DIM, 3)
        self._tracked(draw, x + 12, y, clean(label).upper()[:22], fr, MUTED, 3)
        status = "DAILY BRIEF"
        xr = self.W - m - self._tracked_len(status, f, 3)
        self._tracked(draw, xr, y, status, f, self.accent2, 3)
        pulse = 0.5 + 0.5 * math.sin(t * 4)
        draw.ellipse([xr - 28, y - 7, xr - 14, y + 7], fill=lerp_rgb(BG_TOP, self.accent2, 0.35 + 0.65 * pulse))
        draw.line([(m, 112), (self.W - m, 112)], fill=LINE, width=2)
        draw.rectangle([m, 124, self.W - m, 128], fill=LINE)
        draw.rectangle([m, 124, m + int(self.content_w * clamp01(progress)), 128], fill=self.accent)

    def _kicker(self, draw, index: int, scene: dict, t: float, y: float = 168):
        text = clean(scene.get("kicker") or DEFAULT_KICKERS.get(scene.get("type"), "")).upper()[:32]
        full = f"{index + 1:02d} // {text}"
        shown = int(len(full) * clamp01(t / 0.45))
        f = self.fonts.get("mono_bold", 28)
        x = self._tracked(draw, self.margin, y, full[:5][:shown], f, MUTED, 3)
        x = self._tracked(draw, x, y, full[5:shown], f, self.accent, 3)
        if shown < len(full):
            draw.rectangle([x + 2, y - 14, x + 18, y + 14], fill=self.accent)

    def _headline(self, draw, scene: dict, t: float, y: float = 210):
        text = clean(scene.get("on_screen")).upper()
        if not text:
            return
        font, lines = self._fit_headline(text)
        hl = self._highlight(scene, lines)
        lh = font.size * 1.04
        y = y + font.size * 0.86
        k = 0
        for li, line in enumerate(lines):
            x = self.margin
            for wi, word in enumerate(line.split()):
                a = ease_out((t - 0.05 - 0.045 * k) / 0.35)
                k += 1
                if a > 0:
                    col = self.accent if (li, wi) in hl else INK
                    draw.text((x, y + li * lh + (1 - a) * 24), word, font=font, fill=lerp_rgb(BG_TOP, col, a), anchor="ls")
                x += font.getlength(word + " ")

    def _panel_chrome(self, draw, scene: dict, t: float):
        x0, y0, x1, y1 = self.panel_box
        draw.rounded_rectangle([x0, y0, x1, y1], radius=20, outline=self.edge, width=2)
        draw.line([(x0, y0 + 30), (x0, y0 + 110)], fill=self.accent, width=4)
        draw.line([(x1, y1 - 110), (x1, y1 - 30)], fill=self.accent, width=4)
        draw.line([(x1 - 110, y1), (x1 - 30, y1)], fill=self.accent, width=4)
        kind = scene.get("type")
        f = self.fonts.get("mono", 22)
        self._tracked(draw, x0 + 30, y0 + 40, PANEL_LABELS.get(kind, "NOTES"), f, MUTED, 3)
        pill = {"explain": f"{len(scene.get('bullets') or [1])} POINTS", "diagram": f"{len(scene.get('nodes') or [])} STEPS",
                "compare": "A / B", "code": "TERMINAL", "fact": "NOTE", "payoff": "DONE"}.get(kind, "NEW")
        fp = self.fonts.get("mono_bold", 20)
        pw = self._tracked_len(pill, fp, 2) + 34
        draw.rounded_rectangle([x1 - 30 - pw, y0 + 20, x1 - 30, y0 + 60], radius=20, outline=self.accent2, width=2)
        self._tracked(draw, x1 - 30 - pw + 17, y0 + 40, pill, fp, self.accent2, 2)
        sweep = self.intro + 0.25
        if t < sweep:                       # a light runs along the frame as the new scene lands
            sx = x0 + (x1 - x0) * ease_out(t / sweep)
            draw.line([(max(x0 + 20, sx - 180), y0), (sx, y0)], fill=self.accent, width=4)

    def _captions(self, draw, chunk: dict | None, t: float, speaker: int = 0):
        x0, y0, x1, y1 = self.cap_box
        if len(self.cast) == 2:
            who = self.cast[speaker]
            edge = lerp_rgb(LINE, who.color, 0.75)
            draw.rounded_rectangle([x0, y0, x1, y1], radius=26, fill=CAP_BG, outline=edge, width=3)
            cx = self.cast_pos[speaker][0] + who.size[0] / 2
            tail = [(cx - 24, y1 - 2), (cx + 24, y1 - 2), (cx + (18 if speaker == 0 else -18), y1 + 30)]
            draw.polygon(tail, fill=CAP_BG)
            draw.line([tail[0], tail[2], tail[1]], fill=edge, width=3)
            fn = self.fonts.get("mono_bold", 22)
            lx = x0 + 30 if speaker == 0 else x1 - 30 - self._tracked_len(who.name, fn, 3)
            self._tracked(draw, lx, y0 + 32, who.name, fn, who.color, 3)
            y0 += 34
        elif self.host:
            draw.rounded_rectangle([x0, y0, x1, y1], radius=26, fill=CAP_BG, outline=self.edge, width=3)
            tail = [(x0 + 2, y0 + 110), (x0 - 34, y0 + 150), (x0 + 2, y0 + 146)]       # points at the host's mouth
            draw.polygon(tail, fill=CAP_BG)
            draw.line([tail[0], tail[1], tail[2]], fill=self.edge, width=3)
            fn = self.fonts.get("mono_bold", 22)
            self._tracked(draw, x0 + 30, y0 + 34, self.host.name, fn, self.accent2, 3)
            y0 += 40
        else:
            draw.rectangle([x0, y0, x1, y1], fill=CAP_BG)
            draw.line([(x0, y0), (x1, y0)], fill=self.edge, width=2)
        if not chunk:
            return
        words = [(clean(w), s) for w, s, _ in chunk["words"] if clean(w)]
        text = " ".join(w for w, _ in words)
        font, lines = self._fit(text, "mono_bold", 50, int(x1 - x0 - 70), 34, max_lines=2)
        lh = font.size * 1.25
        y = (y0 + y1) / 2 - (len(lines) - 1) * lh / 2
        space = font.getlength(" ")
        i = 0
        for li, line in enumerate(lines):
            lw = line.split()
            x = (x0 + x1) / 2 - font.getlength(line) / 2
            for w in lw:
                spoken = i < len(words) and words[i][1] <= t + 0.02
                draw.text((x, y + li * lh), w, font=font, fill=INK if spoken else lerp_rgb(CAP_BG, INK, 0.32), anchor="lm")
                x += font.getlength(w) + space
                i += 1

    @staticmethod
    def mouth_level(chunk: dict | None, t: float) -> int:
        """0 = closed .. 3 = wide open, flapping while a caption word is being spoken."""
        if not chunk:
            return 0
        for k, (_, s, e) in enumerate(chunk["words"]):
            if s <= t < e:
                return int(round(1.5 + 1.5 * math.sin((t - s) * 2 * math.pi * 3.2 + k)))
        return 0

    def _draw_cast(self, img, scene: dict, caption: dict | None, t: float, speaker: int):
        kind = scene.get("type", "")
        mouth = self.mouth_level(caption, t)
        duo = len(self.cast) == 2
        for i, (host, (x, y)) in enumerate(zip(self.cast, self.cast_pos)):
            talking = i == speaker
            if talking:
                pose, look = POSES.get(kind, "idle"), None
            else:                                   # the listener watches the speaker and reacts
                pose, look = {"fact": "wow", "payoff": "wave"}.get(kind, "idle"), (1.0, 0.1)
            if pose == "idle" and duo:
                look = (1.0, 0.1)
            sprite = host.sprite(t + i * 1.3, pose, mouth if talking else 0, look)
            if i == 1:
                sprite = ImageOps.mirror(sprite)           # face the other character
            bob = 4 * math.sin(t * 2.2 + i * 1.7) + (2 * math.sin(t * 9) if talking and mouth else 0)
            if talking:
                self._screen(img, self._glow_sprite(260, host.color, 0.30), x + sprite.width / 2, y + sprite.height * 0.47)
            img.paste(sprite, (int(x), int(y + bob)), sprite)

    def _footer(self, index: int, draw):
        if len(self.cast) == 2:
            return                                  # the characters stand where the footer would be
        f = self.fonts.get("mono", 22)
        x, y = (self.cap_box[0], self.cap_box[3] + 40) if self.host else (self.margin, 1648)
        domain = clean(self.meta.get("domain")).upper()
        if domain:
            self._tracked(draw, x, y, f"SOURCE: {domain}"[:30], f, DIM, 2)
        n = max(len(self.scenes), index + 1)
        right = f"{index + 1:02d} / {n:02d}"
        self._tracked(draw, self.W - self.margin - self._tracked_len(right, f, 2), y, right, f, DIM, 2)

    # ------------------------------------------------------------------ panel scenes (drawn in panel coordinates)
    def _card(self, d, box, a=1.0, active=False, radius=18):
        fill = lerp_rgb(CARD, self.accent, 0.10) if active else CARD
        fill = lerp_rgb(PANEL, fill, a)
        d.rounded_rectangle(box, radius=radius, fill=fill, outline=lerp_rgb(PANEL, self.accent if active else LINE, a),
                            width=3 if active else 2)
        return fill

    def _check(self, d, cx, cy, s, color, width=5):
        d.line([(cx - s, cy), (cx - s * 0.3, cy + s * 0.7), (cx + s, cy - s * 0.7)], fill=color, width=width, joint="curve")

    def _s_title(self, layer, d, scene, t, dur):
        pw, ph = layer.size
        ry_max = min(215, (ph - 360) / 2)
        cx, cy = pw / 2, 90 + ry_max
        for k in range(2):                                    # sonar pulses from the core
            p = (t * 0.45 + k * 0.5) % 1.0
            r = 120 + 270 * p
            d.ellipse([cx - r, cy - r * 0.64, cx + r, cy + r * 0.64], outline=lerp_rgb(PANEL, self.accent, 0.4 * (1 - p)), width=2)
        for rx, ry, speed, col in ((340, ry_max, 40, self.accent), (225, ry_max * 0.66, -65, self.accent2)):
            d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], outline=lerp_rgb(PANEL, col, 0.22), width=2)
            ang = (t * speed) % 360
            d.arc([cx - rx, cy - ry, cx + rx, cy + ry], start=ang, end=ang + 48, fill=col, width=4)
        labels = self._satellites()
        fs = self.fonts.get("mono_bold", 22)
        for i, lab in enumerate(labels):
            a = ease_out((t - 0.5 - 0.25 * i) / 0.4)
            if a <= 0:
                continue
            ang = math.radians(-55 + i * 360 / len(labels) + t * 7)
            px, py = cx + 340 * math.cos(ang), cy + ry_max * math.sin(ang)
            tw = self._tracked_len(lab, fs, 2)
            w = tw + 36
            px = min(max(px, w / 2 + 14), pw - w / 2 - 14)
            d.line([(cx + 118 * math.cos(ang), cy + 118 * math.sin(ang)), (px, py)], fill=lerp_rgb(PANEL, LINE, a * 1.4), width=2)
            d.rounded_rectangle([px - w / 2, py - 23, px + w / 2, py + 23], radius=10, fill=lerp_rgb(PANEL, CARD, a),
                                outline=lerp_rgb(PANEL, self.accent2, a * 0.8), width=2)
            self._tracked(d, px - tw / 2, py, lab, fs, lerp_rgb(CARD, self.accent2, a), 2)
        self._screen(layer, self._glow_sprite(250, self.accent, 0.5), cx, cy)
        r = 112 * (0.8 + 0.2 * ease_out(t / 0.6)) + 3 * math.sin(t * 2.5)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=CARD, outline=self.accent, width=5)
        font, lines = self._fit(self.subject.upper(), "headline", 52, int(2 * r - 44), 22, max_lines=2)
        lh = font.size * 1.05
        for i, line in enumerate(lines):
            d.text((cx, cy + (i - (len(lines) - 1) / 2) * lh), line, font=font, fill=INK, anchor="mm")
        self._source_card(d, pw, ph, t)

    def _satellites(self) -> list[str]:
        out = []
        for s in self.scenes[1:-1]:
            words, lab = clean(s.get("on_screen")).upper().split(), ""
            for w in words:
                if len(lab) + len(w) + 1 > 18:
                    break
                lab = (lab + " " + w).strip()
            if lab and lab not in out:
                out.append(lab)
        return out[:4]

    def _source_card(self, d, pw, ph, t):
        a = ease_out((t - 0.8) / 0.5)
        if a <= 0:
            return
        off = (1 - a) * 30
        x0, y0, x1, y1 = 36, ph - 252 + off, pw - 36, ph - 36 + off
        fill = self._card(d, [x0, y0, x1, y1], a)
        d.rectangle([x0, y0 + 22, x0 + 5, y1 - 22], fill=lerp_rgb(PANEL, self.accent, a))
        fm = self.fonts.get("mono", 20)
        self._tracked(d, x0 + 32, y0 + 38, "SOURCE", fm, lerp_rgb(fill, MUTED, a), 3)
        date = clean(self.meta.get("published"))[:10]
        if date:
            self._tracked(d, x1 - 32 - self._tracked_len(date, fm, 2), y0 + 38, date, fm, lerp_rgb(fill, MUTED, a), 2)
        title = clean(self.meta.get("topic_title"))
        if title:
            font, lines = self._fit(title, "bold", 38, int(x1 - x0 - 64), 28, max_lines=2)
            for i, line in enumerate(lines):
                d.text((x0 + 32, y0 + 66 + i * font.size * 1.18), line, font=font, fill=lerp_rgb(fill, INK, a), anchor="la")
        domain = clean(self.meta.get("domain")) or clean(self.meta.get("source"))
        if domain:
            self._tracked(d, x0 + 32, y1 - 34, domain[:44], self.fonts.get("mono_bold", 24), lerp_rgb(fill, self.accent, a), 1)

    def _s_explain(self, layer, d, scene, t, dur):
        pw, ph = layer.size
        bullets = [clean(b) for b in scene.get("bullets") or []][:4] or [clean(scene.get("on_screen"))]
        n = len(bullets)
        big = n <= 2
        f, fn = self.fonts.get("bold", 52 if big else 44), self.fonts.get("mono_bold", 34 if big else 30)
        row_h, gap = (210, 34) if big else (156, 26)
        row_h = min(row_h, (ph - 140 - (n - 1) * gap) / n)
        top = 100 + max(0, (ph - 140 - (n * row_h + (n - 1) * gap)) / 2)
        starts = self._reveal_times(scene, n, dur)
        active = max([i for i in range(n) if t >= starts[i]], default=-1)
        for i, b in enumerate(bullets):
            a = ease_out((t - starts[i]) / 0.45)
            if a <= 0:
                continue
            on = i == active
            y0, x0, x1 = top + i * (row_h + gap), 36 + (1 - a) * 70, pw - 36 + (1 - a) * 70
            fill = self._card(d, [x0, y0, x1, y0 + row_h], a, active=on)
            if on:
                d.rectangle([x0, y0 + 24, x0 + 6, y0 + row_h - 24], fill=self.accent)
            d.text((x0 + 40, y0 + row_h / 2), f"{i + 1:02d}", font=fn, fill=lerp_rgb(fill, self.accent if on else MUTED, a), anchor="lm")
            lines = wrap_text(b, f, int(x1 - x0 - 170))[:2]
            col = INK if on or i > active else lerp_rgb(CARD, INK, 0.62)
            lh = f.size * 1.22
            for j, line in enumerate(lines):
                d.text((x0 + 120, y0 + row_h / 2 + (j - (len(lines) - 1) / 2) * lh), line, font=f,
                       fill=lerp_rgb(fill, col, a), anchor="lm")
            if on:                                         # runs until the next point lands
                nxt = starts[i + 1] if i + 1 < n else dur
                p = clamp01((t - starts[i]) / max(nxt - starts[i], 0.1))
                d.rectangle([x0 + 120, y0 + row_h - 16, x0 + 120 + (x1 - x0 - 160) * p, y0 + row_h - 12],
                            fill=lerp_rgb(fill, self.accent, 0.6))

    def _s_diagram(self, layer, d, scene, t, dur):
        pw, ph = layer.size
        nodes = [clean(x) for x in scene.get("nodes") or []][:5]
        if not nodes:
            return self._s_explain(layer, d, scene, t, dur)
        n = len(nodes)
        box_h = 112 if n <= 4 else 100
        area_top, area_bot = 100, ph - 40
        gap = min(96, (area_bot - area_top - n * box_h) / max(n - 1, 1))
        top = area_top + (area_bot - area_top - (n * box_h + (n - 1) * gap)) / 2
        bw, zig = 600, (70 if n >= 3 else 0)
        centers = [(pw / 2 + (zig if i % 2 else -zig), top + i * (box_h + gap) + box_h / 2) for i in range(n)]
        starts = self._reveal_times(scene, n, dur)
        active = max([i for i in range(n) if t >= starts[i]], default=-1)
        for i in range(n - 1):
            a = ease_out((t - starts[i + 1] + 0.3) / 0.3)
            if a <= 0:
                continue
            (xa, ya), (xb, yb) = centers[i], centers[i + 1]
            ya, yb = ya + box_h / 2, yb - box_h / 2
            d.line([(xa, ya), (xa + (xb - xa) * a, ya + (yb - ya) * a)], fill=lerp_rgb(PANEL, self.accent2, 0.4), width=3)
            if a >= 1:
                for k in range(3):                         # packets flowing down the connector
                    p = (t * 0.9 + k / 3) % 1
                    px, py = xa + (xb - xa) * p, ya + (yb - ya) * p
                    d.ellipse([px - 6, py - 6, px + 6, py + 6], fill=self.accent2)
                ang = math.atan2(yb - ya, xb - xa)
                tip = [(xb, yb), (xb - 18 * math.cos(ang - 0.45), yb - 18 * math.sin(ang - 0.45)),
                       (xb - 18 * math.cos(ang + 0.45), yb - 18 * math.sin(ang + 0.45))]
                d.polygon(tip, fill=self.accent2)
        ft = self.fonts.get("mono_bold", 20)
        for i, label in enumerate(nodes):
            a = ease_out((t - starts[i]) / 0.4)
            if a <= 0:
                continue
            cx, cy = centers[i]
            on = i == active
            s = 0.9 + 0.1 * a
            w, h = bw * s, box_h * s
            if on:
                self._screen(layer, self._glow_sprite(230, self.accent, 0.35), cx, cy)
            fill = self._card(d, [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], a, active=on)
            self._tracked(d, cx - w / 2 + 28, cy - h / 2 + 26, f"STEP {i + 1:02d}", ft,
                          lerp_rgb(fill, self.accent if on else MUTED, a), 2)
            font, lines = self._fit(label, "bold", 40, int(w - 110), 28)
            d.text((cx - w / 2 + 28, cy + 16), lines[0] if lines else "", font=font, fill=lerp_rgb(fill, INK, a), anchor="lm")
            if i < active:
                self._check(d, cx + w / 2 - 44, cy, 13, lerp_rgb(fill, self.accent2, a))

    def _s_compare(self, layer, d, scene, t, dur):
        pw, ph = layer.size
        gap = 44
        col_w = (pw - 2 * 32 - gap) / 2
        xs = [32, 32 + col_w + gap]
        titles = [clean(scene.get("left_title") or "Before"), clean(scene.get("right_title") or "After")]
        lists = [[clean(x) for x in scene.get("left") or []][:3], [clean(x) for x in scene.get("right") or []][:3]]
        colors = [self.accent2, self.accent]
        f = self.fonts.get("bold", 40)
        heights = [[len(wrap_text(x, f, int(col_w - 84))[:2]) * 50 + 76 for x in col] for col in lists]
        body = max((sum(h) + 18 * (len(h) - 1) for h in heights if h), default=0)
        head_h = 120
        head_y = 100 + max(0, (ph - 140 - (head_h + 28 + body)) / 2)
        for side in (0, 1):
            a = ease_out((t - 0.1 - 0.15 * side) / 0.45)
            if a <= 0:
                continue
            x, y = xs[side], head_y + (1 - a) * 30
            fill = self._card(d, [x, y, x + col_w, y + head_h], a)
            d.rectangle([x + 20, y, x + col_w - 20, y + 6], fill=lerp_rgb(PANEL, colors[side], a))
            font, lines = self._fit(titles[side].upper(), "headline", 44, int(col_w - 48), 24)
            d.text((x + col_w / 2, y + head_h / 2 + 4), lines[0] if lines else "", font=font, fill=lerp_rgb(fill, INK, a), anchor="mm")
        dash_off = (t * 40) % 26
        y = head_y + head_h + 24 + dash_off
        while y < ph - 40:
            d.line([(pw / 2, y), (pw / 2, min(y + 14, ph - 40))], fill=LINE, width=3)
            y += 26
        r = 40 + 3 * math.sin(t * 3)
        vy = head_y + head_h / 2
        d.ellipse([pw / 2 - r, vy - r, pw / 2 + r, vy + r], fill=INK)
        d.text((pw / 2, vy + 2), "VS", font=self.fonts.get("headline", 28), fill=PANEL, anchor="mm")
        order = [(side, j) for j in range(3) for side in (0, 1) if j < len(lists[side])]
        span = max(dur * 0.75 - 0.6, 0.6)
        cursor = [head_y + head_h + 28, head_y + head_h + 28]
        for k, (side, j) in enumerate(order):
            a = ease_out((t - 0.6 - k * span / max(len(order), 1)) / 0.4)
            lines = wrap_text(lists[side][j], f, int(col_w - 84))[:2]
            hh = len(lines) * 50 + 76
            x, y = xs[side], cursor[side]
            cursor[side] += hh + 18
            if a <= 0:
                continue
            fill = self._card(d, [x, y + (1 - a) * 24, x + col_w, y + hh + (1 - a) * 24], a)
            d.ellipse([x + 22, y + hh / 2 - 7, x + 36, y + hh / 2 + 7], fill=lerp_rgb(fill, colors[side], a))
            for li, line in enumerate(lines):
                d.text((x + 56, y + hh / 2 + (li - (len(lines) - 1) / 2) * 50 + (1 - a) * 24), line, font=f,
                       fill=lerp_rgb(fill, INK, a), anchor="lm")

    def _code_color(self, tok: str):
        if tok.startswith(("#", "//")):
            return MUTED
        if tok[:1] in "\"'":
            return self.accent2
        if tok[:1].isdigit():
            return AMBER
        if tok.lower() in CODE_KEYWORDS:
            return self.accent
        return INK

    def _s_code(self, layer, d, scene, t, dur):
        pw, ph = layer.size
        code = [clean(c) for c in scene.get("code") or []][:6]
        x0, x1 = 30, pw - 30
        longest = max((len(c) for c in code), default=1)
        size = 56
        while size > 24 and self.fonts.get("mono", size).getlength("M") * (longest + 5) > x1 - x0 - 60:
            size -= 2
        term_h = min(max(len(code), 3) * size * 1.6 + 200, ph - 136)
        y0 = 96 + (ph - 136 - term_h) / 2
        y1 = y0 + term_h
        d.rounded_rectangle([x0, y0, x1, y1], radius=18, fill=(4, 8, 10), outline=LINE, width=2)
        d.line([(x0, y0 + 60), (x1, y0 + 60)], fill=LINE, width=2)
        for i, c in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
            d.ellipse([x0 + 28 + i * 34, y0 + 21, x0 + 48 + i * 34, y0 + 41], fill=c)
        fm = self.fonts.get("mono", 22)
        title = f"~/{clean(self.cfg.brand.channel_name).lower()} - demo"
        self._tracked(d, (x0 + x1) / 2 - self._tracked_len(title, fm, 1) / 2, y0 + 31, title, fm, MUTED, 1)
        f = self.fonts.get("mono", size)
        cw, lh = f.getlength("M"), size * 1.6
        gutter = x0 + 30 + cw * 3.5
        total = sum(len(c) for c in code)
        shown = int(total * ease_in_out((t - 0.3) / max(dur * 0.65, 1.0)))
        y = y0 + 60 + 70
        for i, line in enumerate(code):
            take = max(0, min(len(line), shown))
            shown -= take
            d.text((x0 + 30, y), f"{i + 1:>2}", font=f, fill=DIM, anchor="lm")
            col = 0
            for tok in _CODE_TOKEN.findall(line):
                if col >= take:
                    break
                part = tok[:take - col]
                d.text((gutter + col * cw, y), part, font=f, fill=self._code_color(tok), anchor="lm")
                col += len(tok)
            typing = 0 < take < len(line) or (take == len(line) and shown == 0 and (i == len(code) - 1 or take == 0))
            if typing and (take < len(line) or int(t * 2.5) % 2 == 0):
                cx = gutter + take * cw
                d.rectangle([cx + 2, y - size * 0.55, cx + cw * 0.9, y + size * 0.55], fill=self.accent)
            y += lh
        sy = y0 + 62 + ((t * 120) % (y1 - y0 - 64))       # faint scanline
        d.line([(x0 + 2, sy), (x1 - 2, sy)], fill=(14, 24, 28), width=3)

    def _s_fact(self, layer, d, scene, t, dur):
        pw, ph = layer.size
        cx, cy, R = pw / 2, (76 + ph) / 2, 290
        rot = t * 8
        for k in range(72):
            ang = math.radians(k * 5 + rot)
            major = k % 6 == 0
            r0, r1 = R + (0 if major else 10), R + 28
            d.line([(cx + r0 * math.cos(ang), cy + r0 * math.sin(ang)), (cx + r1 * math.cos(ang), cy + r1 * math.sin(ang))],
                   fill=lerp_rgb(PANEL, self.accent, 0.6 if major else 0.25), width=3 if major else 2)
        p = ease_out((t - 0.2) / 1.4)
        if p > 0:
            d.arc([cx - R + 22, cy - R + 22, cx + R - 22, cy + R - 22], start=-90, end=-90 + 300 * p, fill=self.accent, width=10)
        d.arc([cx - R + 48, cy - R + 48, cx + R - 48, cy + R - 48], start=t * 50, end=t * 50 + 70, fill=self.accent2, width=4)
        self._screen(layer, self._glow_sprite(220, self.accent, 0.35), cx, cy)
        text = clean(scene.get("on_screen"))
        m = _NUMBER.search(text)
        a = ease_out(t / 0.5)
        if m:
            raw = m.group(0).strip()
            num = re.search(r"\d[\d,]*(?:\.\d+)?", raw).group(0)
            dec = len(num.split(".")[1]) if "." in num else 0
            val = float(num.replace(",", "")) * ease_out((t - 0.2) / 1.2)
            shown = f"{val:,.{dec}f}" if "," in num else f"{val:.{dec}f}"
            big = raw.replace(num, shown)
            font, _ = self._fit(raw, "headline", 170, 2 * R - 130, 60)
            d.text((cx, cy), big, font=font, fill=lerp_rgb(PANEL, INK, a), anchor="mm")
        else:
            font, lines = self._fit(self.subject.upper(), "headline", 110, 2 * R - 150, 40, max_lines=2)
            lh = font.size * 1.05
            for i, line in enumerate(lines):
                d.text((cx, cy + (i - (len(lines) - 1) / 2) * lh), line, font=font, fill=lerp_rgb(PANEL, INK, a), anchor="mm")

    def _s_payoff(self, layer, d, scene, t, dur):
        pw, ph = layer.size
        items = []
        for s in self.scenes[1:-1]:
            txt = clean(s.get("on_screen"))
            if txt and txt not in items:
                items.append(txt)
        items = items[:5] or [clean(scene.get("on_screen"))]
        fm = self.fonts.get("mono", 22)
        self._tracked(d, 40, 112, "WHAT WE COVERED", fm, MUTED, 3)
        gap, y = 16, 150
        row_h = min(104, (ph - 190 - 24 - y - gap * (len(items) - 1)) / len(items))
        for i, item in enumerate(items):
            a = ease_out((t - 0.15 - 0.12 * i) / 0.35)
            if a <= 0:
                continue
            tick = clamp01((t - 0.5 - 0.35 * i) / 0.25)
            yy = y + i * (row_h + gap)
            fill = self._card(d, [36, yy, pw - 36, yy + row_h], a)
            bx, by = 66, yy + row_h / 2
            if tick > 0:
                d.rounded_rectangle([bx, by - 22, bx + 44, by + 22], radius=8, fill=lerp_rgb(fill, self.accent, tick))
                self._check(d, bx + 22, by + 1, 11 * tick, PANEL, 5)
            else:
                d.rounded_rectangle([bx, by - 22, bx + 44, by + 22], radius=8, outline=lerp_rgb(fill, MUTED, a), width=2)
            font, lines = self._fit(item, "bold", min(42, int(row_h * 0.45)), pw - 220, 24)
            d.text((bx + 76, by), lines[0] if lines else "", font=font,
                   fill=lerp_rgb(fill, INK if tick >= 1 else MUTED, a), anchor="lm")
        a = ease_out((t - 0.6 - 0.35 * len(items)) / 0.5)
        if a > 0:
            y0 = ph - 190 + (1 - a) * 30
            fill = lerp_rgb(PANEL, lerp_rgb(CARD, self.accent, 0.14), a)
            d.rounded_rectangle([36, y0, pw - 36, y0 + 140], radius=20, fill=fill, outline=lerp_rgb(PANEL, self.accent, a), width=3)
            fh = self.fonts.get("mono_bold", 26)
            handle = clean(self.cfg.brand.handle).upper()
            self._tracked(d, pw / 2 - self._tracked_len(handle, fh, 3) / 2, y0 + 44, handle, fh, lerp_rgb(fill, self.accent, a), 3)
            fc, lc = self._fit(clean(self.cfg.brand.cta), "bold", 36, pw - 140, 26)
            d.text((pw / 2, y0 + 96), lc[0] if lc else "", font=fc, fill=lerp_rgb(fill, INK, a), anchor="mm")

    SCENES = {"title": _s_title, "explain": _s_explain, "diagram": _s_diagram, "compare": _s_compare,
              "code": _s_code, "fact": _s_fact, "payoff": _s_payoff}

    # ------------------------------------------------------------------ frame
    def _index_of(self, scene: dict) -> int:
        for i, s in enumerate(self.scenes):
            if s is scene:
                return i
        return 0

    def frame(self, scene: dict, t_local: float, scene_dur: float, t_global: float, progress: float,
              label: str, caption: dict | None, index: int | None = None) -> Image.Image:
        if index is None:
            index = self._index_of(scene)
        turns = scene.get("_turns")
        who = speaker_at(turns, t_local) or (caption or {}).get("who") or "host"
        speaker = 1 if who == "guest" and len(self.cast) == 2 else 0
        if caption and caption.get("who") and caption["who"] != who:
            caption = None                          # a chunk from the other speaker is ending
        grain = self.grain[int(t_global * self.fps) % len(self.grain)]
        shot = self._shot(scene, t_local, who)
        if shot != "panel":
            k = turn_at(turns, t_local)
            lines = scene.get("lines") or []
            line = lines[k]["text"] if k is not None and k < len(lines) else ""
            img = (self._cine_close(scene, t_global, caption, speaker, line) if shot == "close"
                   else self._cine_wide(scene, t_global, t_local, caption, speaker, index))
            self._progress(ImageDraw.Draw(img), progress)
            return ImageChops.add(img, grain, offset=-self.GRAIN_BIAS)
        img = self.background(t_global).copy()
        draw = ImageDraw.Draw(img)
        self._header(draw, progress, label, t_global)
        self._kicker(draw, index, scene, t_local)
        self._headline(draw, scene, t_local)

        layer = self.panel_base.copy()
        self.SCENES.get(scene.get("type"), Renderer._s_explain)(self, layer, ImageDraw.Draw(layer), scene, t_local, scene_dur)
        # camera: settle in as the scene lands, then a slow push with a little drift
        intro = ease_out(t_local / self.intro)
        zoom = 1.0 + 0.035 * ease_in_out(t_local / max(scene_dur, 1.0)) + 0.06 * (1 - intro)
        pw, ph = layer.size
        cw, ch = pw / zoom, ph / zoom
        dx = (pw - cw) / 2 * 0.5 * math.sin(t_local * 0.35)
        bx, by = (pw - cw) / 2 + dx, (ph - ch) / 2
        layer = layer.resize((pw, ph), Image.BILINEAR, box=(bx, by, bx + cw, by + ch))
        if intro < 1:
            layer = Image.blend(self.panel_base, layer, intro)
        img.paste(layer, self.panel_box[:2], self.panel_mask)
        self._panel_chrome(draw, scene, t_local)

        self._captions(draw, caption, t_global, speaker)
        if self.cast:
            self._draw_cast(img, scene, caption, t_global, speaker)
        self._footer(index, draw)
        return ImageChops.add(img, grain, offset=-self.GRAIN_BIAS)


def source_meta(script) -> dict:
    """What the renderer shows about the source: domain, outlet, date and the article headline."""
    url = (script.sources or [""])[0]
    domain = urlparse(url).netloc.lower()
    for prefix in ("www.", "en.", "m."):
        domain = domain.removeprefix(prefix)
    extra = script.extra or {}
    return {"domain": domain, "source": extra.get("source", ""), "published": extra.get("published") or "",
            "subject": extra.get("subject", ""), "topic_title": script.topic_title, "category": script.category,
            "cast": extra.get("cast") or []}
