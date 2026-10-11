"""Procedural motion graphics renderer (Pillow). Produces one 1080x1920 RGB frame per call.

No GPU, no external assets: animated gradient/blob or voxel backgrounds, animated typography,
bullet reveals, flow diagrams, comparisons, typed code panels, big-fact pulses, captions with
word highlighting, progress bar and brand chip.
"""
from __future__ import annotations

import math
import random
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from .utils import ROOT

THEMES = {
    "neon": dict(bg=("#0b0f1a", "#141b2d"), blobs=["#7c5cff", "#00d4ff", "#ff4fd8"], text="#ffffff",
                 muted="#aab4d4", accent="#00d4ff", accent2="#ff4fd8", panel="#161d33", style="blobs"),
    "blueprint": dict(bg=("#071e3a", "#0d3a6b"), blobs=["#1f6feb", "#2bb3ff"], text="#eaf4ff",
                      muted="#9fc4e8", accent="#ffd166", accent2="#2bb3ff", panel="#0b2d55", style="grid"),
    "voxel": dict(bg=("#1b2a17", "#2e4a28"), blobs=["#5aa02c", "#8b5a2b", "#3b8dd6", "#7d7d7d"], text="#ffffff",
                  muted="#cfe6c3", accent="#ffd23f", accent2="#5aa02c", panel="#243b1f", style="voxel"),
    "sunset": dict(bg=("#2b0b3a", "#7a1f4e"), blobs=["#ff7a59", "#ffb347", "#ff4f81"], text="#fff8f0",
                   muted="#ffd9c7", accent="#ffe066", accent2="#ff7a59", panel="#3d1050", style="blobs"),
    "terminal": dict(bg=("#050807", "#0b1410"), blobs=["#00ff9c", "#00b36b"], text="#d7ffe9",
                     muted="#7fd6a8", accent="#00ff9c", accent2="#00b36b", panel="#0d1f17", style="scan"),
}


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


class Fonts:
    def __init__(self, cfg):
        self.paths = {
            "bold": self._find(cfg.brand.font_candidates_bold),
            "regular": self._find(cfg.brand.font_candidates_regular),
            "mono": self._find(cfg.brand.font_candidates_mono),
        }
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
            path = self.paths.get(kind) or self.paths.get("regular")
            if path:
                self.cache[key] = ImageFont.truetype(path, size)
            else:
                self.cache[key] = ImageFont.load_default(size)
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
    def __init__(self, cfg, theme_name: str, seed: int = 0):
        self.cfg = cfg
        self.W, self.H = int(cfg.video.width), int(cfg.video.height)
        self.fps = int(cfg.video.fps)
        self.theme = THEMES.get(theme_name, THEMES["neon"])
        self.name = theme_name
        self.fonts = Fonts(cfg)
        self.rng = random.Random(seed)
        self.text = hex_to_rgb(self.theme["text"])
        self.muted = hex_to_rgb(self.theme["muted"])
        self.accent = hex_to_rgb(self.theme["accent"])
        self.accent2 = hex_to_rgb(self.theme["accent2"])
        self.panel = hex_to_rgb(self.theme["panel"])
        self.bg_mid = lerp_rgb(hex_to_rgb(self.theme["bg"][0]), hex_to_rgb(self.theme["bg"][1]), 0.5)
        self.base = self._gradient()
        self.small = (self.W // 8, self.H // 8)
        self.blob_params = [(self.rng.uniform(0, 6.28), self.rng.uniform(0.05, 0.12), self.rng.uniform(0.25, 0.45))
                            for _ in self.theme["blobs"]]
        self.voxels = [(self.rng.random(), self.rng.random(), self.rng.uniform(0.01, 0.03), self.rng.choice(self.theme["blobs"]),
                        self.rng.randint(1, 3)) for _ in range(48)]
        self.grid = self._grid() if self.theme["style"] == "grid" else None
        self.margin = 80
        self.content_w = self.W - 2 * self.margin

    # ------------------------------------------------------------------ backgrounds
    def _gradient(self) -> Image.Image:
        a, b = hex_to_rgb(self.theme["bg"][0]), hex_to_rgb(self.theme["bg"][1])
        t = np.linspace(0, 1, self.H, dtype=np.float32)[:, None]
        col = np.array(a, dtype=np.float32)[None, :] * (1 - t) + np.array(b, dtype=np.float32)[None, :] * t
        arr = np.repeat(col[:, None, :], self.W, axis=1).astype(np.uint8)
        return Image.fromarray(arr, "RGB")

    def _grid(self) -> Image.Image:
        g = Image.new("RGB", (self.W, self.H), (0, 0, 0))
        d = ImageDraw.Draw(g)
        for x in range(0, self.W, 60):
            d.line([(x, 0), (x, self.H)], fill=(18, 40, 70), width=1)
        for y in range(0, self.H, 60):
            d.line([(0, y), (self.W, y)], fill=(18, 40, 70), width=1)
        return g

    def background(self, t: float) -> Image.Image:
        style = self.theme["style"]
        if style == "voxel":
            sw, sh = self.W // 40, self.H // 40
            layer = Image.new("RGB", (sw, sh), (0, 0, 0))
            d = ImageDraw.Draw(layer)
            for (x, y, speed, color, size) in self.voxels:
                yy = (y + t * speed) % 1.0
                px, py = int(x * sw), int(yy * sh)
                c = hex_to_rgb(color)
                d.rectangle([px, py, px + size - 1, py + size - 1], fill=tuple(int(v * 0.30) for v in c))
            layer = layer.resize((self.W, self.H), Image.NEAREST)
            img = ImageChops.add(self.base, layer)
            # subtle horizon band
            d2 = ImageDraw.Draw(img)
            d2.rectangle([0, int(self.H * 0.985), self.W, self.H], fill=self.accent2)
            return img
        layer = Image.new("RGB", self.small, (0, 0, 0))
        d = ImageDraw.Draw(layer)
        sw, sh = self.small
        for (phase, speed, size), color in zip(self.blob_params, self.theme["blobs"]):
            cx = sw * (0.5 + 0.38 * math.sin(phase + t * speed * 2))
            cy = sh * (0.5 + 0.40 * math.cos(phase * 1.3 + t * speed * 1.6))
            r = size * sw
            c = hex_to_rgb(color)
            d.ellipse([cx - r, cy - r * 1.4, cx + r, cy + r * 1.4], fill=tuple(int(v * 0.5) for v in c))
        layer = layer.filter(ImageFilter.GaussianBlur(10)).resize((self.W, self.H), Image.BILINEAR)
        img = ImageChops.add(self.base, layer)
        if self.grid is not None:
            img = ImageChops.add(img, self.grid)
        if style == "scan":
            d3 = ImageDraw.Draw(img)
            y = int((t * 160) % self.H)
            d3.rectangle([0, y, self.W, y + 3], fill=tuple(int(v * 0.35) for v in self.accent))
        return img

    # ------------------------------------------------------------------ helpers
    def _fade(self, color, a: float):
        return lerp_rgb(self.bg_mid, color, ease_out(a))

    def _text_lines(self, draw, lines, font, cx, y, fill, spacing=1.12, align="center", x_left=None):
        h = font.size * spacing
        for i, line in enumerate(lines):
            w = font.getlength(line)
            x = (cx - w / 2) if align == "center" else x_left
            draw.text((x, y + i * h), line, font=font, fill=fill)
        return y + len(lines) * h

    def _pill(self, draw, box, fill, radius=28, outline=None, width=3):
        draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width if outline else 0)

    def _fit_font(self, text, kind, size, max_w, min_size=36):
        font = self.fonts.get(kind, size)
        while size > min_size and any(font.getlength(l) > max_w for l in wrap_text(text, font, max_w)):
            size -= 4
            font = self.fonts.get(kind, size)
        return font

    # ------------------------------------------------------------------ overlays
    def _top(self, draw, progress: float, label: str):
        x0, x1, y = self.margin, self.W - self.margin, 118
        draw.rounded_rectangle([x0, y, x1, y + 8], radius=4, fill=self.panel)
        draw.rounded_rectangle([x0, y, x0 + max(8, int((x1 - x0) * clamp01(progress))), y + 8], radius=4, fill=self.accent)
        f = self.fonts.get("bold", 30)
        label = label.upper()[:28]
        w = f.getlength(label)
        self._pill(draw, [x0, 150, x0 + w + 44, 150 + 52], fill=self.panel, radius=26)
        draw.text((x0 + 22, 157), label, font=f, fill=self.accent)

    def _brand(self, draw):
        f1, f2 = self.fonts.get("bold", 34), self.fonts.get("regular", 30)
        name, handle = self.cfg.brand.channel_name, self.cfg.brand.handle
        y = self.H - 330
        draw.ellipse([self.margin, y, self.margin + 44, y + 44], fill=self.accent)
        draw.text((self.margin + 60, y - 2), name, font=f1, fill=self.text)
        draw.text((self.margin + 60 + f1.getlength(name) + 16, y + 2), handle, font=f2, fill=self.muted)

    def _captions(self, draw, chunk: dict | None, t: float):
        if not chunk:
            return
        words = [w for w, _, _ in chunk["words"]]
        text = " ".join(words)
        font = self._fit_font(text, "bold", 64, self.content_w - 60, 44)
        total_w = sum(font.getlength(w) for w in words) + font.getlength(" ") * (len(words) - 1)
        cy = int(self.H * 0.705)
        pad_x, pad_y = 30, 18
        box_h = font.size + 2 * pad_y
        x0 = self.W / 2 - total_w / 2
        self._pill(draw, [x0 - pad_x, cy - box_h / 2, x0 + total_w + pad_x, cy + box_h / 2],
                   fill=(12, 12, 16), radius=22)
        x = x0
        for w, s, e in chunk["words"]:
            active = s <= t < e or (t >= e and chunk["words"][-1][0] == w and t < chunk["end"] + 0.05)
            fill = self.accent if active else self.text
            draw.text((x, cy - font.size * 0.58), w, font=font, fill=fill)
            x += font.getlength(w) + font.getlength(" ")

    # ------------------------------------------------------------------ scenes
    def _s_title(self, img, draw, scene, t, dur):
        title = scene["on_screen"]
        font = self._fit_font(title, "bold", 100, self.content_w, 60)
        lines = wrap_text(title, font, self.content_w)
        y = 330
        h = font.size * 1.1
        for i, line in enumerate(lines):
            a = ease_out((t - 0.12 * i) / 0.55)
            w = font.getlength(line)
            draw.text(((self.W - w) / 2, y + i * h + (1 - a) * 40), line, font=font, fill=self._fade(self.text, a))
        bar_y = y + len(lines) * h + 30
        bw = int(self.content_w * 0.5 * ease_out((t - 0.3) / 0.6))
        if bw > 0:
            draw.rounded_rectangle([(self.W - bw) / 2, bar_y, (self.W + bw) / 2, bar_y + 12], radius=6, fill=self.accent)
        sub = scene.get("subtitle") or ""
        if sub:
            f2 = self.fonts.get("regular", 42)
            a = ease_out((t - 0.6) / 0.6)
            self._text_lines(draw, wrap_text(sub, f2, self.content_w), f2, self.W / 2, bar_y + 60, self._fade(self.muted, a))
        # decorative rotating ring
        cx, cy, r = self.W / 2, int(self.H * 0.52), 160
        ang = t * 60
        draw.arc([cx - r, cy - r, cx + r, cy + r], start=ang, end=ang + 250, fill=self.accent2, width=10)
        draw.arc([cx - r * 0.7, cy - r * 0.7, cx + r * 0.7, cy + r * 0.7], start=-ang * 1.5, end=-ang * 1.5 + 200,
                 fill=self.accent, width=6)

    def _headline(self, draw, text, y=300):
        font = self._fit_font(text, "bold", 66, self.content_w, 44)
        lines = wrap_text(text, font, self.content_w)
        return self._text_lines(draw, lines, font, self.W / 2, y, self.text) + 24

    def _s_explain(self, img, draw, scene, t, dur):
        y = self._headline(draw, scene["on_screen"])
        bullets = scene.get("bullets") or []
        reveal = max(dur * 0.7, 1.0)
        f = self.fonts.get("regular", 46)
        box_h = 112
        block_h = len(bullets) * (box_h + 22)
        y = max(y + 30, int((y + 30 + int(self.H * 0.64)) / 2 - block_h / 2))
        for i, b in enumerate(bullets):
            a = ease_out((t - i * reveal / max(len(bullets), 1)) / 0.5)
            if a <= 0:
                continue
            dx = (1 - a) * 80
            x0, y0 = self.margin + dx, y + i * (box_h + 22)
            self._pill(draw, [x0, y0, self.W - self.margin + dx, y0 + box_h], fill=self._fade(self.panel, a), radius=26)
            draw.ellipse([x0 + 30, y0 + box_h / 2 - 14, x0 + 58, y0 + box_h / 2 + 14], fill=self._fade(self.accent, a))
            lines = wrap_text(b, f, self.content_w - 130)[:2]
            ty = y0 + box_h / 2 - (len(lines) * f.size * 1.1) / 2
            self._text_lines(draw, lines, f, 0, ty, self._fade(self.text, a), align="left", x_left=x0 + 86)

    def _s_diagram(self, img, draw, scene, t, dur):
        y = self._headline(draw, scene["on_screen"])
        nodes = scene.get("nodes") or []
        n = len(nodes)
        box_h, gap = 118, 64
        total_h = n * box_h + (n - 1) * gap
        y0 = max(y + 30, int(self.H * 0.42 - total_h / 2))
        reveal = max(dur * 0.65, 1.0)
        f = self.fonts.get("bold", 44)
        current = -1
        for i in range(n):
            if t >= i * reveal / max(n, 1):
                current = i
        for i, label in enumerate(nodes):
            a = ease_out((t - i * reveal / max(n, 1)) / 0.45)
            if a <= 0:
                continue
            yy = y0 + i * (box_h + gap)
            w = min(self.content_w - 100, max(420, f.getlength(label) + 90))
            x0 = (self.W - w) / 2
            outline = self.accent if i == current else None
            self._pill(draw, [x0, yy, x0 + w, yy + box_h], fill=self._fade(self.panel, a), radius=28,
                       outline=outline, width=5)
            lines = wrap_text(label, f, w - 50)[:2]
            ty = yy + box_h / 2 - (len(lines) * f.size * 1.1) / 2
            self._text_lines(draw, lines, f, self.W / 2, ty, self._fade(self.text, a))
            if i < n - 1:
                ay0, ay1 = yy + box_h + 8, yy + box_h + gap - 8
                la = ease_out((t - (i + 0.5) * reveal / max(n, 1)) / 0.4)
                if la > 0:
                    mid = self.W / 2
                    draw.line([(mid, ay0), (mid, ay0 + (ay1 - ay0 - 14) * la)], fill=self._fade(self.accent2, la), width=8)
                    if la > 0.9:
                        draw.polygon([(mid - 16, ay1 - 16), (mid + 16, ay1 - 16), (mid, ay1)], fill=self.accent2)

    def _s_compare(self, img, draw, scene, t, dur):
        y = self._headline(draw, scene["on_screen"])
        col_w = (self.content_w - 40) / 2
        xl, xr = self.margin, self.margin + col_w + 40
        fh, fi = self.fonts.get("bold", 42), self.fonts.get("regular", 38)
        y += 20
        for x, title, items, color, side in ((xl, scene.get("left_title", "A"), scene.get("left", []), self.muted, 0),
                                             (xr, scene.get("right_title", "B"), scene.get("right", []), self.accent, 1)):
            a = ease_out((t - 0.1 * side) / 0.5)
            self._pill(draw, [x, y, x + col_w, y + 90], fill=self._fade(self.panel, a), radius=24)
            draw.rounded_rectangle([x, y, x + col_w, y + 10], radius=5, fill=self._fade(color, a))
            tl = wrap_text(title, fh, col_w - 30)[:1]
            self._text_lines(draw, tl, fh, x + col_w / 2, y + 28, self._fade(self.text, a))
            yy = y + 120
            for j, item in enumerate(items):
                ia = ease_out((t - 0.5 - j * 0.6 - 0.2 * side) / 0.45)
                if ia <= 0:
                    continue
                lines = wrap_text(item, fi, col_w - 40)[:2]
                hh = len(lines) * fi.size * 1.15 + 36
                self._pill(draw, [x, yy, x + col_w, yy + hh], fill=self._fade(self.panel, ia * 0.8), radius=20)
                self._text_lines(draw, lines, fi, x + col_w / 2, yy + 18, self._fade(self.text, ia))
                yy += hh + 16
        # VS badge
        cx, cy = self.W / 2, y + 45
        draw.ellipse([cx - 40, cy - 40, cx + 40, cy + 40], fill=self.accent2)
        fv = self.fonts.get("bold", 34)
        draw.text((cx - fv.getlength("VS") / 2, cy - 20), "VS", font=fv, fill=(20, 20, 20))

    def _s_code(self, img, draw, scene, t, dur):
        y = self._headline(draw, scene["on_screen"])
        code = scene.get("code") or []
        f = self.fonts.get("mono", 40)
        y += 10
        panel_h = 70 + max(len(code), 3) * 58 + 40
        self._pill(draw, [self.margin, y, self.W - self.margin, y + panel_h], fill=(16, 18, 24), radius=28)
        for i, c in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
            draw.ellipse([self.margin + 30 + i * 36, y + 24, self.margin + 52 + i * 36, y + 46], fill=c)
        total_chars = sum(len(l) for l in code)
        shown = int(total_chars * ease_in_out(t / max(dur * 0.7, 1.0)))
        yy = y + 80
        for i, line in enumerate(code):
            take = max(0, min(len(line), shown))
            shown -= take
            draw.text((self.margin + 30, yy), f"{i + 1:>2}", font=f, fill=self.muted)
            draw.text((self.margin + 100, yy), line[:take], font=f, fill=self.accent if i % 2 == 0 else self.text)
            if 0 < take < len(line) or (take == len(line) and shown == 0 and i == len(code) - 1 and int(t * 2) % 2 == 0):
                cx = self.margin + 100 + f.getlength(line[:take])
                draw.rectangle([cx + 2, yy + 4, cx + 20, yy + f.size], fill=self.accent)
            yy += 58

    def _s_fact(self, img, draw, scene, t, dur):
        cx, cy = self.W / 2, int(self.H * 0.40)
        for k in range(3):
            r = 230 + 90 * k + 40 * math.sin(t * 2.2 + k)
            col = lerp_rgb(self.bg_mid, self.accent2 if k % 2 else self.accent, 0.35 - 0.1 * k)
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=col, width=6)
        text = scene["on_screen"]
        font = self._fit_font(text, "bold", 90, self.content_w - 40, 52)
        lines = wrap_text(text, font, self.content_w - 40)
        a = ease_out(t / 0.5)
        h = font.size * 1.1
        y = cy - len(lines) * h / 2
        for i, line in enumerate(lines):
            w = font.getlength(line)
            draw.text(((self.W - w) / 2, y + i * h + (1 - a) * 30), line, font=font, fill=self._fade(self.text, a))

    def _s_payoff(self, img, draw, scene, t, dur):
        text = scene["on_screen"]
        font = self._fit_font(text, "bold", 86, self.content_w, 50)
        lines = wrap_text(text, font, self.content_w)
        a = ease_out(t / 0.5)
        y = 380
        h = font.size * 1.1
        for i, line in enumerate(lines):
            w = font.getlength(line)
            draw.text(((self.W - w) / 2, y + i * h + (1 - a) * 30), line, font=font, fill=self._fade(self.text, a))
        y2 = y + len(lines) * h + 60
        bw = int(self.content_w * 0.4)
        draw.rounded_rectangle([(self.W - bw) / 2, y2, (self.W + bw) / 2, y2 + 10], radius=5, fill=self.accent)
        a2 = ease_out((t - 0.8) / 0.6)
        if a2 > 0:
            f2 = self.fonts.get("regular", 44)
            self._text_lines(draw, wrap_text(self.cfg.brand.cta, f2, self.content_w), f2, self.W / 2, y2 + 50,
                             self._fade(self.muted, a2))

    SCENES = {"title": _s_title, "explain": _s_explain, "diagram": _s_diagram, "compare": _s_compare,
              "code": _s_code, "fact": _s_fact, "payoff": _s_payoff}

    # ------------------------------------------------------------------ frame
    def frame(self, scene: dict, t_local: float, scene_dur: float, t_global: float, progress: float,
              label: str, caption: dict | None) -> Image.Image:
        img = self.background(t_global)
        draw = ImageDraw.Draw(img)
        self._top(draw, progress, label)
        self.SCENES.get(scene["type"], Renderer._s_explain)(self, img, draw, scene, t_local, scene_dur)
        self._captions(draw, caption, t_global)
        self._brand(draw)
        return img
