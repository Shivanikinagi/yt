"""Original anime-style and cartoon characters drawn with Pillow, plus the voice each one speaks with.

Videos are a conversation between two of them: a host who explains and a guest who asks and reacts.
Each character is a chibi bust (big head, shoulders, arms) drawn at 3x and downsampled for smooth
edges. It talks (the mouth follows the word timings), blinks, breathes and strikes a pose per scene
type: waving on the title and payoff, pointing at the panel while explaining, hand on chin for a
comparison, arms up for a big fact. Sprites are cached by pose state, so most video frames only
cost a paste. All characters are original designs; none imitate an existing franchise.
"""
from __future__ import annotations

import math
from collections import OrderedDict

from PIL import Image, ImageDraw, ImageFilter

SS = 3                      # supersampling factor
W, H = 320, 360             # sprite size in output pixels
OUTLINE = (22, 26, 31)
MOUTH = (96, 30, 38)
TONGUE = (232, 110, 120)
BLUSH = (255, 128, 140)

# voice: Piper voice (offline default) and Edge voice (opt-in engine); pitch > 1 = higher, cartoonier.
CAST = {
    "yuki": dict(kind="anime", name="YUKI", skin=(250, 222, 205), hair=(206, 200, 236), iris=(124, 92, 214),
                 style="long", glasses=True, outfit=(44, 54, 96), collar="sailor", color=(190, 170, 255),
                 voice=dict(piper="en_US-lessac-medium", edge="en-US-AvaNeural", pitch=1.04)),
    "sora": dict(kind="anime", name="SORA", skin=(247, 214, 190), hair=(44, 66, 132), iris=(64, 156, 232),
                 style="spiky", ahoge=True, outfit=(226, 120, 44), collar="shirt", color=(255, 170, 80),
                 voice=dict(piper="en_US-joe-medium", edge="en-US-BrianNeural", pitch=1.08)),
    "hana": dict(kind="anime", name="HANA", skin=(250, 222, 205), hair=(240, 140, 182), iris=(62, 168, 122),
                 style="long", clip=True, ahoge=True, outfit=(236, 238, 244), collar="sailor", color=(255, 140, 190),
                 voice=dict(piper="en_US-amy-medium", edge="en-US-EmmaNeural", pitch=1.06)),
    "ren": dict(kind="anime", name="REN", skin=(236, 200, 170), hair=(32, 30, 40), iris=(204, 62, 62),
                style="spiky", outfit=(36, 40, 52), collar="shirt", color=(240, 96, 96),
                voice=dict(piper="en_GB-alan-medium", edge="en-GB-RyanNeural", pitch=1.0)),
    "nova": dict(kind="human", name="NOVA", skin=(241, 196, 162), hair=(58, 36, 30), style="bob", glasses=True,
                 outfit=(176, 78, 92), color=(255, 150, 120),
                 voice=dict(piper="en_US-kristin-medium", edge="en-US-JennyNeural", pitch=1.0)),
    "kai": dict(kind="human", name="KAI", skin=(170, 116, 84), hair=(26, 22, 24), style="spiky", headphones=True,
                outfit=(58, 108, 186), color=(100, 180, 255),
                voice=dict(piper="en_US-ryan-medium", edge="en-US-GuyNeural", pitch=1.0)),
    "bit": dict(kind="robot", name="BIT", body=(214, 222, 230), trim=(146, 158, 172), color=(120, 210, 255),
                voice=dict(piper="en_US-ryan-medium", edge="en-US-ChristopherNeural", pitch=0.92)),
    "mochi": dict(kind="cat", name="MOCHI", fur=(244, 196, 132), fur2=(255, 241, 222), outfit=(88, 150, 104),
                  color=(255, 200, 120), voice=dict(piper="en_US-kristin-medium", edge="en-US-AnaNeural", pitch=1.18)),
}
POSES = {"title": "wave", "explain": "point", "diagram": "point", "code": "point", "compare": "think",
         "fact": "wow", "payoff": "wave"}
LOOK = {"point": (1.0, -0.8), "think": (-0.7, -1.0), "wow": (0.0, 0.0), "wave": (0.5, 0.2), "idle": (0.5, 0.2)}


def voice_for(key: str) -> dict:
    return dict(CAST.get(key, CAST["bit"])["voice"])


def pick_cast(cfg, index: int) -> list[str]:
    """The [host, guest] pair for the index-th video of the day (empty = plain narrator)."""
    pairs = [[k for k in pair if k in CAST] for pair in (cfg.video.get("cast_pairs") or [])]
    pairs = [pair[:2] for pair in pairs if pair]
    return pairs[index % len(pairs)] if pairs else []


def _mix(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


class _Pen:
    """ImageDraw wrapper that takes coordinates in sprite units (320x360) and draws at ss x."""

    def __init__(self, img: Image.Image, ss: int = SS):
        self.d = ImageDraw.Draw(img)
        self.ss = ss

    def _s(self, v):
        return v * self.ss

    def ellipse(self, cx, cy, rx, ry, fill, outline=OUTLINE, width=3.5):
        self.d.ellipse([self._s(cx - rx), self._s(cy - ry), self._s(cx + rx), self._s(cy + ry)], fill=fill,
                       outline=outline, width=int(width * self.ss) if outline else 0)

    def rrect(self, x0, y0, x1, y1, r, fill, outline=OUTLINE, width=3.5):
        self.d.rounded_rectangle([self._s(x0), self._s(y0), self._s(x1), self._s(y1)], radius=self._s(r), fill=fill,
                                 outline=outline, width=int(width * self.ss) if outline else 0)

    def polygon(self, pts, fill, outline=OUTLINE, width=3.5):
        self.d.polygon([(self._s(x), self._s(y)) for x, y in pts], fill=fill, outline=outline,
                       width=int(width * self.ss) if outline else 0)

    def line(self, pts, fill, width):
        pts = [(self._s(x), self._s(y)) for x, y in pts]
        self.d.line(pts, fill=fill, width=int(width * self.ss), joint="curve")
        r = width * self.ss / 2
        for x, y in (pts[0], pts[-1]):
            self.d.ellipse([x - r, y - r, x + r, y + r], fill=fill)

    def arc(self, cx, cy, rx, ry, start, end, fill, width):
        self.d.arc([self._s(cx - rx), self._s(cy - ry), self._s(cx + rx), self._s(cy + ry)], start, end,
                   fill=fill, width=int(width * self.ss))

    def chord(self, cx, cy, rx, ry, start, end, fill, outline=OUTLINE, width=3.5):
        self.d.chord([self._s(cx - rx), self._s(cy - ry), self._s(cx + rx), self._s(cy + ry)], start, end, fill=fill,
                     outline=outline, width=int(width * self.ss) if outline else 0)

    def capsule(self, p0, p1, r, fill, outline=OUTLINE, width=3.5):
        """A limb: outlined round-capped thick line."""
        if outline:
            self.line([p0, p1], outline, 2 * (r + width))
        self.line([p0, p1], fill, 2 * r)


class Host:
    def __init__(self, key: str, accent, seed: int = 0, scale: float = 1.0, cache_size: int = 96,
                 tall: bool = False):
        self.key = key if key in CAST else "bit"
        self.spec = CAST[self.key]
        self.name = self.spec["name"]
        # tall = a longer torso for close-ups, so the body reaches the bottom of the frame
        self.h = H + 120 if tall else H
        self.body_bottom = self.h + 80
        self.size = (int(W * scale), int(self.h * scale))
        self.ss = max(SS, math.ceil(scale))           # draw at least at output resolution
        self.cache_size = cache_size
        self.accent = tuple(accent)
        self.top = self.spec.get("outfit") or _mix((44, 50, 60), self.accent, 0.55)
        self.color = self.spec.get("color", self.accent)
        self.blink_offset = (seed % 1000) / 1000 * 3.0
        self._cache: OrderedDict[tuple, Image.Image] = OrderedDict()

    # ------------------------------------------------------------------ public
    def sprite(self, t: float, pose: str, mouth: int, look: tuple | None = None) -> Image.Image:
        blink = (t + self.blink_offset) % 3.7 < 0.13
        wave = int(t * 7) % 4 if pose == "wave" else 0
        bulb = int(t * 2) % 2 if self.spec["kind"] == "robot" else 0
        key = (pose, max(0, min(3, mouth)), blink, wave, bulb, look or LOOK.get(pose, LOOK["idle"]))
        img = self._cache.get(key)
        if img is None:
            img = self._draw(*key)
            self._cache[key] = img
            if len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
        else:
            self._cache.move_to_end(key)
        return img

    # ------------------------------------------------------------------ drawing
    def _hand_color(self):
        kind = self.spec["kind"]
        return self.spec["trim"] if kind == "robot" else self.spec["fur2"] if kind == "cat" else self.spec["skin"]

    def _arms(self, p: _Pen, pose: str, wave: int, front: bool):
        """Idle arms hang behind the face; gesturing arms are drawn in front of it."""
        left_sh, right_sh = (92, 296), (228, 296)
        sleeve, hand = (self.spec["body"] if self.spec["kind"] == "robot" else self.top), self._hand_color()
        idle_l, idle_r = (80, 392), (240, 392)
        targets = {"idle": (idle_l, idle_r), "point": (idle_l, (288, 176)), "think": ((146, 250), idle_r),
                   "wow": ((44, 178), (276, 178)), "wave": (idle_l, (282 + (-10, 0, 10, 0)[wave], 162))}
        lh, rh = targets.get(pose, targets["idle"])
        for sh, h, idle in ((left_sh, lh, idle_l), (right_sh, rh, idle_r)):
            if (h == idle) == front:
                continue
            p.capsule(sh, h, 21, sleeve)
            if h != idle:
                p.ellipse(h[0], h[1], 21, 21, hand)
                if pose == "point" and h == rh:
                    p.capsule((h[0] + 6, h[1] - 10), (h[0] + 20, h[1] - 38), 7, hand)
                if pose == "wave":
                    p.ellipse(h[0] - 18, h[1] + 6, 8, 8, hand)

    def _torso(self, p: _Pen):
        kind = self.spec["kind"]
        if kind == "robot":
            p.rrect(64, 262, 256, self.body_bottom, 46, self.spec["body"])
            p.rrect(112, 292, 208, 352, 14, (24, 32, 40))
            p.ellipse(160, 322, 12, 12, self.accent, outline=None)
            p.rrect(140, 238, 180, 270, 6, self.spec["trim"])          # neck
            return
        p.rrect(58, 262, 262, self.body_bottom, 70, self.top)
        p.chord(160, 266, 54, 30, 0, 180, _mix(self.top, (0, 0, 0), 0.25), width=3)   # neckline / hood edge
        p.line([(140, 292), (136, 330)], (236, 240, 244), 4)
        p.line([(180, 292), (184, 330)], (236, 240, 244), 4)

    def _eyes(self, p: _Pen, pose, blink, look, eye_y=162, gap=44, color=OUTLINE):
        lx, ly = look
        wow = pose == "wow"
        for ex in (160 - gap, 160 + gap):
            cx, cy = ex + lx * 5, eye_y + ly * 4
            if blink:
                p.arc(cx, cy + 2, 13, 8, 200, 340, color, 4)
                continue
            rx, ry = (15, 20) if wow else (12.5, 16.5)
            p.ellipse(cx, cy, rx, ry, color, outline=None)
            p.ellipse(cx - rx * 0.35, cy - ry * 0.4, rx * 0.38, rx * 0.38, (255, 255, 255), outline=None)
            p.ellipse(cx + rx * 0.35, cy + ry * 0.35, rx * 0.18, rx * 0.18, (255, 255, 255), outline=None)

    def _brows(self, p: _Pen, pose, color, eye_y=162, gap=44):
        lift = 8 if pose == "wow" else 0
        for side, ex in ((-1, 160 - gap), (1, 160 + gap)):
            tilt = 7 if pose == "think" and side > 0 else 0      # one brow up while thinking
            y = eye_y - 32 - lift
            p.line([(ex - 13, y), (ex + 13, y - tilt)], color, 5)

    def _mouth(self, p: _Pen, level: int, cy=208, cat=False):
        if level <= 0:
            if cat:
                p.arc(152, cy - 4, 8, 7, 10, 170, OUTLINE, 3.5)
                p.arc(168, cy - 4, 8, 7, 10, 170, OUTLINE, 3.5)
            else:
                p.arc(160, cy - 8, 16, 11, 20, 160, OUTLINE, 4)
            return
        h = (0, 8, 13, 18)[level]
        w = 14 + level * 2
        p.ellipse(160, cy, w, h, MOUTH, width=3)
        if level >= 2:
            p.chord(160, cy + h * 0.45, w * 0.62, h * 0.5, 0, 180, TONGUE, outline=None)

    def _cheeks(self, p: _Pen, base):
        for cx in (106, 214):
            p.ellipse(cx, 196, 14, 8, _mix(base, BLUSH, 0.45), outline=None)

    def _finish(self, img: Image.Image) -> Image.Image:
        if img.size == self.size:
            return img
        return img.convert("RGBa").resize(self.size, Image.LANCZOS).convert("RGBA")

    def back(self, blur: float = 26, dim: float = 0.55) -> Image.Image:
        """The character seen from behind, out of focus: the foreground shoulder in an over-the-shoulder shot."""
        if "_back" not in self.__dict__:
            img = Image.new("RGBA", (W * self.ss, self.h * self.ss), (0, 0, 0, 0))
            p = _Pen(img, self.ss)
            s, kind = self.spec, self.spec["kind"]
            body = s["body"] if kind == "robot" else self.top
            p.rrect(56, 262, 264, self.body_bottom, 70, body, outline=None)
            if kind == "robot":
                p.rrect(60, 52, 260, 246, 56, s["body"], outline=None)
                p.line([(160, 56), (160, 26)], s["trim"], 5)
                p.ellipse(160, 20, 11, 11, self.accent, outline=None)
            else:
                head = s["fur"] if kind == "cat" else s["hair"]
                if kind == "cat":
                    for sx in (-1, 1):
                        p.polygon([(160 + sx * 100, 120), (160 + sx * 92, 30), (160 + sx * 40, 78)], head, outline=None)
                p.ellipse(160, 160, 112, 108, head, outline=None)
                if s.get("style") == "long":
                    p.rrect(50, 150, 270, self.body_bottom - 40, 64, head, outline=None)
            img = self._finish(img)
            r, g, b, a = img.split()
            rgb = Image.merge("RGB", (r, g, b)).point(lambda v: int(v * dim))
            img = Image.merge("RGBA", (*rgb.split(), a))
            small = img.resize((img.width // 4, img.height // 4), Image.BILINEAR)
            small = small.convert("RGBa").filter(ImageFilter.GaussianBlur(blur / 4)).convert("RGBA")
            self._back = small.resize(img.size, Image.BILINEAR)
        return self._back

    def _draw(self, pose, mouth, blink, wave, bulb, look) -> Image.Image:
        img = Image.new("RGBA", (W * self.ss, self.h * self.ss), (0, 0, 0, 0))
        p = _Pen(img, self.ss)
        s, kind = self.spec, self.spec["kind"]
        self._arms(p, pose, wave, front=False)

        if kind == "anime":
            self._draw_anime(p, pose, mouth, blink, look)
            self._arms(p, pose, wave, front=True)
            return self._finish(img)

        if kind == "robot":
            self._torso(p)
            p.line([(160, 56), (160, 26)], s["trim"], 5)
            p.ellipse(160, 20, 11, 11, self.accent if bulb else _mix(self.accent, (30, 30, 30), 0.6))
            p.ellipse(58, 150, 18, 26, s["trim"])
            p.ellipse(262, 150, 18, 26, s["trim"])
            p.rrect(60, 52, 260, 246, 56, s["body"])
            p.rrect(82, 96, 238, 218, 38, (18, 26, 34))
            glow = self.accent
            lx, ly = look
            for ex in (122, 198):
                cx, cy = ex + lx * 6, 146 + ly * 5
                if blink:
                    p.rrect(cx - 14, cy - 2, cx + 14, cy + 3, 2, glow, outline=None)
                elif pose == "wow":
                    p.ellipse(cx, cy, 17, 17, glow, outline=None)
                else:
                    p.rrect(cx - 12, cy - 15, cx + 12, cy + 15, 10, glow, outline=None)
            if mouth > 0:
                for i in range(5):
                    bh = 3 + mouth * 4 * (0.55 + 0.45 * math.sin(i * 1.9 + mouth))
                    x = 136 + i * 12
                    p.rrect(x - 3.5, 190 - bh, x + 3.5, 190 + bh, 3, glow, outline=None)
            else:
                p.arc(160, 180, 20, 12, 20, 160, glow, 4)
            p.ellipse(100, 202, 9, 5, _mix((18, 26, 34), BLUSH, 0.5), outline=None)
            p.ellipse(220, 202, 9, 5, _mix((18, 26, 34), BLUSH, 0.5), outline=None)

        elif kind == "cat":
            fur, fur2 = s["fur"], s["fur2"]
            self._torso(p)
            for sx in (-1, 1):
                p.polygon([(160 + sx * 100, 120), (160 + sx * 92, 30), (160 + sx * 40, 78)], fur)
                p.polygon([(160 + sx * 86, 104), (160 + sx * 84, 52), (160 + sx * 54, 82)], _mix(fur, BLUSH, 0.5), outline=None)
            p.ellipse(160, 150, 112, 96, fur)
            p.polygon([(140, 58), (160, 92), (180, 58)], _mix(fur, (150, 90, 40), 0.35), outline=None)   # tabby stripe
            p.ellipse(160, 200, 46, 30, fur2, outline=None)
            self._eyes(p, pose, blink, look, eye_y=158, gap=46)
            self._cheeks(p, fur)
            p.polygon([(152, 184), (168, 184), (160, 193)], (226, 120, 130), width=2.5)
            for sx in (-1, 1):
                for k, dy in enumerate((-6, 4, 14)):
                    p.line([(160 + sx * 52, 196 + dy), (160 + sx * 98, 190 + dy + k * 4)], (120, 90, 70), 2.5)
            self._mouth(p, mouth, cy=210, cat=True)

        else:  # human
            skin, hair = s["skin"], s["hair"]
            if s.get("style") == "bob":
                p.ellipse(160, 168, 122, 118, hair)                      # hair behind the head
            if s.get("headphones"):
                p.arc(160, 150, 118, 116, 180, 360, (40, 44, 52), 12)     # band sits behind the hair
            self._torso(p)
            p.rrect(142, 236, 178, 272, 10, skin)                         # neck
            p.ellipse(62, 168, 16, 22, skin)
            p.ellipse(258, 168, 16, 22, skin)
            p.ellipse(160, 158, 102, 98, skin)
            if s.get("style") == "bob":
                p.chord(160, 128, 108, 84, 180, 360, hair)
                p.polygon([(96, 106), (150, 104), (118, 136)], hair, outline=None)
            else:
                p.chord(160, 136, 106, 90, 180, 360, hair)
                spikes = [(58, 136), (56, 84), (82, 92), (84, 40), (116, 66), (128, 14), (156, 56), (186, 10),
                          (198, 58), (234, 30), (234, 84), (264, 78), (262, 136)]
                p.polygon(spikes, hair)
            self._eyes(p, pose, blink, look)
            self._brows(p, pose, hair)
            self._cheeks(p, skin)
            self._mouth(p, mouth)
            if s.get("glasses"):
                for ex in (116, 204):
                    p.rrect(ex - 30, 140, ex + 30, 184, 14, None, outline=(40, 44, 52), width=4)
                p.line([(146, 158), (174, 158)], (40, 44, 52), 4)
            if s.get("headphones"):
                for sx in (-1, 1):
                    p.rrect(160 + sx * 112 - 18, 136, 160 + sx * 112 + 18, 196, 14, self.accent)

        self._arms(p, pose, wave, front=True)
        return self._finish(img)

    # ------------------------------------------------------------------ anime style
    @staticmethod
    def _face_outline(cx=160, cy=150, rx=96, ry=94):
        """Round skull tapering to a soft pointed chin."""
        pts = []
        for k in range(64):
            a = 2 * math.pi * k / 64
            sa, ca = math.sin(a), math.cos(a)
            if sa > 0:
                pts.append((cx + rx * ca * (1 - 0.24 * sa ** 2), cy + ry * sa * (1 + 0.15 * sa ** 6)))
            else:
                pts.append((cx + rx * ca, cy + ry * sa))
        return pts

    def _draw_anime(self, p: _Pen, pose, mouth, blink, look):
        s = self.spec
        skin, hair, iris = s["skin"], s["hair"], s["iris"]
        hair_dark = _mix(hair, (0, 0, 0), 0.22)
        if s["style"] == "long":
            p.rrect(50, 112, 270, min(430, self.body_bottom), 64, hair_dark)                        # hair falling behind the shoulders
            p.ellipse(160, 150, 120, 114, hair_dark)
        # torso with a school-uniform collar
        p.rrect(62, 266, 258, self.body_bottom, 64, self.top)
        p.rrect(143, 232, 177, 272, 10, skin)
        white = (246, 247, 250)
        if s.get("collar") == "sailor":
            p.polygon([(88, 272), (160, 344), (232, 272), (214, 264), (160, 318), (106, 264)], white, width=3)
            p.polygon([(146, 322), (174, 322), (168, 352), (152, 352)], self.accent, width=2.5)
        else:
            p.polygon([(128, 262), (160, 296), (140, 312)], white, width=3)
            p.polygon([(192, 262), (160, 296), (180, 312)], white, width=3)
            p.polygon([(152, 298), (168, 298), (174, 360), (160, 376), (146, 360)], self.accent, width=2.5)
        p.ellipse(66, 170, 11, 18, skin, width=3)
        p.ellipse(254, 170, 11, 18, skin, width=3)
        p.polygon(self._face_outline(), skin)
        # eyes
        lx, ly = look
        wow = pose == "wow"
        for side, ex in ((-1, 118), (1, 202)):
            cy = 172
            if blink:
                p.arc(ex, cy - 6, 22, 13, 20, 160, OUTLINE, 5)
                continue
            p.ellipse(ex, cy, 22, 27, white, outline=None)
            ix, iy = ex + lx * 5, cy + 2 + ly * 4
            irx, iry = (13, 19) if wow else (15, 21)
            p.ellipse(ix, iy, irx, iry, iris, outline=None)
            p.chord(ix, iy, irx, iry, 180, 360, _mix(iris, (0, 0, 0), 0.4), outline=None)
            p.ellipse(ix, iy + 3, 6 if wow else 7, 9 if wow else 10, (24, 20, 34), outline=None)
            p.ellipse(ix - 5, iy - 8, 6, 7, white, outline=None)
            p.ellipse(ix + 6, iy + 9, 3, 3, white, outline=None)
            p.arc(ex, cy + 2, 24, 29, 195, 345, OUTLINE, 6)                  # upper lash line
            p.line([(ex + side * 23, cy - 9), (ex + side * 32, cy - 16)], OUTLINE, 4)
        p.line([(160, 204), (157, 210)], _mix(skin, (120, 60, 50), 0.5), 2.5)
        for cx in (106, 214):
            p.ellipse(cx, 204, 15, 6, _mix(skin, BLUSH, 0.5), outline=None)
        if mouth <= 0:
            p.arc(160, 222, 11, 6, 20, 160, OUTLINE, 3.5)
        else:
            h, w = (0, 5, 8, 11)[mouth], 7 + mouth * 2
            p.ellipse(160, 226, w, h, MOUTH, width=2.5)
            if mouth >= 2:
                p.chord(160, 226 + h * 0.45, w * 0.6, h * 0.5, 0, 180, TONGUE, outline=None)
        # front hair: silhouette first (spikes or the back hair), then an unoutlined crown, then bangs whose
        # strand tips alone are stroked, so no straight seams cross the head
        if s["style"] != "long":
            spikes = [(56, 124), (36, 80), (78, 88), (66, 34), (112, 62), (120, 8), (152, 52), (190, 4), (198, 54),
                      (242, 24), (236, 78), (286, 72), (264, 124)]
            p.polygon(spikes, hair)
        p.chord(160, 140, 108, 104, 180, 360, hair, outline=None)
        tips = [(268, 152), (252, 158), (238, 106), (222, 150), (204, 102), (186, 156), (166, 104), (146, 152),
                (126, 102), (108, 148), (90, 106), (72, 158), (52, 152)]
        p.polygon([(52, 110), (268, 110)] + tips, hair, outline=None)
        p.line(tips, OUTLINE, 3)
        shine = _mix(hair, (255, 255, 255), 0.35)
        p.arc(160, 112, 66, 34, 205, 335, shine, 6)
        if s["style"] == "long":
            for sx in (-1, 1):                                                # side locks
                p.polygon([(160 + sx * 92, 118), (160 + sx * 70, 140), (160 + sx * 82, 262), (160 + sx * 104, 300),
                           (160 + sx * 110, 200)], hair)
        if s.get("ahoge"):
            p.arc(170, 40, 22, 26, 180, 300, hair, 7)
        if s.get("clip"):
            p.rrect(206, 98, 244, 112, 6, self.accent, width=2.5)
        if s.get("glasses"):
            for ex in (118, 202):
                p.rrect(ex - 32, 148, ex + 32, 196, 10, None, outline=(60, 56, 80), width=3.5)
            p.line([(150, 166), (170, 166)], (60, 56, 80), 3.5)
