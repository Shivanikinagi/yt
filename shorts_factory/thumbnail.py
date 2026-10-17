"""1280x720 thumbnail for long-form videos: the host's surprised close-up, 2-4 huge words, one badge.

Built like the thumbnails that win clicks in the trending tech charts: one face with a strong
expression, very few words in heavy type with one of them coloured, high contrast, nothing small.
"""
from __future__ import annotations

import random
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .characters import CAST, Host
from .visuals import AMBER, THEMES, Fonts, clean, hex_to_rgb, wrap_text

TW, TH = 1280, 720


def _background(accent, seed: int) -> Image.Image:
    rng = random.Random(seed)
    yy, xx = np.mgrid[0:TH, 0:TW].astype(np.float32)
    base = np.zeros((TH, TW, 3), np.float32)
    base += np.array((8, 10, 22), np.float32)
    r = np.sqrt(((xx - TW * 0.72) / (TW * 0.5)) ** 2 + ((yy - TH * 0.45) / (TH * 0.7)) ** 2)
    base += np.clip(1 - r, 0, 1)[..., None] ** 1.6 * np.array(accent, np.float32) * 0.55
    img = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB")
    discs = Image.new("RGB", (TW, TH))
    d = ImageDraw.Draw(discs)
    for _ in range(26):                                     # out-of-focus city lights
        rr = rng.uniform(14, 46)
        x, y = rng.uniform(TW * 0.35, TW), rng.uniform(0, TH * 0.8)
        c = rng.choice(((255, 196, 120), (130, 190, 255), tuple(accent)))
        d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=tuple(int(v * rng.uniform(0.2, 0.45)) for v in c))
    from PIL import ImageChops
    return ImageChops.add(img, discs.filter(ImageFilter.GaussianBlur(5)))


def make_thumbnail(script, cfg, out_path: Path, seed: int = 0) -> Path:
    theme = THEMES.get(script.theme, THEMES["neon"])
    accent = hex_to_rgb(theme["accent"])
    img = _background(accent, seed)
    cast = [k for k in (script.extra or {}).get("cast") or [] if k in CAST]
    if cast:
        host = Host(cast[0], accent, seed, scale=2.3, tall=True)
        sprite = host.sprite(0.4, "wow", 2, look=(-0.6, 0.0))
        glow = Image.new("L", sprite.size, 0)
        glow.paste(sprite.getchannel("A"))
        glow = glow.filter(ImageFilter.GaussianBlur(18))
        x, y = TW - sprite.width + 60, TH - int(sprite.height * 0.72)
        img.paste(Image.new("RGB", sprite.size, tuple(min(255, int(v * 1.2)) for v in accent)), (x, y), glow)
        img.paste(sprite, (x, y), sprite)
    fonts = Fonts(cfg)
    d = ImageDraw.Draw(img)
    words = clean((script.extra or {}).get("thumbnail_text") or script.title).upper()
    max_w = 700 if cast else 1160
    size = 168
    while size > 70:
        font = fonts.get("headline", size)
        lines = wrap_text(words, font, max_w)
        if len(lines) <= 3 and all(font.getlength(l) <= max_w for l in lines) and len(lines) * size * 1.02 <= 470:
            break
        size -= 6
    lines = lines[:3]
    total_h = len(lines) * font.size * 1.02
    y = (TH - total_h) / 2 + font.size * 0.82 + 20
    hot = len(lines) - 1                                   # the last line carries the punch, in colour
    for i, line in enumerate(lines):
        fill = AMBER if i == hot and len(lines) > 1 else (255, 255, 255)
        d.text((56, y + i * font.size * 1.02), line, font=font, fill=fill, anchor="ls",
               stroke_width=max(6, font.size // 16), stroke_fill=(0, 0, 0))
    subject = clean((script.extra or {}).get("subject") or script.category).upper()[:22]
    if subject:
        fb = fonts.get("mono_bold", 34)
        w = fb.getlength(subject) + 44
        d.rounded_rectangle([56, 44, 56 + w, 104], radius=14, fill=accent)
        d.text((56 + w / 2, 74), subject, font=fb, fill=(8, 10, 16), anchor="mm")
    out_path = Path(out_path)
    img.save(out_path, "JPEG", quality=90, optimize=True)
    return out_path
