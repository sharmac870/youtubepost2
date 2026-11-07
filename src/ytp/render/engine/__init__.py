"""In-process render engine: neural TTS (edge-tts, free) + Pillow frames + ffmpeg, adapted from the
YoutubePosts renderer. It replaces OpenShorts (LLD Q4 fallback): no extra always-on container, no GPU,
nothing to license; music is synthesized, captions are word-timed from the voice engine."""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from pathlib import Path

from PIL import ImageDraw

from ...common.models import ScriptJSON
from . import builder, tts

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

    @classmethod
    def of(cls, s: ScriptJSON) -> "RenderScript":
        return cls(s.title, s.hook_text, [sc.model_dump() for sc in s.scenes], s.call_to_action)

    @property
    def narration_parts(self) -> list[str]:
        return [self.hook] + [s["narration"] for s in self.script_scenes] + [self.outro]


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


def render(script: ScriptJSON, category_id: str, folder: Path, voice: str, rate: str,
           max_seconds: float = 58.0) -> tuple[Path, Path, float]:
    folder.mkdir(parents=True, exist_ok=True)
    rs = RenderScript.of(script)
    spoken = tts.synthesize(rs.narration_parts, voice, rate, folder / "audio")
    if spoken.duration + builder.TAIL > max_seconds:
        log.info("voiceover %.1f s is too long; re-voicing faster", spoken.duration)
        spoken = tts.synthesize(rs.narration_parts, voice, "+28%", folder / "audio")
    if spoken.duration + builder.TAIL > max_seconds + 4:
        from ...common.errors import Permanent

        raise Permanent(f"narration runs {spoken.duration:.0f} s, over the {max_seconds:.0f} s limit")
    look, badge = CATEGORY_LOOK.get(category_id, ("violet", "TRENDING"))
    theme = replace(builder.THEMES[look], badge=badge)
    video = builder.render(rs, spoken, look, folder / "video.mp4", theme, LabelledRenderer)
    thumb = builder.thumbnail(rs, spoken, look, folder / "preview.jpg", theme, LabelledRenderer)
    return video, thumb, round(spoken.duration + builder.TAIL, 1)
