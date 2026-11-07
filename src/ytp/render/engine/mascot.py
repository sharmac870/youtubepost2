"""The channel's presenter: a 2D cartoon host whose mouth moves with the voice, who blinks, raises an eyebrow on
emphasis, bobs while talking and waves at the end. Drawn with Pillow, so there is nothing to license or pay for.

Lip sync comes from the voiceover itself: the loudness of each video frame opens the mouth (four shapes), and
words with an o/u sound round it. That is how simple cartoon lip sync works, and it needs no extra engine.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw

BASE_W, BASE_H = 400, 520      # design units; drawn 2x and scaled down for smooth edges
SS = 2

SKIN = (196, 136, 98)
SKIN_SHADE = (172, 114, 80)
HAIR = (32, 24, 22)
EYE_WHITE = (250, 250, 248)
PUPIL = (36, 26, 22)
MOUTH = (110, 30, 40)
TONGUE = (226, 112, 120)
TEETH = (250, 248, 244)
OUTLINE = (24, 18, 18)


@dataclass(frozen=True)
class Pose:
    mouth: int = 0        # 0 closed smile, 1 slightly open, 2 open, 3 wide
    round: bool = False   # an "o" shape instead of a wide one
    blink: bool = False
    brow: int = 0         # 0 relaxed, 1 raised, 2 one brow up (curious)
    look: int = 0         # pupils: -1 left, 0 at the viewer, 1 right
    wave: int = -1        # -1 no wave, else 0..7: the raised hand's angle
    smile: int = 1        # 0 neutral, 1 smile, 2 big grin (only when the mouth is closed)


def _s(v: float) -> int:
    return round(v * SS)


def _box(cx, cy, w, h):
    return (_s(cx - w / 2), _s(cy - h / 2), _s(cx + w / 2), _s(cy + h / 2))


@lru_cache(maxsize=1024)
def draw(pose: Pose, shirt: tuple[int, int, int], height: int) -> Image.Image:
    """The host in one pose, as RGBA, `height` pixels tall."""
    img = Image.new("RGBA", (_s(BASE_W), _s(BASE_H)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    line = _s(4)
    dark = tuple(int(c * 0.72) for c in shirt)

    # Body and shoulders, with a collar.
    d.rounded_rectangle((_s(40), _s(352), _s(360), _s(600)), radius=_s(110), fill=shirt, outline=OUTLINE, width=line)
    # Neck, then the collar over it.
    d.rectangle((_s(170), _s(318), _s(230), _s(372)), fill=SKIN_SHADE)
    d.polygon([(_s(160), _s(356)), (_s(200), _s(404)), (_s(240), _s(356))], fill=dark, outline=OUTLINE)

    # Waving arm, raised from the right shoulder.
    if pose.wave >= 0:
        swing = 12 * math.sin(pose.wave / 8 * 2 * math.pi)
        sx, sy = 318, 400
        hx, hy = 352 + swing * 0.6, 262 - abs(swing) * 0.3
        dx, dy = hx - sx, hy - sy
        n = math.hypot(dx, dy) or 1
        px, py = -dy / n * 24, dx / n * 24
        d.polygon([(_s(sx + px), _s(sy + py)), (_s(hx + px), _s(hy + py)), (_s(hx - px), _s(hy - py)),
                   (_s(sx - px), _s(sy - py))], fill=shirt, outline=OUTLINE, width=line)
        d.ellipse(_box(hx, hy - 16, 50, 54), fill=SKIN, outline=OUTLINE, width=line)
        for i in range(4):  # fingers
            fx = hx - 19 + i * 12.5
            d.rounded_rectangle((_s(fx - 5), _s(hy - 60), _s(fx + 6), _s(hy - 26)), radius=_s(5), fill=SKIN,
                                outline=OUTLINE, width=_s(2))
        d.ellipse(_box(hx, hy - 16, 46, 40), fill=SKIN)
        d.ellipse(_box(hx - 28, hy - 6, 16, 30), fill=SKIN, outline=OUTLINE, width=_s(2))  # thumb

    # Ears, head.
    for ex in (92, 308):
        d.ellipse(_box(ex, 222, 40, 62), fill=SKIN, outline=OUTLINE, width=line)
    d.ellipse(_box(200, 205, 220, 262), fill=SKIN, outline=OUTLINE, width=line)

    # Hair: a swoop on top with a side part.
    d.chord(_box(200, 150, 232, 190), 180, 360, fill=HAIR)
    d.pieslice(_box(150, 120, 150, 110), 160, 330, fill=HAIR)
    d.pieslice(_box(240, 116, 170, 120), 190, 360, fill=HAIR)
    d.polygon([(_s(88), _s(170)), (_s(108), _s(110)), (_s(130), _s(160))], fill=HAIR)
    d.polygon([(_s(312), _s(170)), (_s(292), _s(110)), (_s(270), _s(160))], fill=HAIR)

    # Eyebrows.
    lift_l = {0: 0, 1: 12, 2: 2}[pose.brow]
    lift_r = {0: 0, 1: 12, 2: 16}[pose.brow]
    d.line((_s(140), _s(170 - lift_l), _s(180), _s(164 - lift_l)), fill=HAIR, width=_s(9))
    d.line((_s(220), _s(164 - lift_r), _s(260), _s(170 - lift_r)), fill=HAIR, width=_s(9))

    # Eyes.
    for cx in (160, 240):
        if pose.blink:
            d.arc(_box(cx, 200, 40, 18), 0, 180, fill=OUTLINE, width=_s(5))
        else:
            d.ellipse(_box(cx, 198, 40, 46), fill=EYE_WHITE, outline=OUTLINE, width=_s(3))
            px = cx + 6 * pose.look
            d.ellipse(_box(px, 202, 20, 22), fill=PUPIL)
            d.ellipse(_box(px + 4, 197, 7, 7), fill=EYE_WHITE)

    # Nose and cheeks.
    d.arc(_box(200, 236, 26, 22), 20, 160, fill=SKIN_SHADE, width=_s(5))
    for cx in (138, 262):
        d.ellipse(_box(cx, 252, 30, 16), fill=(214, 128, 110))

    # Mouth.
    cy = 282
    if pose.mouth == 0:
        if pose.smile == 0:
            d.line((_s(178), _s(cy), _s(222), _s(cy)), fill=OUTLINE, width=_s(5))
        else:
            w, h = (64, 26) if pose.smile == 1 else (84, 40)
            if pose.smile == 2:
                d.chord(_box(200, cy - 8, w, h * 1.6), 0, 180, fill=MOUTH, outline=OUTLINE, width=_s(4))
                d.chord(_box(200, cy - 8, w - 12, 18), 0, 180, fill=TEETH)
            else:
                d.arc(_box(200, cy - 10, w, h), 15, 165, fill=OUTLINE, width=_s(5))
    else:
        if pose.round:
            w, h = (26, 30) if pose.mouth == 1 else (34, 42) if pose.mouth == 2 else (40, 50)
        else:
            w, h = (50, 16) if pose.mouth == 1 else (60, 30) if pose.mouth == 2 else (68, 44)
        d.ellipse(_box(200, cy, w, h), fill=MOUTH, outline=OUTLINE, width=_s(4))
        if pose.mouth >= 2:
            d.chord(_box(200, cy + h * 0.32, w * 0.62, h * 0.5), 180, 360, fill=TONGUE)
            if not pose.round:
                d.chord(_box(200, cy - h * 0.36, w * 0.7, h * 0.36), 0, 180, fill=TEETH)

    scale = height / BASE_H
    return img.resize((max(1, int(BASE_W * scale)), height), Image.LANCZOS)


class Lipsync:
    """Mouth shape per video frame from the voiceover's loudness and the word being spoken."""

    def __init__(self, samples: np.ndarray, sample_rate: int, words, fps: int = 30):
        hop = sample_rate // fps
        frames = max(1, len(samples) // hop)
        rms = np.sqrt(np.mean(samples[: frames * hop].reshape(frames, hop) ** 2, axis=1)) if len(samples) >= hop \
            else np.zeros(1)
        peak = float(np.percentile(rms, 95)) or 1.0
        level = np.clip(rms / peak, 0, 1)
        # Smooth a little so the mouth doesn't flicker, but keep it quick enough to follow syllables.
        smooth = np.convolve(level, [0.25, 0.5, 0.25], mode="same")
        self.level = smooth
        self.fps = fps
        self.words = list(words)

    def level_at(self, t: float) -> float:
        i = int(t * self.fps)
        return float(self.level[i]) if 0 <= i < len(self.level) else 0.0

    def word_at(self, t: float):
        for w in self.words:
            if w.start <= t < w.end:
                return w
        return None

    def mouth(self, t: float) -> tuple[int, bool]:
        level = self.level_at(t)
        word = self.word_at(t)
        if word is None or level < 0.12:
            return 0, False
        shape = 1 if level < 0.35 else 2 if level < 0.7 else 3
        # Round the mouth on o/u sounds, roughly in the middle of the word.
        text = word.text.lower()
        middle = word.start + (word.end - word.start) * 0.5
        rounded = any(v in text for v in ("o", "u", "w")) and abs(t - middle) < (word.end - word.start) * 0.3
        return shape, rounded


def pose_at(t: float, sync: Lipsync, tone: str = "curious", waving: bool = False, seed: int = 0) -> Pose:
    mouth, rounded = sync.mouth(t)
    # Blink about every 2.5-4.5 s, at an irregular point in each stretch, like a person does.
    period = 2.5 + (seed % 5) * 0.5
    cycle = int(t / period)
    at = ((cycle * 7919 + seed) % 97) / 97 * (period - 0.3)
    blink = at <= (t % period) < at + 0.12
    level = sync.level_at(t)
    brow = 1 if tone in ("excited", "funny") and level > 0.8 else 2 if tone == "curious" and level > 0.85 else 0
    look = 0 if (int(t / 1.7) + seed) % 5 else (1 if seed % 2 else -1)
    smile = 2 if tone in ("excited", "funny") else 0 if tone == "serious" else 1
    wave = int(t * 8) % 8 if waving else -1
    return Pose(mouth, rounded, blink, brow, look, wave, smile)


def bob(t: float, level: float) -> tuple[int, float]:
    """A small talking bob (pixels up/down) and tilt (degrees)."""
    return int(-6 * level + 3 * math.sin(t * 2.2)), 2.0 * math.sin(t * 1.3)
