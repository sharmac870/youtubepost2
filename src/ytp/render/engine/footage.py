"""Real footage for each line of the script: free stock clips from Pexels and Pixabay, cut every 2-3 seconds.

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
from collections.abc import Callable
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
                out.append(Clip(self.name, str(v["id"]), v.get("url", ""), f["link"], f["width"], f["height"],
                                float(v.get("duration") or 0), (v.get("user") or {}).get("name", ""), query))
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
                out.append(Clip(self.name, str(v["id"]), v.get("pageURL", ""), s["url"], s["width"], s["height"],
                                float(v.get("duration") or 0), v.get("user", ""), query))
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
    _results: dict = field(default_factory=dict)

    def find(self, queries: list[str], min_seconds: float = 2.0) -> Clip | None:
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
                    if (clip.provider, clip.id) in self.used or 0 < clip.duration < min_seconds:
                        continue
                    if self.download(clip):
                        self.used.add((clip.provider, clip.id))
                        self.credits.append(clip)
                        return clip
        return None

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
        self.proc = subprocess.Popen(
            [ffmpeg(), "-v", "error", "-stream_loop", "-1", "-ss", f"{start:.2f}", "-i", path, "-t", f"{seconds + 0.5:.2f}",
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
