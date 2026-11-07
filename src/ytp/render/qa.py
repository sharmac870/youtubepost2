"""MediaQAService (LLD Sections 2.2, 4.1 and 13): spec checks before anything is published. Fails closed.

Uses the ffmpeg binary (system ffmpeg in the image, or the one bundled with imageio-ffmpeg), parsing its
stream report plus one pass of ebur128 loudness and blackdetect.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

MAX_BYTES = 300 * 1024 * 1024


@dataclass
class QAReport:
    passed: bool
    width: int = 0
    height: int = 0
    fps: float = 0.0
    video_codec: str = ""
    audio_codec: str = ""
    sample_rate: int = 0
    duration_s: float = 0.0
    size_bytes: int = 0
    loudness_lufs: float | None = None
    black_s: float = 0.0
    problems: list[str] = field(default_factory=list)


def ffmpeg_exe() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def parse(report: str) -> dict:
    out: dict = {}
    if m := re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", report):
        out["duration_s"] = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    if m := re.search(r"Stream #\S+.*?Video: (\w+).*?, (\d{2,5})x(\d{2,5})", report):
        out["video_codec"], out["width"], out["height"] = m[1], int(m[2]), int(m[3])
    if m := re.search(r"Video:.*?([\d.]+) fps", report):
        out["fps"] = float(m[1])
    if m := re.search(r"Audio: (\w+).*?(\d{4,6}) Hz", report):
        out["audio_codec"], out["sample_rate"] = m[1], int(m[2])
    if m := re.search(r"Integrated loudness:\s+I:\s+(-?[\d.]+) LUFS", report):
        out["loudness_lufs"] = float(m[1])
    out["black_s"] = round(sum(float(d) for d in re.findall(r"black_duration:([\d.]+)", report)), 2)
    return out


def check(path: Path, max_seconds: float = 90.0, run=subprocess.run) -> QAReport:
    path = Path(path)
    size = path.stat().st_size
    result = run([ffmpeg_exe(), "-hide_banner", "-nostats", "-i", str(path),
                  "-vf", "blackdetect=d=1.5:pix_th=0.05", "-af", "ebur128", "-f", "null", "-"],
                 capture_output=True, text=True, timeout=600)
    info = parse(result.stderr)
    r = QAReport(passed=False, size_bytes=size, **info)
    if (r.width, r.height) != (1080, 1920):
        r.problems.append(f"resolution {r.width}x{r.height}, needs 1080x1920")
    if r.video_codec not in ("h264", "hevc"):
        r.problems.append(f"video codec {r.video_codec or 'missing'}, needs H.264 or HEVC")
    if r.audio_codec != "aac":
        r.problems.append(f"audio codec {r.audio_codec or 'missing'}, needs AAC")
    if r.sample_rate > 48000:
        r.problems.append(f"audio {r.sample_rate} Hz, max 48 kHz")
    if not 23 <= r.fps <= 60:
        r.problems.append(f"{r.fps} fps, needs 23-60")
    if not 5 <= r.duration_s <= max_seconds:
        r.problems.append(f"duration {r.duration_s:.1f} s, needs 5-{max_seconds:.0f} s")
    if size > MAX_BYTES:
        r.problems.append(f"{size / 1e6:.0f} MB, max 300 MB")
    if r.loudness_lufs is not None and not -24 <= r.loudness_lufs <= -6:
        r.problems.append(f"loudness {r.loudness_lufs} LUFS, target -14 (allowed -24 to -6)")
    if r.black_s > 3:
        r.problems.append(f"{r.black_s} s of black frames")
    r.passed = not r.problems
    return r
