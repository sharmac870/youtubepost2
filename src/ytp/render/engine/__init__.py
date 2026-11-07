"""In-process render engine: neural TTS (edge-tts, free) + Pillow frames + ffmpeg, adapted from the
YoutubePosts renderer. It replaces OpenShorts (LLD Q4 fallback): no extra always-on container, no GPU,
nothing to license; music is synthesized, captions are word-timed from the voice engine.

style="footage" (default) makes the video feel made by a person: real stock clips from Pexels/Pixabay for
what each line talks about, cut every 2-3 s with a slow push-in; a cartoon host who lip-syncs to the voice;
captions that pop word by word; a voice whose pace and pitch follow each line's tone; and colours and music
that change per video. Without a stock API key it keeps everything except the clips. style="classic" is the
original text-card video."""
from __future__ import annotations

import logging
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from PIL import ImageDraw

from ...common.models import ScriptJSON
from . import builder, footage, tts

log = logging.getLogger(__name__)

# YouTube category -> look (colours, moving background) and on-screen badge.
CATEGORY_LOOK = {
    "17": ("business", "SPORTS"), "20": ("violet", "GAMING"), "24": ("viral", "ENTERTAINMENT"),
    "25": ("viral", "TRENDING NEWS"), "26": ("tech", "HOW-TO"), "28": ("tech", "TECH & SCIENCE"),
    "22": ("violet", "TRENDING"), "27": ("tech", "EXPLAINED"), "23": ("viral", "TRENDING"),
}


@dataclass
class RenderScript:
    """The shape the renderer reads."""
    title: str
    hook: str
    script_scenes: list[dict]
    outro: str
    format: str = "short"
    hook_broll: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    hook_show: str = ""
    # Which scene each voiced segment belongs to (-1 hook, -2 outro), when the voice is made sentence by sentence.
    segment_scene: list[int] | None = None

    @classmethod
    def of(cls, s: ScriptJSON) -> "RenderScript":
        return cls(s.title, s.hook_text, [sc.model_dump() for sc in s.scenes], s.call_to_action,
                   hook_broll=list(s.hook_broll), tags=list(s.tags), hook_show=s.hook_show)

    def lines(self) -> list[dict]:
        """Every spoken sentence with its planned picture, in order: the unit the footage style is built from.
        Scripts written before shot lists get one line per scene, searched by the scene's b-roll terms."""
        out = [{"scene": -1, "say": self.hook, "show": self.hook_show, "search": list(self.hook_broll),
                "tone": "hook"}]
        for i, scene in enumerate(self.script_scenes):
            tone = scene.get("tone", "curious")
            shots = scene.get("shots") or [{"say": scene["narration"], "show": "", "search": ""}]
            for shot in shots:
                search = [shot.get("search", "")] if shot.get("search") else []
                out.append({"scene": i, "say": shot["say"], "show": shot.get("show", ""),
                            "search": search + [b for b in scene.get("broll") or [] if b not in search], "tone": tone})
        out.append({"scene": -2, "say": self.outro, "show": "", "search": [], "tone": "outro"})
        return out

    @property
    def narration_parts(self) -> list[str]:
        return [self.hook] + [s["narration"] for s in self.script_scenes] + [self.outro]

    @property
    def tones(self) -> list[str]:
        return ["hook"] + [s.get("tone", "curious") for s in self.script_scenes] + ["outro"]


class LabelledRenderer(builder.Renderer):
    """Adds the on-screen "AI-generated" label for the first 3 seconds (LLD 11.2)."""

    def frame(self, t):
        frame = super().frame(t)
        if t < 3.0:
            draw = ImageDraw.Draw(frame)
            label = "AI-GENERATED"
            typeface = builder.font("mono", 26, 700)
            w = int(typeface.getlength(label)) + 36
            x, y = self.L.width - w - 40, 60
            builder.box(frame, x, y, x + w, y + 46, 12, (20, 26, 38), 0.85)
            draw.text((x + w // 2, y + 23), label, font=typeface, fill=builder.WHITE, anchor="mm")
        return frame


def plan_beats(lines: list[dict], voice: tts.Voiceover, stock: footage.Footage | None,
               fallback: list[str]) -> list[builder.Beat]:
    """One beat per spoken sentence, starting exactly when the sentence starts and showing a picture chosen for
    that sentence. Sentences longer than about 4.5 s get a second picture for their second half."""
    beats: list[builder.Beat] = []
    segments = voice.segments
    flip = False
    for k, (line, seg) in enumerate(zip(lines, segments)):
        if line["scene"] == -2:
            continue
        start = 0.0 if k == 0 else seg.start
        end = segments[k + 1].start if k + 1 < len(segments) else voice.duration + builder.TAIL
        spans = [(start, end)]
        if end - start > 4.5:
            middle = min((w.start for w in seg.words if start + 1.5 < w.start < end - 1.5),
                         key=lambda x: abs(x - (start + end) / 2), default=None)
            if middle:
                spans = [(start, middle), (middle, end)]
        queries = line["search"] or fallback
        first = None
        for n, (a, b) in enumerate(spans):
            clip = stock.pick(line["say"], line["show"], queries, min_seconds=min(b - a, 4.0)) if stock else None
            if clip is None and n and first is not None:
                clip = first  # no second picture that fits: stay on the first one
            first = first or clip
            offset = 0.0
            if clip and not clip.still and clip.duration > (b - a) + 1:
                offset = min(1.5, (clip.duration - (b - a)) / 3)
            if clip is first and n:
                offset += spans[0][1] - spans[0][0]
            flip = not flip
            beats.append(builder.Beat(a, b, clip.path if clip else None, offset, zoom_in=flip))
    return beats


def render(script: ScriptJSON, category_id: str, folder: Path, voice: str, rate: str,
           max_seconds: float = 58.0, *, style: str = "footage", mascot: bool = True,
           providers: Callable[[], list] | None = None, judge: Callable[[], object] | None = None,
           images: Callable[[], object] | None = None) -> tuple[Path, Path, float]:
    """providers/judge/images are factories (they read secrets lazily): stock libraries, the vision check that
    makes sure each clip fits its sentence, and the AI-image fallback. Any of them may be missing."""
    folder.mkdir(parents=True, exist_ok=True)
    rs = RenderScript.of(script)
    modern = style != "classic"
    if modern:
        # Sentence by sentence: each sentence is its own voice segment, so its picture cuts in exactly on it.
        lines = rs.lines()
        parts = [line["say"] for line in lines]
        tones = [line["tone"] for line in lines]
        pauses = [0.08 if i + 1 < len(lines) and lines[i + 1]["scene"] == line["scene"] else None
                  for i, line in enumerate(lines)]
        rs.segment_scene = [line["scene"] for line in lines]
    else:
        lines, parts, tones, pauses = [], rs.narration_parts, None, None
    spoken = tts.synthesize(parts, voice, rate, folder / "audio", tones, pauses)
    if spoken.duration + builder.TAIL > max_seconds:
        log.info("voiceover %.1f s is too long; re-voicing faster", spoken.duration)
        spoken = tts.synthesize(parts, voice, "+28%", folder / "audio", tones, pauses)
    if spoken.duration + builder.TAIL > max_seconds + 4:
        from ...common.errors import Permanent

        raise Permanent(f"narration runs {spoken.duration:.0f} s, over the {max_seconds:.0f} s limit")
    look, badge = CATEGORY_LOOK.get(category_id, ("violet", "TRENDING"))
    theme = replace(builder.THEMES[look], badge=badge)
    options: dict = {}
    if modern:
        stock = None
        found, judge_, images_ = [], None, None
        for name, make in (("stock footage", providers), ("vision check", judge), ("AI images", images)):
            try:
                value = make() if make else None
            except Exception as exc:  # noqa: BLE001 - a missing key just turns that part off
                log.warning("%s unavailable: %s", name, exc)
                value = None
            if name == "stock footage":
                found = value or []
            elif name == "vision check":
                judge_ = value
            else:
                images_ = value
        if found or images_ is not None:
            portrait = getattr(rs, "format", "short") != "video"
            stock = footage.Footage(found, folder / "clips", portrait=portrait, judge=judge_, images=images_)
        beats = plan_beats(lines, spoken, stock, rs.tags[:2])
        if stock is not None:
            stock.write_credits(folder / "credits.json")
            log.info("visuals: %d for %d beats (vision tokens %s, AI images %s)", len(stock.credits), len(beats),
                     getattr(judge_, "tokens", "-"), getattr(images_, "count", "-"))
        options = {"beats": beats, "mascot": mascot, "modern": True}
    video = builder.render(rs, spoken, look, folder / "video.mp4", theme, LabelledRenderer, **options)
    thumb = builder.thumbnail(rs, spoken, look, folder / "preview.jpg", theme, LabelledRenderer, **options)
    return video, thumb, round(spoken.duration + builder.TAIL, 1)
