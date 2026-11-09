"""Real footage for each spoken sentence: free stock clips from Pexels and Pixabay, checked by eye.

Keyword search alone gives loosely related clips, which makes the voice and the picture drift apart. So each
sentence's candidates are LOOKED AT: a vision model (Claude Haiku, about a cent per video) scores every
candidate's thumbnail against the sentence and its planned picture, and only a clip that clearly fits is used.
When none fits, an AI clip is generated for that sentence (fal.ai image-to-video, optional, paid per clip and
capped per video and per day), or else an AI image (fal.ai Flux, about $0.003 each), and without those the
sentence gets the drawn caption card rather than an unrelated clip.

Both libraries are free for commercial use with no attribution required; every clip used is still recorded in
credits.json next to the render (and in the render's licence_ref) so its source can be shown on request.
Without an API key, or when a search finds nothing, the renderer falls back to its drawn background.

ClipReader streams a clip's frames from ffmpeg in order (the renderer draws frames in time order), already
scaled to cover the frame, and adds a slow push-in (Ken Burns) so even static shots move.
"""
from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path

import httpx
import imageio_ffmpeg
import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

FPS = 30
OVERSCAN = 1.12   # clips are decoded this much larger than the frame, for the push-in


@dataclass
class Clip:
    provider: str
    id: str
    url: str          # the clip's page, for credits
    file_url: str     # the mp4 to download
    width: int
    height: int
    duration: float
    author: str = ""
    query: str = ""
    path: str = ""    # local file once downloaded
    thumb: str = ""   # preview image URL, for the vision check
    score: int = -1   # the vision check's 0-10 fit to its sentence; -1 = not checked
    said: str = ""    # the sentence it was chosen for

    @property
    def still(self) -> bool:
        return self.path.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))


class Pexels:
    name = "pexels"

    def __init__(self, api_key: str, http: httpx.Client | None = None):
        self.key = api_key
        self.http = http or httpx.Client(timeout=20)

    def search(self, query: str, portrait: bool = True, per_page: int = 8) -> list[Clip]:
        r = self.http.get("https://api.pexels.com/videos/search", headers={"Authorization": self.key},
                          params={"query": query, "orientation": "portrait" if portrait else "landscape",
                                  "size": "medium", "per_page": per_page})
        r.raise_for_status()
        out = []
        for v in r.json().get("videos", []):
            files = [f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4" and f.get("link")
                     and f.get("width") and f.get("height")]
            # The smallest file that still fills a 1080-wide frame without much upscaling.
            short = (lambda f: min(f["width"], f["height"]))
            good = sorted([f for f in files if short(f) >= 700], key=short) or sorted(files, key=short, reverse=True)
            if good:
                f = good[0]
                pictures = [p.get("picture") for p in v.get("video_pictures") or [] if p.get("picture")]
                thumb = pictures[len(pictures) // 2] if pictures else v.get("image", "")
                out.append(Clip(self.name, str(v["id"]), v.get("url", ""), f["link"], f["width"], f["height"],
                                float(v.get("duration") or 0), (v.get("user") or {}).get("name", ""), query,
                                thumb=thumb))
        return out


class Pixabay:
    name = "pixabay"

    def __init__(self, api_key: str, http: httpx.Client | None = None):
        self.key = api_key
        self.http = http or httpx.Client(timeout=20)

    def search(self, query: str, portrait: bool = True, per_page: int = 8) -> list[Clip]:
        r = self.http.get("https://pixabay.com/api/videos/", params={"key": self.key, "q": query[:100],
                                                                      "per_page": max(3, per_page), "safesearch": "true"})
        r.raise_for_status()
        out = []
        for v in r.json().get("hits", []):
            sizes = [s for s in (v.get("videos") or {}).values() if s.get("url") and s.get("width")]
            good = sorted([s for s in sizes if min(s["width"], s["height"]) >= 700],
                          key=lambda s: s["width"]) or sorted(sizes, key=lambda s: -s["width"])
            if good:
                s = good[0]
                thumbs = [x.get("thumbnail") for x in (v.get("videos") or {}).values() if x.get("thumbnail")]
                out.append(Clip(self.name, str(v["id"]), v.get("pageURL", ""), s["url"], s["width"], s["height"],
                                float(v.get("duration") or 0), v.get("user", ""), query,
                                thumb=thumbs[-1] if thumbs else ""))
        if portrait:  # Pixabay has no orientation filter; prefer the tall clips.
            out.sort(key=lambda c: c.height < c.width)
        return out


@dataclass
class Footage:
    """Finds and downloads clips for a list of search terms, never using the same clip twice in one video."""
    providers: list
    cache: Path
    portrait: bool = True
    used: set = field(default_factory=set)
    credits: list = field(default_factory=list)
    http: httpx.Client | None = None
    judge: object | None = None       # ClaudeJudge; None = keyword order only
    images: object | None = None      # FalImages; None = no AI-image fallback
    video: object | None = None       # FalVideo; None = no AI clips
    _results: dict = field(default_factory=dict)

    def candidates(self, queries: list[str], min_seconds: float, limit: int = 8) -> list[Clip]:
        out: list[Clip] = []
        seen = set(self.used)
        for query in queries:
            for provider in self.providers:
                key = (provider.name, query)
                if key not in self._results:
                    try:
                        self._results[key] = provider.search(query, self.portrait)
                    except Exception as exc:  # noqa: BLE001 - a failed search just means no footage for it
                        log.warning("%s search %r failed: %s", provider.name, query, exc)
                        self._results[key] = []
                for clip in self._results[key]:
                    if (clip.provider, clip.id) in seen or 0 < clip.duration < min_seconds:
                        continue
                    seen.add((clip.provider, clip.id))
                    out.append(clip)
                    if len(out) >= limit:
                        return out
        return out

    def pick(self, say: str, show: str, queries: list[str], min_seconds: float = 2.0,
             image_fallback: bool = True) -> Clip | None:
        """The visual for one sentence: the best stock clip that clearly shows it, else an AI image of it
        (unless image_fallback is off), else None (the renderer then shows the caption card instead of
        something unrelated)."""
        queries = [q for q in queries if q]
        clips = self.candidates(queries, min_seconds) if queries else []
        if clips and self.judge is not None:
            try:
                ranked = self.judge.rank(say, show, clips)
            except Exception as exc:  # noqa: BLE001 - judge down: don't guess, fall through to the image/card
                log.warning("vision check failed: %s", exc)
                ranked = []
            order = [(clips[i], score) for i, score in ranked if score >= self.judge.min_score]
        else:
            order = [(c, -1) for c in clips]
        for clip, score in order:
            if self.download(clip):
                clip.score, clip.said = score, say
                self.used.add((clip.provider, clip.id))
                self.credits.append(clip)
                return clip
        return self.illustrate(say, show) if image_fallback else None

    def illustrate(self, say: str, show: str) -> Clip | None:
        """An AI still image of the sentence's picture, or None."""
        if self.images is None or not (show or say):
            return None
        try:
            image = self.images.generate(show or say, self.portrait, self.cache)
        except Exception as exc:  # noqa: BLE001
            log.warning("image generation failed: %s", exc)
            return None
        if image is not None:
            image.said = say
            self.credits.append(image)
        return image

    def animate_many(self, wanted: list[tuple[str, str]], workers: int = 6) -> list[Clip | None]:
        """AI clips for several (sentence, picture) pairs at once: each takes a minute or two to generate, so
        they run in parallel. None where the budget ran out or generation failed."""
        if self.video is None or not wanted:
            return [None] * len(wanted)

        def one(pair: tuple[str, str]) -> Clip | None:
            say, show = pair
            try:
                return self.video.animate(show or say, self.portrait, self.cache)
            except Exception as exc:  # noqa: BLE001 - a failed clip falls back to stock or a still
                log.warning("AI clip failed for %r: %s", say[:40], exc)
                return None

        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(wanted)))) as pool:
            made = list(pool.map(one, wanted))
        for (say, _), clip in zip(wanted, made):
            if clip is not None:
                clip.said = say
                self.credits.append(clip)
        return made

    def download(self, clip: Clip) -> bool:
        self.cache.mkdir(parents=True, exist_ok=True)
        path = self.cache / f"{clip.provider}-{hashlib.sha1(clip.file_url.encode()).hexdigest()[:16]}.mp4"
        if not path.exists():
            try:
                http = self.http or httpx.Client(timeout=60, follow_redirects=True)
                with http.stream("GET", clip.file_url, follow_redirects=True) as r:
                    r.raise_for_status()
                    size = 0
                    with open(path.with_suffix(".part"), "wb") as out:
                        for chunk in r.iter_bytes(1 << 16):
                            size += len(chunk)
                            if size > 80 * 1024 * 1024:   # a stock clip this big is the wrong rendition
                                raise ValueError("clip too large")
                            out.write(chunk)
                path.with_suffix(".part").rename(path)
            except Exception as exc:  # noqa: BLE001
                log.warning("download of %s clip %s failed: %s", clip.provider, clip.id, exc)
                path.with_suffix(".part").unlink(missing_ok=True)
                return False
        clip.path = str(path)
        return True

    def write_credits(self, path: Path) -> None:
        path.write_text(json.dumps([{k: v for k, v in asdict(c).items() if k != "path"} for c in self.credits],
                                   indent=1))


JUDGE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["scores"],
    "properties": {"scores": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["clip", "score"],
        "properties": {"clip": {"type": "integer"}, "score": {"type": "integer"}}}}},
}

JUDGE_SYSTEM = """You are a video editor choosing b-roll for a vertical Short. You get one spoken sentence, the
picture the writer planned for it, and numbered thumbnails of candidate stock clips. Score each clip 0-10 for how
clearly a viewer would connect it with THAT sentence while hearing it: 9-10 shows exactly what is said, 6-8 is a
clear, natural illustration, 3-5 is only loosely related, 0-2 is unrelated or misleading. Score text-heavy,
watermarked, logo-led, offensive or low-quality clips 0. Output only the JSON."""


class ClaudeJudge:
    """Looks at candidate thumbnails and scores how well each fits its sentence (Claude, vision)."""

    def __init__(self, client, model: str, min_score: int = 6, http: httpx.Client | None = None,
                 on_usage: Callable[[int, int], None] | None = None):
        self.client = client
        self.model = model
        self.min_score = min_score
        self.on_usage = on_usage   # counts the tokens against the daily LLM spend cap
        self.http = http or httpx.Client(timeout=20, follow_redirects=True)
        self.tokens = 0

    def _image(self, url: str) -> str | None:
        import base64
        import io

        try:
            r = self.http.get(url)
            r.raise_for_status()
            img = Image.open(io.BytesIO(r.content)).convert("RGB")
            img.thumbnail((320, 320))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=80)
            return base64.b64encode(buf.getvalue()).decode()
        except Exception as exc:  # noqa: BLE001
            log.info("thumbnail %s unavailable: %s", url, exc)
            return None

    def rank(self, say: str, show: str, clips: list[Clip]) -> list[tuple[int, int]]:
        content: list[dict] = [{"type": "text", "text": f"Sentence: {say}\nPlanned picture: {show or '(none)'}"}]
        shown = []
        for i, clip in enumerate(clips):
            data = self._image(clip.thumb) if clip.thumb else None
            if data:
                shown.append(i)
                content += [{"type": "text", "text": f"Clip {i}:"},
                            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}]
        if not shown:
            return []
        response = self.client.messages.create(
            model=self.model, max_tokens=400, system=JUDGE_SYSTEM, messages=[{"role": "user", "content": content}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": JUDGE_SCHEMA}})
        usage = getattr(response, "usage", None)
        tin, tout = int(getattr(usage, "input_tokens", 0) or 0), int(getattr(usage, "output_tokens", 0) or 0)
        self.tokens += tin + tout
        if self.on_usage:
            self.on_usage(tin, tout)
        text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), "{}")
        scores = {int(s["clip"]): max(0, min(10, int(s["score"]))) for s in json.loads(text).get("scores", [])
                  if int(s.get("clip", -1)) in shown}
        return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


class FalImages:
    """An AI image for a sentence no stock clip fits (fal.ai Flux schnell, about $0.003 an image)."""

    URL = "https://fal.run/fal-ai/flux/schnell"
    STYLE = (", photorealistic, natural light, vertical composition, no text, no logos, no watermark, "
             "no recognisable real person")

    def __init__(self, api_key: str, http: httpx.Client | None = None):
        self.key = api_key
        self.http = http or httpx.Client(timeout=90, follow_redirects=True)
        self.count = 0

    def generate(self, prompt: str, portrait: bool, cache: Path) -> Clip | None:
        r = self.http.post(self.URL, headers={"Authorization": f"Key {self.key}"},
                           json={"prompt": prompt[:300] + self.STYLE, "num_images": 1,
                                 "image_size": "portrait_16_9" if portrait else "landscape_16_9",
                                 "enable_safety_checker": True})
        r.raise_for_status()
        images = r.json().get("images") or []
        if not images:
            return None
        url = images[0]["url"]
        img = self.http.get(url)
        img.raise_for_status()
        cache.mkdir(parents=True, exist_ok=True)
        path = cache / f"ai-{hashlib.sha1(prompt.encode()).hexdigest()[:16]}.jpg"
        Image.open(__import__("io").BytesIO(img.content)).convert("RGB").save(path, quality=92)
        self.count += 1
        return Clip("fal-flux", path.stem, "", url, 0, 0, 0.0, "AI-generated", prompt[:60], str(path))


class FalVideo:
    """A short AI video clip for one sentence (fal.ai image-to-video). Nothing runs or costs anything between
    videos: each clip is one paid request. A Flux keyframe of the sentence's picture is made first and then
    animated, which keeps one look across a video and wastes fewer generations than text-to-video.

    Spend is capped per video (max_usd) and per day (budget_left, read from the ledger); a clip that would go
    over either cap is not requested and the sentence falls back to stock footage or a still image."""

    BASE = "https://fal.run/"
    MOTION = (". Smooth natural motion, realistic, cinematic vertical shot, no text, no logos, no watermark, "
              "no recognisable real person.")

    def __init__(self, api_key: str, images, model: str, seconds: int = 5, price_per_s: float = 0.07,
                 max_usd: float = 1.2, budget_left: Callable[[], float] | None = None,
                 on_spend: Callable[[float], None] | None = None, http: httpx.Client | None = None):
        self.key = api_key
        self.images = images          # FalImages, for the keyframe
        self.model = model.strip("/")
        self.seconds = seconds
        self.price_per_s = price_per_s
        self.max_usd = max_usd
        self.budget_left = budget_left
        self.on_spend = on_spend
        self.http = http or httpx.Client(timeout=600, follow_redirects=True)
        self.spent = 0.0              # this video: reserved and paid
        self.count = 0
        self._pending = 0.0           # reserved but not yet written to the daily ledger
        self._lock = threading.Lock()

    @property
    def clip_usd(self) -> float:
        return round(self.seconds * self.price_per_s, 4)

    def _reserve(self) -> bool:
        cost = self.clip_usd
        with self._lock:
            if self.spent + cost > self.max_usd + 1e-9:
                return False
            if self.budget_left is not None and self.budget_left() - self._pending < cost - 1e-9:
                return False
            self.spent += cost
            self._pending += cost
            return True

    def _release(self, paid: bool) -> None:
        cost = self.clip_usd
        with self._lock:
            self._pending -= cost
            if not paid:
                self.spent -= cost

    def animate(self, prompt: str, portrait: bool, cache: Path) -> Clip | None:
        cache.mkdir(parents=True, exist_ok=True)
        path = cache / f"aiv-{hashlib.sha1(f'{self.model}|{prompt}'.encode()).hexdigest()[:16]}.mp4"
        if path.exists():
            return Clip("fal-video", path.stem, "", "", 0, 0, float(self.seconds), "AI-generated", prompt[:60],
                        str(path))
        if not self._reserve():
            log.info("AI clip skipped: video cap $%.2f or daily cap reached", self.max_usd)
            return None
        try:
            keyframe = self.images.generate(prompt, portrait, cache)
            if keyframe is None or not keyframe.file_url:
                self._release(paid=False)
                return None
            r = self.http.post(self.BASE + self.model, headers={"Authorization": f"Key {self.key}"},
                               json={"prompt": prompt[:400] + self.MOTION, "image_url": keyframe.file_url,
                                     "duration": str(self.seconds)})
            r.raise_for_status()
            url = (r.json().get("video") or {}).get("url")
            if not url:
                raise ValueError("no video in the response")
        except Exception:
            self._release(paid=False)
            raise
        self._release(paid=True)       # the clip is generated, so it is billed
        if self.on_spend:
            self.on_spend(self.clip_usd)
        self.count += 1
        video = self.http.get(url)
        video.raise_for_status()
        path.write_bytes(video.content)
        return Clip("fal-video", path.stem, "", url, 0, 0, float(self.seconds), "AI-generated", prompt[:60],
                    str(path))


def providers_from(pexels_key: str = "", pixabay_key: str = "") -> list:
    out = []
    if pexels_key:
        out.append(Pexels(pexels_key))
    if pixabay_key:
        out.append(Pixabay(pixabay_key))
    return out


class ClipReader:
    """Frames of one clip, in order, sized to cover width x height, with a slow push-in.
    Clips shorter than the shot are looped by ffmpeg."""

    def __init__(self, path: str, width: int, height: int, seconds: float, start: float = 0.0,
                 zoom_in: bool = True, ffmpeg: Callable[[], str] = imageio_ffmpeg.get_ffmpeg_exe):
        self.W, self.H = width, height
        self.w2, self.h2 = int(width * OVERSCAN) // 2 * 2, int(height * OVERSCAN) // 2 * 2
        self.frames = max(1, round(seconds * FPS))
        self.zoom_in = zoom_in
        self.index = 0
        self.last: Image.Image | None = None
        vf = (f"scale={self.w2}:{self.h2}:force_original_aspect_ratio=increase,crop={self.w2}:{self.h2},"
              f"fps={FPS},eq=saturation=1.12:contrast=1.04")
        still = path.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
        source = ["-loop", "1", "-i", path] if still else ["-stream_loop", "-1", "-ss", f"{start:.2f}", "-i", path]
        self.proc = subprocess.Popen(
            [ffmpeg(), "-v", "error", *source, "-t", f"{seconds + 0.5:.2f}",
             "-an", "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def next(self) -> Image.Image:
        size = self.w2 * self.h2 * 3
        raw = self.proc.stdout.read(size) if self.proc.stdout else b""
        if len(raw) == size:
            self.last = Image.frombuffer("RGB", (self.w2, self.h2), raw, "raw", "RGB", 0, 1)
        elif self.last is None:
            self.last = Image.new("RGB", (self.w2, self.h2), (10, 12, 18))
        progress = min(1.0, self.index / max(1, self.frames - 1))
        self.index += 1
        z = progress if self.zoom_in else 1 - progress
        # Crop shrinks from the whole overscanned frame to the frame size: a gentle 12% push-in.
        cw = int(self.w2 - (self.w2 - self.W) * z)
        ch = int(self.h2 - (self.h2 - self.H) * z)
        x0, y0 = (self.w2 - cw) // 2, (self.h2 - ch) // 2
        return self.last.crop((x0, y0, x0 + cw, y0 + ch)).resize((self.W, self.H), Image.BILINEAR)

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        if self.proc.stdout:
            self.proc.stdout.close()
        self.proc.wait(timeout=10)


@lru_cache(maxsize=4)
def shade(width: int, height: int) -> Image.Image:
    """How much to darken each row over footage: the top (badge) and the lower third (captions) get darker,
    the middle stays bright so the footage reads."""
    y = np.linspace(0, 1, height)
    top = np.clip(1 - y / 0.2, 0, 1) * 0.5
    bottom = np.clip((y - 0.5) / 0.35, 0, 1) ** 1.4 * 0.62
    base = 0.06
    alpha = np.clip(base + top + bottom, 0, 0.85)
    column = (alpha * 255).astype("uint8")[:, None]
    return Image.fromarray(np.repeat(column, width, axis=1), "L")
