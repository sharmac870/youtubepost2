"""Renders the vertical 1080x1920 Short (or a 1920x1080 landscape video): dark cyber backdrop, fast cuts, big on-screen captions and
word-by-word animated subtitles synced to the voiceover, over a synthesized music bed.

Adapted from the motion-graphics engine in sharmac870/Advert (content_engine/video.py): frames are
drawn with Pillow and piped straight into the ffmpeg binary bundled with imageio-ffmpeg, so there is
no system ffmpeg to install and nothing paid or licensed involved. The music and sound effects are
synthesized here, so there is nothing to clear for copyright either.
"""
from __future__ import annotations

import logging
import math
import subprocess
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import mascot as host
from .footage import ClipReader, shade
from .tts import SAMPLE_RATE, Voiceover, Word, decode, write_wav

log = logging.getLogger(__name__)

ASSETS = Path(__file__).resolve().parent / "assets"
WIDTH, HEIGHT, FPS = 1080, 1920, 30
TAIL = 0.7  # Seconds held after the last word.


@dataclass(frozen=True)
class Layout:
    """Where everything goes. Every position and size in the renderer comes from here."""
    width: int
    height: int
    left: int
    right: int
    header_y: int
    tag_y: int
    hook_top: int
    scene_center: int
    scene_min_top: int
    caption_y: int
    outro_top: int
    hook_size: int
    hook_lines: int
    scene_size: int
    stat_size: int
    scene_lines: int
    outro_size: int
    outro_lines: int
    subtitle_size: int
    glows: tuple
    crf: int = 21

    @property
    def column(self) -> int:
        return self.right - self.left

    @property
    def center(self) -> int:
        return (self.left + self.right) // 2


# Shorts overlays its buttons on the right and its title on the bottom; keep text clear of both.
PORTRAIT = Layout(1080, 1920, 80, 940, header_y=150, tag_y=420, hook_top=560, scene_center=820, scene_min_top=500,
                  caption_y=1240, outro_top=560, hook_size=104, hook_lines=6, scene_size=116, stat_size=150,
                  scene_lines=4, outro_size=88, outro_lines=5, subtitle_size=84,
                  glows=((900, 380, 700, 1.0), (120, 1500, 650, 0.6)))
LANDSCAPE = Layout(1920, 1080, 140, 1780, header_y=60, tag_y=200, hook_top=300, scene_center=470, scene_min_top=260,
                   caption_y=850, outro_top=280, hook_size=110, hook_lines=4, scene_size=104, stat_size=140,
                   scene_lines=3, outro_size=84, outro_lines=3, subtitle_size=72,
                   glows=((1550, 250, 800, 1.0), (250, 900, 700, 0.6)), crf=23)


def layout_for(fmt: str) -> Layout:
    return LANDSCAPE if fmt == "video" else PORTRAIT

INK = (5, 8, 15)
WHITE = (240, 245, 250)
MUTED = (150, 165, 185)
BLACK = (0, 0, 0)
RED = (255, 59, 76)
YELLOW = (255, 214, 10)


@dataclass(frozen=True)
class Theme:
    badge: str
    accent: tuple[int, int, int]
    glow: tuple[int, int, int]
    texture: str = "hex"  # What scrolls behind the text: hex, dots, chart, stripes or stars.


THEMES = {
    "cyber": Theme("CYBER ALERT", (0, 255, 156), (0, 90, 70), "hex"),
    "tech": Theme("TECH & AI", (64, 196, 255), (10, 50, 110), "dots"),
    "business": Theme("MONEY MOVES", (255, 196, 0), (90, 60, 0), "chart"),
    "viral": Theme("BREAKING", (255, 70, 90), (100, 10, 25), "stripes"),
    "violet": Theme("DEEP DIVE", (190, 130, 255), (60, 20, 110), "stars"),
}
LOOKS = tuple(THEMES)


def theme_for(niche: str, look: str = "", badge: str = "") -> Theme:
    """The topic's look (colours and background) unless one was picked, with the given badge text if any.
    For custom topics the script writer suggests a look and badge to fit the subject."""
    theme = THEMES.get(look) or THEMES.get(niche, THEMES["cyber"])
    if badge:
        theme = replace(theme, badge=badge.upper())
    elif niche == "custom" and not look:
        theme = replace(theme, badge="TRENDING")
    return theme

VISUAL_LABELS = {
    "alert": "WHAT HAPPENED",
    "stat": "BY THE NUMBERS",
    "list": "KEY FACTS",
    "explainer": "HOW IT WORKS",
    "quote": "ON THE RECORD",
    "warning": "WHAT TO DO NOW",
}


# ----- Drawing helpers (from Advert) -----


@lru_cache(maxsize=None)
def font(kind: str, size: int, weight: int = 700) -> ImageFont.FreeTypeFont:
    loaded = ImageFont.truetype(str(ASSETS / f"{kind}.ttf"), size)
    loaded.set_variation_by_axes([weight])
    return loaded


def clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def ease(t: float, start: float, duration: float = 0.35) -> float:
    return 1 - (1 - clamp((t - start) / duration)) ** 3


def wrap(text: str, typeface, width: int) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if typeface.getlength(trial) <= width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    return lines + [current] if current else lines


def fit(text: str, kind: str, weight: int, size: int, minimum: int, width: int, max_lines: int):
    """The largest size at which the text fits, since its length isn't known in advance."""
    while True:
        typeface = font(kind, size, weight)
        lines = wrap(text, typeface, width)
        too_wide = any(typeface.getlength(line) > width for line in lines)
        if size <= minimum or (len(lines) <= max_lines and not too_wide):
            return typeface
        size -= 4


@lru_cache(maxsize=512)
def rounded_mask(width: int, height: int, radius: int) -> Image.Image:
    scale = 3
    large = Image.new("L", (width * scale, height * scale), 0)
    ImageDraw.Draw(large).rounded_rectangle((0, 0, width * scale - 1, height * scale - 1), radius * scale, fill=255)
    return large.resize((width, height), Image.LANCZOS)


def box(image: Image.Image, x0, y0, x1, y1, radius, color, alpha: float = 1.0):
    size = (int(x1 - x0), int(y1 - y0))
    if size[0] <= 0 or size[1] <= 0 or alpha <= 0:
        return
    mask = rounded_mask(*size, radius)
    if alpha < 1:
        mask = mask.point(lambda v: int(v * alpha))
    image.paste(color, (int(x0), int(y0)), mask)


# ----- Background -----


@lru_cache(maxsize=8)
def backdrop(glow: tuple[int, int, int], layout: Layout = PORTRAIT, retro: bool = True) -> Image.Image:
    """Near-black with a coloured glow, a grid and scanlines. Taller than the frame, for a slow drift."""
    height = layout.height + 400
    y, x = np.mgrid[0:height, 0:layout.width]
    base = np.array(INK, dtype=float)[None, None] + np.zeros((height, layout.width, 3))
    for cx, cy, r, s in layout.glows:
        g = np.exp(-(((x - cx) / r) ** 2 + ((y - cy) / r) ** 2))
        base += g[..., None] * np.array(glow, dtype=float) * s
    if retro:  # The terminal look: a grid and scanlines. Other looks keep a clean gradient.
        grid = ((x % 72 == 0) | (y % 72 == 0)).astype(float)
        base += grid[..., None] * np.array([10, 14, 18])
        base *= (1 - 0.18 * (y % 4 == 0))[..., None]
    return Image.fromarray(np.clip(base, 0, 255).astype("uint8"), "RGB")


@lru_cache(maxsize=8)
def code_rain(accent: tuple[int, int, int], layout: Layout = PORTRAIT) -> Image.Image:
    """A tall, faint texture of hex characters that scrolls behind everything."""
    rng = np.random.default_rng(42)
    height = layout.height * 2
    layer = Image.new("L", (layout.width, height), 0)
    draw = ImageDraw.Draw(layer)
    typeface = font("mono", 26, 500)
    for column in range(0, layout.width, 54):
        y = int(rng.integers(0, 400))
        while y < height:
            run = int(rng.integers(4, 18))
            for _ in range(run):
                draw.text((column, y), rng.choice(list("0123456789ABCDEF")), font=typeface, fill=int(rng.integers(10, 42)))
                y += 32
            y += int(rng.integers(60, 500))
    return layer


@lru_cache(maxsize=8)
def texture_layer(kind: str, layout: Layout = PORTRAIT) -> Image.Image:
    """The scrolling background for each look, two frames tall so it can loop."""
    if kind == "hex":
        return code_rain((0, 0, 0), layout)
    rng = np.random.default_rng(11)
    width, height = layout.width, layout.height * 2
    layer = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(layer)
    if kind == "dots":  # A circuit-board field of dots with a few traces.
        for x in range(30, width, 60):
            for y in range(30, height, 60):
                r = 3 if rng.random() < 0.85 else 6
                draw.ellipse((x - r, y - r, x + r, y + r), fill=int(rng.integers(18, 48)))
        for _ in range(40):
            x, y = int(rng.integers(0, width // 60)) * 60 + 30, int(rng.integers(0, height // 60)) * 60 + 30
            dx, dy = (int(rng.integers(2, 6)) * 60, 0) if rng.random() < 0.5 else (0, int(rng.integers(2, 6)) * 60)
            draw.line((x, y, x + dx, y + dy), fill=40, width=3)
    elif kind == "chart":  # Rising market lines and faint bars.
        for band in range(0, height, 420):
            points, y = [], band + 360
            for x in range(-40, width + 80, 80):
                y = max(band + 40, min(band + 400, y - int(rng.integers(-40, 70))))
                points.append((x, y))
            draw.line(points, fill=46, width=5, joint="curve")
            for x, top in points[::2]:
                if top + 30 < band + 410:
                    draw.rectangle((x - 14, top + 30, x + 14, band + 410), fill=14)
    elif kind == "stripes":  # Diagonal hazard stripes, like a breaking-news bumper.
        for offset in range(-height, width + height, 140):
            draw.polygon([(offset, 0), (offset + 50, 0), (offset + 50 + height, height), (offset + height, height)], fill=22)
    else:  # stars: a slow starfield with a few bright ones.
        for _ in range(int(width * height / 9000)):
            x, y = int(rng.integers(0, width)), int(rng.integers(0, height))
            r = float(rng.choice([1, 1, 1, 2, 2, 3]))
            draw.ellipse((x - r, y - r, x + r, y + r), fill=int(rng.integers(30, 110)))
    return layer


# ----- Timeline -----


@dataclass
class Shot:
    kind: str  # hook, scene, outro
    start: float
    end: float
    caption: str
    visual: str = "alert"
    number: int = 0
    tone: str = "curious"
    broll: tuple = ()


@dataclass
class Beat:
    """A stretch of one shot that shows one stock clip. Shots are cut into 2-3 s beats, the way people edit."""
    start: float
    end: float
    clip: str | None = None     # local mp4; None = the drawn background
    offset: float = 0.0         # where in the clip to start
    zoom_in: bool = True


def build_shots(script, voice: Voiceover) -> list[Shot]:
    segments = voice.segments
    owners = getattr(script, "segment_scene", None)
    if owners and len(owners) == len(segments):
        # Voiced sentence by sentence: a scene runs from its first sentence to the next scene's first sentence.
        total = voice.duration + TAIL
        firsts = [i for i, o in enumerate(owners) if i == 0 or owners[i - 1] != o]
        shots = []
        for n, i in enumerate(firsts):
            start = 0.0 if i == 0 else segments[i].start
            end = segments[firsts[n + 1]].start if n + 1 < len(firsts) else total
            owner = owners[i]
            if owner == -1:
                shots.append(Shot("hook", start, end, script.hook, tone="excited"))
            elif owner == -2:
                shots.append(Shot("outro", start, end, script.outro, tone="funny"))
            else:
                scene = script.script_scenes[owner]
                shots.append(Shot("scene", start, end, scene["on_screen_text"], scene["visual"], owner + 1,
                                  scene.get("tone", "curious")))
        return shots
    total = voice.duration + TAIL
    shots = []
    for i, segment in enumerate(segments):
        start = 0.0 if i == 0 else segment.start
        end = segments[i + 1].start if i + 1 < len(segments) else total
        if i == 0:
            shots.append(Shot("hook", start, end, script.hook, tone="excited",
                              broll=tuple(getattr(script, "hook_broll", ()) or ())))
        elif i == len(segments) - 1:
            shots.append(Shot("outro", start, end, script.outro, tone="funny"))
        else:
            scene = script.script_scenes[i - 1]
            shots.append(Shot("scene", start, end, scene["on_screen_text"], scene["visual"], i,
                              scene.get("tone", "curious"), tuple(scene.get("broll") or ())))
    return shots


def caption_groups(words: list[Word], max_words: int = 3, max_chars: int = 18) -> list[list[Word]]:
    """Short phrases, the way high-retention Shorts show subtitles: a few words at a time."""
    groups, current = [], []
    for word in words:
        current.append(word)
        text = " ".join(w.text for w in current)
        ends_clause = word.text[-1:] in ".,!?;:"
        if len(current) >= max_words or len(text) >= max_chars or ends_clause:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


# ----- Frame -----


# Per-video variety: the caption highlight, the host's shirt and the music change from video to video.
HIGHLIGHTS = ((255, 214, 10), (0, 230, 150), (255, 92, 130), (90, 200, 255), (255, 150, 40))
SHIRTS = ((64, 150, 255), (255, 96, 72), (40, 180, 130), (150, 100, 240), (240, 170, 40))


def style_seed(script) -> int:
    import hashlib

    return int(hashlib.sha1(str(getattr(script, "title", "")).encode()).hexdigest()[:8], 16)


class Renderer:
    """Draws each frame. `modern` (the default) adds the stock-footage beats, the host, kinetic captions and
    per-video variety; modern=False keeps the original text-card look exactly."""

    def __init__(self, script, voice: Voiceover, niche: str, date: datetime | None = None,
                 layout: Layout | None = None, theme: Theme | None = None, beats: list[Beat] | None = None,
                 mascot: bool = False, modern: bool = False):
        self.script = script
        self.voice = voice
        self.modern = modern or mascot or bool(beats)
        base = layout or layout_for(getattr(script, "format", "short"))
        self.with_host = mascot
        if mascot and base is PORTRAIT:  # make room for the host under the hook and outro text
            base = replace(base, hook_top=400, hook_lines=4, hook_size=96, outro_top=380, outro_lines=4,
                           caption_y=1190, subtitle_size=88)
        self.L = base
        self.portrait = base.height > base.width
        self.theme = theme or theme_for(niche)
        self.shots = build_shots(script, voice)
        self.duration = voice.duration + TAIL
        self.groups = caption_groups(voice.words, 3 if self.portrait else 5, 18 if self.portrait else 34)
        self.date = (date or datetime.now(timezone.utc)).strftime("%b %d, %Y").upper()
        self.seed = style_seed(script)
        self.highlight = HIGHLIGHTS[self.seed % len(HIGHLIGHTS)] if self.modern else YELLOW
        self.shirt = SHIRTS[(self.seed // 7) % len(SHIRTS)]
        self.beats = sorted(beats or [], key=lambda b: b.start)
        self._reader: tuple[Beat, ClipReader] | None = None
        self.sync = None
        if mascot:
            try:
                samples = decode(voice.path)
            except Exception:  # noqa: BLE001 - no audio to read: the host just doesn't talk
                samples = np.zeros(SAMPLE_RATE, dtype=np.float32)
            self.sync = host.Lipsync(samples, SAMPLE_RATE, voice.words, FPS)

    def beat_at(self, t: float) -> Beat | None:
        for beat in self.beats:
            if beat.start <= t < beat.end:
                return beat
        return None

    def clip_frame(self, beat: Beat, t: float) -> Image.Image:
        if self._reader is None or self._reader[0] is not beat:
            self.close()
            reader = ClipReader(beat.clip, self.L.width, self.L.height, beat.end - beat.start, beat.offset,
                                beat.zoom_in)
            # Catch up when a frame is asked for mid-beat (the thumbnail).
            for _ in range(max(0, int((t - beat.start) * FPS))):
                reader.next()
            self._reader = (beat, reader)
        frame = self._reader[1].next()
        black = Image.new("RGB", frame.size, INK)
        return Image.composite(black, frame, shade(self.L.width, self.L.height))

    def close(self) -> None:
        if self._reader is not None:
            self._reader[1].close()
            self._reader = None

    def shot_at(self, t: float) -> Shot:
        for shot in self.shots:
            if t < shot.end:
                return shot
        return self.shots[-1]

    def frame(self, t: float) -> Image.Image:
        L = self.L
        beat = self.beat_at(t)
        on_footage = bool(beat and beat.clip)
        if on_footage:
            frame = self.clip_frame(beat, t)
        else:
            drift = int(400 * t / self.duration)
            frame = backdrop(self.theme.glow, L, self.theme.texture == "hex").crop((0, drift, L.width, drift + L.height))
            rain = texture_layer(self.theme.texture, L)
            offset = int(t * (90 if self.theme.texture in ("hex", "stripes") else 40)) % L.height
            frame.paste(self.theme.accent, (0, 0), rain.crop((0, L.height - offset, L.width, 2 * L.height - offset)))

        shot = self.shot_at(t)
        local = t - shot.start
        draw = ImageDraw.Draw(frame)
        self.header(frame, draw, t)
        if shot.kind == "hook":
            self.hook(frame, draw, shot, local)
        elif shot.kind == "outro":
            self.outro(frame, draw, shot, local)
        elif on_footage:
            self.headline(frame, draw, shot, local)
        else:
            self.scene(frame, draw, shot, local)
        if self.with_host:
            self.presenter(frame, shot, t, local)
            draw = ImageDraw.Draw(frame)
        if shot.kind == "scene":  # The hook and outro already show their full text.
            if self.modern:
                self.kinetic(frame, draw, t)
            else:
                self.subtitles(draw, t)
        self.progress(frame, t)
        frame = self.cut_effects(frame, local, shot)
        if beat and beat.start > shot.start + 0.05 and t - beat.start < 0.16:
            # A small punch-in on cuts inside a shot, lighter than the scene-change hit.
            k = 1 + 0.035 * (1 - ease(t - beat.start, 0, 0.16))
            w, h = int(L.width * k), int(L.height * k)
            frame = frame.resize((w, h), Image.BILINEAR).crop(((w - L.width) // 2, (h - L.height) // 2,
                                                               (w - L.width) // 2 + L.width,
                                                               (h - L.height) // 2 + L.height))
        return frame

    # ----- Modern layers -----

    def headline(self, frame, draw, shot, local):
        """Over footage the scene's caption becomes a compact headline at the top, leaving the picture clear."""
        L = self.L
        accent = RED if shot.visual in ("alert", "warning") else self.theme.accent
        a = ease(local, 0.0, 0.25)
        label = f"{shot.number:02d} / {VISUAL_LABELS.get(shot.visual, 'KEY FACTS')}"
        tag = font("mono", 30, 700)
        typeface = fit(shot.caption.upper(), "sans", 800, 76 if self.portrait else 70, 46, L.column - 60, 2)
        lines = wrap(shot.caption.upper(), typeface, L.column - 60)
        step = int(typeface.size * 1.08)
        top = L.tag_y - 120 + int((1 - a) * -30) if self.portrait else L.tag_y - 40
        height = 74 + step * len(lines) + 30
        width = max(int(tag.getlength(label)), *(int(typeface.getlength(x)) for x in lines)) + 70
        width = min(width, L.column)
        box(frame, L.left, top, L.left + int(width * a), top + height, 26, (8, 12, 20), 0.78 * a)
        box(frame, L.left, top, L.left + 10, top + height, 5, accent, a)
        if a > 0.6:
            draw.text((L.left + 36, top + 22), label, font=tag, fill=accent)
            for i, line in enumerate(lines):
                color = accent if (shot.visual == "stat" and i == 0) else WHITE
                draw.text((L.left + 36, top + 70 + i * step), line, font=typeface, fill=color)

    def presenter(self, frame, shot, t, local):
        """The host: big under the hook and outro text, small in the corner during scenes, reacting to the voice."""
        L = self.L
        big = shot.kind in ("hook", "outro")
        height = int(L.height * (0.34 if big else 0.15)) if self.portrait else int(L.height * (0.46 if big else 0.26))
        level = self.sync.level_at(t)
        pose = host.pose_at(t, self.sync, shot.tone, waving=shot.kind == "outro" and local > 0.4, seed=self.seed)
        image = host.draw(pose, self.shirt, height)
        dy, tilt = host.bob(t, level)
        if abs(tilt) > 0.3:
            image = image.rotate(tilt, resample=Image.BICUBIC, center=(image.width // 2, image.height))
        enter = ease(local, 0.0, 0.35) if big else ease(local, 0.1, 0.3)
        bottom = L.height - int(L.height * 0.17) if self.portrait else L.height - 20
        if big:
            x = L.center - image.width // 2 - (40 if self.portrait else -L.width // 4)
        else:
            x = L.left - 20 if self.portrait else L.right - image.width
        y = bottom - image.height + dy + int((1 - enter) * image.height * 0.6)
        # A soft spotlight behind the host so it sits in the scene instead of floating.
        glow_r = int(image.width * 0.62)
        cx, cy = x + image.width // 2, y + int(image.height * 0.45)
        spot = Image.new("L", (glow_r * 2, glow_r * 2), 0)
        ImageDraw.Draw(spot).ellipse((0, 0, glow_r * 2 - 1, glow_r * 2 - 1), fill=int(120 * enter))
        frame.paste(self.theme.accent if big else (20, 26, 38), (cx - glow_r, cy - glow_r), spot)
        frame.paste(image, (x, y), image)

    def kinetic(self, frame, draw, t):
        """Captions that pop in a few words at a time, with the spoken word on a coloured block."""
        group = next((g for g in self.groups if g[0].start - 0.05 <= t < g[-1].end + 0.25), None)
        if not group:
            return
        L = self.L
        size = L.subtitle_size + (8 if self.portrait else 0)
        typeface = font("sans", size, 800)
        texts = [w.text.upper() for w in group]
        space = typeface.getlength(" ") + 6
        widths = [typeface.getlength(text) for text in texts]
        total = sum(widths) + space * (len(texts) - 1)
        if total > L.column:
            typeface = font("sans", int(size * L.column / total), 800)
            space = typeface.getlength(" ") + 6
            widths = [typeface.getlength(text) for text in texts]
            total = sum(widths) + space * (len(texts) - 1)
        x = L.center - total / 2
        pop = ease(t, group[0].start - 0.05, 0.14)
        base_y = L.caption_y + int((1 - pop) * 36)
        for word, text, width in zip(group, texts, widths):
            if t < word.start - 0.05:  # words appear as they are spoken
                x += width + space
                continue
            active = word.start <= t < word.end + 0.06
            bounce = int(-10 * (1 - ease(t, word.start, 0.12))) if active else 0
            y = base_y + bounce
            if active:
                pad = 14
                box(frame, x - pad, y - 6, x + width + pad, y + typeface.size + 18, 18, self.highlight)
                draw.text((x, y), text, font=typeface, fill=INK)
            else:
                strong = any(ch.isdigit() for ch in text) or len(text.strip(".,!?")) >= 8
                draw.text((x, y), text, font=typeface, fill=self.highlight if strong else WHITE,
                          stroke_width=9, stroke_fill=BLACK)
            x += width + space

    def header(self, frame, draw, t):
        L, accent = self.L, self.theme.accent
        LEFT, y = L.left, L.header_y
        badge = font("mono", 34, 800)
        label = self.theme.badge
        width = int(badge.getlength(label)) + 92
        box(frame, LEFT, y, LEFT + width, y + 64, 14, (20, 26, 38))
        box(frame, LEFT, y, LEFT + 8, y + 64, 4, accent)
        # A blinking live dot.
        if int(t * 2) % 2 == 0:
            draw.ellipse((LEFT + 28, y + 22, LEFT + 48, y + 42), fill=RED)
        draw.text((LEFT + 64, y + 32), label, font=badge, fill=WHITE, anchor="lm")
        draw.text((L.right, y + 32), self.date, font=font("mono", 28, 500), fill=MUTED, anchor="rm")

    def hook(self, frame, draw, shot, local):
        L, accent = self.L, self.theme.accent
        LEFT = L.left
        a = ease(local, 0.0, 0.25)
        typeface = fit(shot.caption.upper(), "sans", 800, L.hook_size, 60, L.column, L.hook_lines)
        lines = wrap(shot.caption.upper(), typeface, L.column)
        step = int(typeface.size * 1.08)
        top = L.hook_top - int((1 - a) * 60)
        for i, line in enumerate(lines):
            # Lines slam in one after another.
            la = ease(local, 0.05 * i, 0.2)
            if la <= 0:
                continue
            x = LEFT - int((1 - la) * 120)
            color = accent if i == 0 else WHITE
            draw.text((x, top + i * step), line, font=typeface, fill=color, stroke_width=4, stroke_fill=BLACK)
        bar = ease(local, 0.3, 0.8)
        y = top + len(lines) * step + 40
        box(frame, LEFT, y, LEFT + int(300 * bar), y + 14, 7, RED)

    def scene(self, frame, draw, shot, local):
        L = self.L
        LEFT, RIGHT, COLUMN = L.left, L.right, L.column
        accent = RED if shot.visual in ("alert", "warning") else self.theme.accent
        label = VISUAL_LABELS.get(shot.visual, "KEY FACTS")
        a = ease(local, 0.0, 0.25)

        tag = font("mono", 32, 700)
        draw.text((LEFT, L.tag_y), f"{shot.number:02d} / {label}", font=tag, fill=accent)

        size = L.stat_size if shot.visual == "stat" else L.scene_size
        typeface = fit(shot.caption.upper(), "sans", 800, size, 56, COLUMN - 60, L.scene_lines)
        lines = wrap(shot.caption.upper(), typeface, COLUMN - 60)
        step = int(typeface.size * 1.1)
        height = step * len(lines) + 110
        top = max(L.scene_min_top, L.scene_center - height // 2) + int((1 - a) * 50)
        box(frame, LEFT, top, RIGHT, top + height, 34, (14, 20, 32), 0.92 * a)
        box(frame, LEFT, top, LEFT + 12, top + height, 6, accent, a)
        for i, line in enumerate(lines):
            color = accent if (shot.visual == "stat" and i == 0) else WHITE
            draw.text((LEFT + 50, top + 55 + i * step), line, font=typeface, fill=color)

        if shot.visual == "warning":
            # A pulsing hazard ring next to the label.
            pulse = 0.5 + 0.5 * math.sin(local * 8)
            r = 18 + 6 * pulse
            cx, cy = RIGHT - 30, L.tag_y + 18
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=RED, width=5)

    def outro(self, frame, draw, shot, local):
        L, accent = self.L, self.theme.accent
        CENTER = L.center
        a = ease(local, 0.0, 0.3)
        typeface = fit(shot.caption, "sans", 800, L.outro_size, 54, L.column, L.outro_lines)
        lines = wrap(shot.caption, typeface, L.column)
        step = int(typeface.size * 1.12)
        top = L.outro_top + int((1 - a) * 40)
        for i, line in enumerate(lines):
            draw.text((CENTER, top + i * step), line, font=typeface, fill=WHITE, anchor="ma",
                      stroke_width=3, stroke_fill=BLACK)
        b = ease(local, 0.4, 0.3)
        if b > 0:
            pulse = 1 + 0.04 * math.sin(local * 6)
            w, h = int(520 * pulse), int(120 * pulse)
            y = top + len(lines) * step + 70
            box(frame, CENTER - w // 2, y, CENTER + w // 2, y + h, h // 2, RED, b)
            draw.text((CENTER, y + h // 2), "SUBSCRIBE", font=font("sans", 58, 800), fill=WHITE, anchor="mm")
            preset = self.theme in THEMES.values() and L is PORTRAIT
            tagline = f"daily {self.theme.badge.lower()} in 45 seconds" if preset else "hit subscribe for more"
            if not self.with_host:  # the waving host sits where the tagline would go
                draw.text((CENTER, y + h + 50), tagline, font=font("mono", 34, 600), fill=accent, anchor="mm")

    def subtitles(self, draw, t):
        group = next((g for g in self.groups if g[0].start - 0.05 <= t < g[-1].end + 0.25), None)
        if not group:
            return
        L = self.L
        COLUMN, size = L.column, L.subtitle_size
        typeface = font("sans", size, 800)
        texts = [w.text.upper() for w in group]
        space = typeface.getlength(" ")
        widths = [typeface.getlength(text) for text in texts]
        total = sum(widths) + space * (len(texts) - 1)
        if total > COLUMN:
            typeface = font("sans", int(size * COLUMN / total), 800)
            space = typeface.getlength(" ")
            widths = [typeface.getlength(text) for text in texts]
            total = sum(widths) + space * (len(texts) - 1)
        x = L.center - total / 2
        pop = ease(t, group[0].start - 0.05, 0.12)
        y = L.caption_y + int((1 - pop) * 20)
        for word, text, width in zip(group, texts, widths):
            active = word.start <= t < word.end + 0.08
            draw.text((x, y), text, font=typeface, fill=YELLOW if active else WHITE,
                      stroke_width=9, stroke_fill=BLACK)
            x += width + space

    def progress(self, frame, t):
        box(frame, 0, 0, int(self.L.width * clamp(t / self.duration)), 12, 0, self.theme.accent)

    def cut_effects(self, frame: Image.Image, local: float, shot: Shot) -> Image.Image:
        """A zoom punch, a flash and a short RGB-split glitch on every cut."""
        if shot.kind != "hook" and local < 0.3:
            scale = 1 + 0.08 * (1 - ease(local, 0, 0.3))
            W, H = self.L.width, self.L.height
            w, h = int(W * scale), int(H * scale)
            frame = frame.resize((w, h), Image.BILINEAR).crop(((w - W) // 2, (h - H) // 2,
                                                                (w - W) // 2 + W, (h - H) // 2 + H))
        if local < 0.1:
            r, g, b = frame.split()
            shift = int(18 * (1 - local / 0.1))
            frame = Image.merge("RGB", (r.transform(r.size, Image.AFFINE, (1, 0, shift, 0, 1, 0)), g,
                                        b.transform(b.size, Image.AFFINE, (1, 0, -shift, 0, 1, 0))))
        if shot.kind != "hook" and local < 0.07:
            frame = Image.blend(frame, Image.new("RGB", frame.size, WHITE), 0.35 * (1 - local / 0.07))
        return frame


# ----- Audio -----


def tone(frequency, length, attack=0.005, decay=0.4, harmonics=(1.0,)):
    times = np.arange(int(length * SAMPLE_RATE)) / SAMPLE_RATE
    wave_ = sum(s * np.sin(2 * np.pi * frequency * (i + 1) * times) for i, s in enumerate(harmonics))
    envelope = np.minimum(times / attack, 1.0) * np.exp(-times / decay)
    return wave_ * envelope * np.minimum((length - times) / 0.01, 1.0)


def place(mix: np.ndarray, signal: np.ndarray, start: float, gain: float = 1.0):
    begin = int(start * SAMPLE_RATE)
    if begin >= len(mix):
        return
    part = signal[: len(mix) - begin]
    mix[begin : begin + len(part)] += part * gain


MUSIC = (  # (BPM, root Hz, arpeggio in semitones): one is picked per video so they don't all sound the same
    (120, 55.0, (0, 3, 7, 10)),
    (104, 49.0, (0, 4, 7, 11)),
    (128, 58.27, (0, 7, 3, 10)),
    (112, 61.74, (0, 5, 7, 12)),
    (96, 51.91, (0, 3, 5, 7)),
)


def music_bed(duration: float, cuts: list[float], variant: int = 0) -> np.ndarray:
    """A driving bed: drone, a kick pulse, ticking hats, an arpeggio, and a whoosh-hit on every cut.
    `variant` picks tempo, key and melody; 0 is the original bed."""
    bpm, root, arp = MUSIC[variant % len(MUSIC)]
    rng = np.random.default_rng(7 + variant)
    mix = np.zeros(int(duration * SAMPLE_RATE))
    beat = 60.0 / bpm
    times = np.arange(len(mix)) / SAMPLE_RATE
    # Drone on the root, slowly breathing.
    drone = sum(np.sin(2 * np.pi * f * times) for f in (root, root * 2, root * 2.996)) / 3
    mix += drone * 0.18 * (0.75 + 0.25 * np.sin(2 * np.pi * times / 8))
    for i in range(int(duration / beat)):
        t = i * beat
        kick_t = np.arange(int(0.3 * SAMPLE_RATE)) / SAMPLE_RATE
        kick = np.sin(2 * np.pi * (45 * kick_t + 30 * (1 - np.exp(-kick_t * 30)))) * np.exp(-kick_t / 0.09)
        place(mix, kick, t, 0.55)
        if i % 4 == 2:
            place(mix, tone(root * 4 * 2 ** (arp[(i // 4) % 4] / 12), 0.4, decay=0.15, harmonics=(1, 0.5, 0.2)), t, 0.12)
        if variant and i % 2 == 1:  # the varied beds add a lighter off-beat pluck
            place(mix, tone(root * 8 * 2 ** (arp[(i // 2) % 4] / 12), 0.25, decay=0.08, harmonics=(1, 0.3)), t, 0.05)
        noise = np.diff(rng.normal(size=int(0.04 * SAMPLE_RATE)), prepend=0)
        place(mix, noise * np.exp(-np.arange(len(noise)) / (0.008 * SAMPLE_RATE)), t + beat / 2, 0.05)
    for cut in cuts:
        length = int(0.35 * SAMPLE_RATE)
        sweep = rng.normal(size=length) * np.linspace(0, 1, length) ** 2
        place(mix, sweep, max(0.0, cut - 0.35), 0.06)
        hit_t = np.arange(int(0.5 * SAMPLE_RATE)) / SAMPLE_RATE
        place(mix, np.sin(2 * np.pi * 50 * hit_t) * np.exp(-hit_t / 0.15), cut, 0.5)
    mix *= np.clip((duration - times) / 1.0, 0, 1)
    return np.tanh(mix)


def mix_audio(voice: Voiceover, shots: list[Shot], duration: float, path: Path, variant: int = 0) -> None:
    speech = decode(voice.path)
    total = int(duration * SAMPLE_RATE)
    speech = np.pad(speech, (0, max(0, total - len(speech))))[:total]
    music = music_bed(duration, [s.start for s in shots[1:]], variant)
    # Duck the music under the voice so every word stays clear.
    window = int(0.05 * SAMPLE_RATE)
    envelope = np.convolve(np.abs(speech), np.ones(window) / window, mode="same")
    duck = 1 - 0.6 * np.clip(envelope / 0.05, 0, 1)
    out = speech * 1.0 + music * 0.16 * duck
    out = out / max(1.0, float(np.max(np.abs(out))) / 0.95)
    write_wav(path, out)


# ----- Encoding -----


def render(script, voice: Voiceover, niche: str, output: Path, theme: Theme | None = None,
           renderer_cls=None, **options) -> Path:
    """options go to the Renderer: beats=[Beat...], mascot=True, modern=True."""
    renderer = (renderer_cls or Renderer)(script, voice, niche, theme=theme, **options)
    L = renderer.L
    audio_path = output.with_suffix(".mix.wav")
    mix_audio(voice, renderer.shots, renderer.duration, audio_path, renderer.seed % len(MUSIC) if renderer.modern else 0)
    frames = int(renderer.duration * FPS)
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{L.width}x{L.height}", "-r", str(FPS), "-i", "-",
        "-i", str(audio_path), "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(L.crf), "-pix_fmt", "yuv420p",
        "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-ar", "44100",  # EBU R128 target for Shorts/Reels
        "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(output),
    ]
    encoder = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for index in range(frames):
            encoder.stdin.write(renderer.frame(index / FPS).tobytes())
        encoder.stdin.close()
        errors = encoder.stderr.read().decode(errors="replace")
        if encoder.wait(timeout=300) != 0:
            raise RuntimeError(f"ffmpeg failed: {errors[:500]}")
    except BrokenPipeError as exc:
        raise RuntimeError(f"ffmpeg stopped: {encoder.stderr.read().decode(errors='replace')[:500]}") from exc
    finally:
        renderer.close()
        if encoder.poll() is None:
            encoder.kill()
    log.info("rendered %s (%.1f s, %d frames)", output, renderer.duration, frames)
    return output


def thumbnail(script, voice: Voiceover, niche: str, path: Path, theme: Theme | None = None,
              renderer_cls=None, **options) -> Path:
    """A still from the hook, saved for reference (Shorts pick their own thumbnail frame)."""
    renderer = (renderer_cls or Renderer)(script, voice, niche, theme=theme, **options)
    try:
        renderer.frame(1.2).save(path, quality=90)
    finally:
        renderer.close()
    return path
