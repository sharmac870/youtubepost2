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

    @classmethod
    def of(cls, s: ScriptJSON) -> "RenderScript":
        return cls(s.title, s.hook_text, [sc.model_dump() for sc in s.scenes], s.call_to_action,
                   hook_broll=list(s.hook_broll), tags=list(s.tags))

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


def plan_beats(shots: list[builder.Shot], words: list[tts.Word], stock: footage.Footage | None,
               fallback: list[str]) -> list[builder.Beat]:
    """One stock clip per 2-3 s beat of the hook and scenes, from that shot's search terms."""
    beats: list[builder.Beat] = []
    flip = False
    for shot in shots:
        if shot.kind == "outro":
            continue
        queries = list(shot.broll) or fallback
        for n, (start, end) in enumerate(builder.split_beats(shot, words)):
            clip = None
            if stock is not None and queries:
                rotated = queries[n % len(queries):] + queries[: n % len(queries)]
                clip = stock.find(rotated, min_seconds=min(end - start, 4.0))
            offset = 0.0
            if clip and clip.duration > (end - start) + 1:
                offset = min(1.5, (clip.duration - (end - start)) / 3)
            flip = not flip
            beats.append(builder.Beat(start, end, clip.path if clip else None, offset, zoom_in=flip))
    return beats


def render(script: ScriptJSON, category_id: str, folder: Path, voice: str, rate: str,
           max_seconds: float = 58.0, *, style: str = "footage", mascot: bool = True,
           providers: Callable[[], list] | None = None) -> tuple[Path, Path, float]:
    folder.mkdir(parents=True, exist_ok=True)
    rs = RenderScript.of(script)
    modern = style != "classic"
    tones = rs.tones if modern else None
    spoken = tts.synthesize(rs.narration_parts, voice, rate, folder / "audio", tones)
    if spoken.duration + builder.TAIL > max_seconds:
        log.info("voiceover %.1f s is too long; re-voicing faster", spoken.duration)
        spoken = tts.synthesize(rs.narration_parts, voice, "+28%", folder / "audio", tones)
    if spoken.duration + builder.TAIL > max_seconds + 4:
        from ...common.errors import Permanent

        raise Permanent(f"narration runs {spoken.duration:.0f} s, over the {max_seconds:.0f} s limit")
    look, badge = CATEGORY_LOOK.get(category_id, ("violet", "TRENDING"))
    theme = replace(builder.THEMES[look], badge=badge)
    options: dict = {}
    if modern:
        stock = None
        found = []
        try:
            found = providers() if providers else []
        except Exception as exc:  # noqa: BLE001 - no key or no vault: render without clips
            log.warning("stock footage unavailable: %s", exc)
        if found:
            portrait = getattr(rs, "format", "short") != "video"
            stock = footage.Footage(found, folder / "clips", portrait=portrait)
        shots = builder.build_shots(rs, spoken)
        beats = plan_beats(shots, spoken.words, stock, rs.tags[:2])
        if stock is not None:
            stock.write_credits(folder / "credits.json")
            log.info("stock footage: %d clips for %d beats", len(stock.credits), len(beats))
        options = {"beats": beats, "mascot": mascot, "modern": True}
    video = builder.render(rs, spoken, look, folder / "video.mp4", theme, LabelledRenderer, **options)
    thumb = builder.thumbnail(rs, spoken, look, folder / "preview.jpg", theme, LabelledRenderer, **options)
    return video, thumb, round(spoken.duration + builder.TAIL, 1)
