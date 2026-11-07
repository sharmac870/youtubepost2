"""Domain schemas (LLD Section 6.2) and the content-item state machine (Section 5.3)."""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator


class State(StrEnum):
    DISCOVERED = "DISCOVERED"
    SKIPPED = "SKIPPED"
    SCRIPTED = "SCRIPTED"
    REJECTED = "REJECTED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXPIRED = "EXPIRED"
    APPROVED = "APPROVED"
    RENDERING = "RENDERING"
    RENDERED = "RENDERED"
    QA_PASSED = "QA_PASSED"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    PARTIALLY_PUBLISHED = "PARTIALLY_PUBLISHED"
    FAILED = "FAILED"


S = State
TRANSITIONS: dict[State, set[State]] = {
    S.DISCOVERED: {S.SKIPPED, S.SCRIPTED, S.FAILED, S.REJECTED},
    S.SCRIPTED: {S.REJECTED, S.AWAITING_APPROVAL, S.APPROVED},
    S.AWAITING_APPROVAL: {S.APPROVED, S.EXPIRED, S.REJECTED, S.AWAITING_APPROVAL},  # self = edited
    S.EXPIRED: {S.APPROVED, S.REJECTED, S.AWAITING_APPROVAL},
    S.REJECTED: {S.AWAITING_APPROVAL},           # operator can reopen a rejected draft
    S.APPROVED: {S.RENDERING, S.FAILED},
    S.RENDERING: {S.RENDERED, S.FAILED, S.RENDERING},
    S.RENDERED: {S.QA_PASSED, S.FAILED},
    S.QA_PASSED: {S.PUBLISHING},
    S.PUBLISHING: {S.PUBLISHED, S.PARTIALLY_PUBLISHED, S.FAILED},
    S.PARTIALLY_PUBLISHED: {S.PUBLISHING, S.PUBLISHED},
    S.FAILED: {S.APPROVED, S.PUBLISHING},        # operator: re-render, or retry publishing a rendered item
    S.PUBLISHED: set(),
    S.SKIPPED: set(),
}
TERMINAL = {S.PUBLISHED, S.FAILED, S.REJECTED, S.SKIPPED, S.EXPIRED}


class PubState(StrEnum):
    PENDING = "PENDING"
    UPLOADING = "UPLOADING"
    PROCESSING = "PROCESSING"
    LIVE = "LIVE"
    FAILED = "FAILED"
    REMOVED = "REMOVED"


class Platform(StrEnum):
    YOUTUBE = "YOUTUBE"
    INSTAGRAM = "INSTAGRAM"


class Outcome(StrEnum):
    REJECT = "REJECT"
    REVIEW = "REVIEW"
    APPROVE = "APPROVE"


# ----- Untrusted text -----

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁦-⁩]")
_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+")


def sanitize(text: str, limit: int) -> str:
    """LLD D1: Unicode normalisation, no control or bidi characters, no URLs, collapsed whitespace, capped."""
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = _CONTROL.sub("", text)
    text = _URL.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


class TrendPayload(BaseModel):
    """The topic signal a draft is written from.

    origin "youtube": a video on India's mostPopular chart (metadata only). "news": a story many outlets are
    covering, found by the niche hierarchy. "theme": a story found for a theme the owner typed in the console.
    """
    origin: Literal["youtube", "news", "theme"] = "youtube"
    source_video_id: str = Field("", pattern=r"^([A-Za-z0-9_-]{11})?$")
    title: str = Field(max_length=200)
    description: str = Field("", max_length=600)
    channel_id: str = Field("", max_length=40)
    channel_title: str = Field("", max_length=100)
    category_id: str = Field("25", pattern=r"^\d{1,3}$")
    view_count: int = Field(0, ge=0)
    like_count: int = Field(0, ge=0)
    published_at: datetime
    velocity_score: float = Field(0, ge=0)
    niche: str = Field("", max_length=40)
    theme: str = Field("", max_length=200)         # what the owner asked for (origin "theme")
    brief: str = Field("", max_length=4000)        # article excerpt, for facts; untrusted
    tags: list[str] = Field(default_factory=list, max_length=30)
    fingerprint: str = ""
    news: list[dict] = Field(default_factory=list, max_length=8)   # {title, source, url, published}

    @field_validator("title", "channel_title", mode="before")
    @classmethod
    def _clean_short(cls, v):
        return sanitize(v, 200)

    @field_validator("description", mode="before")
    @classmethod
    def _clean_desc(cls, v):
        return sanitize(v, 600)

    @field_validator("theme", mode="before")
    @classmethod
    def _clean_theme(cls, v):
        return sanitize(v, 200)

    @field_validator("brief", mode="before")
    @classmethod
    def _clean_brief(cls, v):
        return sanitize(v, 4000)

    @field_validator("tags", mode="before")
    @classmethod
    def _clean_tags(cls, v):
        return [sanitize(t, 40) for t in (v or [])][:30]


class Language(StrEnum):
    EN_IN = "en-IN"
    HI_IN = "hi-IN"


VISUALS = ("alert", "stat", "list", "explainer", "quote", "warning")
TONES = ("excited", "curious", "serious", "calm", "funny")


def _broll(v) -> list[str]:
    """Stock-footage search terms: short, plain words, no names (stock libraries have no real people's clips)."""
    out = []
    for term in v or []:
        term = re.sub(r"[^\w\s-]", " ", str(term), flags=re.UNICODE)
        term = re.sub(r"\s+", " ", term).strip()[:40]
        if term and term.lower() not in {t.lower() for t in out}:
            out.append(term)
    return out[:3]


class ShotLine(BaseModel):
    """One spoken sentence and the one picture shown while it is spoken. The voice is generated per shot, so
    the cut to this picture lands exactly when the sentence starts."""
    say: str = Field(min_length=2, max_length=220)
    show: str = Field(default="", max_length=200)     # what the viewer should see, concretely
    search: str = Field(default="", max_length=60)    # stock-footage search for it, 2-4 plain words

    @field_validator("search", mode="before")
    @classmethod
    def _search(cls, v):
        return (_broll([v]) or [""])[0]


class Scene(BaseModel):
    narration: str = Field(min_length=3, max_length=480)
    on_screen_text: str = Field(min_length=1, max_length=60)
    visual: str = "explainer"
    # What the viewer sees while this line is spoken, as stock-footage search terms (e.g. "mumbai traffic night").
    broll: list[str] = Field(default_factory=list)
    tone: str = "curious"   # how the line is voiced and how the mascot reacts
    shots: list[ShotLine] = Field(default_factory=list, max_length=4)

    @model_validator(mode="before")
    @classmethod
    def _narration_from_shots(cls, data):
        """Scripts written shot by shot carry no separate narration: it is the shots' sentences in order."""
        if isinstance(data, dict) and data.get("shots") and not data.get("narration"):
            data = {**data, "narration": " ".join(str(s.get("say", "")).strip() for s in data["shots"]
                                                  if isinstance(s, dict))}
        return data

    @field_validator("visual", mode="before")
    @classmethod
    def _visual(cls, v):
        return v if v in VISUALS else "explainer"

    @field_validator("tone", mode="before")
    @classmethod
    def _tone(cls, v):
        return v if v in TONES else "curious"

    _clean_broll = field_validator("broll", mode="before")(classmethod(lambda cls, v: _broll(v)))


class ScriptJSON(BaseModel):
    """LLD ScriptJSON. `scenes` carries the narrative body and its visual cues together, which is what the
    renderer needs; `narrative_body` and `visual_cues` are derived from it so the LLD fields still exist."""
    topic: str = Field(max_length=120)
    title: str = Field(min_length=5, max_length=95)
    description: str = Field(max_length=4500)
    caption: str = Field(max_length=2000)
    hashtags: list[str] = Field(min_length=3, max_length=8)
    tags: list[str] = Field(default_factory=list, max_length=15)
    language: Language = Language.EN_IN
    hook_text: str = Field(min_length=3, max_length=120)
    hook_broll: list[str] = Field(default_factory=list)
    hook_show: str = Field(default="", max_length=200)
    scenes: list[Scene] = Field(min_length=3, max_length=8)
    call_to_action: str = Field(min_length=3, max_length=120)
    claims_need_sources: bool
    sources: list[HttpUrl] = Field(default_factory=list, max_length=5)
    risk_flags: list[str] = Field(default_factory=list, max_length=10)

    _clean_hook_broll = field_validator("hook_broll", mode="before")(classmethod(lambda cls, v: _broll(v)))

    @field_validator("hashtags", mode="before")
    @classmethod
    def _hashtags(cls, v):
        out = []
        for tag in v or []:
            tag = re.sub(r"[^\w]", "", str(tag).lstrip("#"), flags=re.UNICODE)
            if tag and tag.lower() not in {t.lower() for t in out}:
                out.append(tag[:30])
        return out[:8]

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, v):
        return [re.sub(r"[<>\"#]", "", str(t)).strip()[:40] for t in (v or []) if str(t).strip()][:15]

    @field_validator("tags")
    @classmethod
    def tags_total_length(cls, v: list[str]) -> list[str]:
        if sum(len(t) + 1 for t in v) > 480:
            raise ValueError("YouTube tags exceed 500 characters")
        return v

    @model_validator(mode="after")
    def _length(self):
        words = self.word_count
        if not 60 <= words <= 175:
            raise ValueError(f"narration is {words} words; a 30-58 second Short needs 60-175")
        return self

    @property
    def narration_parts(self) -> list[str]:
        return [self.hook_text] + [s.narration for s in self.scenes] + [self.call_to_action]

    @property
    def narrative_body(self) -> str:
        return " ".join(s.narration for s in self.scenes)

    @property
    def visual_cues(self) -> list[str]:
        return [s.on_screen_text for s in self.scenes]

    @property
    def word_count(self) -> int:
        return sum(len(p.split()) for p in self.narration_parts)


class PolicyDecision(BaseModel):
    outcome: Outcome
    reasons: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
