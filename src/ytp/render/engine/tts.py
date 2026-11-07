"""Free neural voiceover with word-level timings, for captions that follow the voice exactly.

edge-tts (Microsoft Edge's online neural voices) is the primary engine and reports when each word is
spoken. If it fails, gTTS is used and word timings are estimated from the length of each word.
Each part of the script (hook, scenes, outro) is voiced separately, so scene cuts land on the voice.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

import imageio_ffmpeg
import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 44100
GAP = 0.12  # Silence between parts, in seconds. Short, to keep the pace up.

# How each tone is voiced: rate change (percentage points on top of the base rate), pitch (Hz), and the pause
# after the line (s). People speed up and lift their pitch when excited, slow down when serious, and pause
# before a point lands; a flat rate with equal gaps is what makes TTS sound like a reader.
PROSODY = {
    "hook": (6, 4, 0.32),
    "excited": (8, 5, 0.18),
    "funny": (5, 3, 0.30),
    "curious": (0, 2, 0.24),
    "serious": (-6, -3, 0.34),
    "calm": (-8, -2, 0.26),
    "outro": (0, 2, 0.0),
}


def shift_rate(base: str, delta: int) -> str:
    """'+10%' shifted by delta points, kept within edge-tts' sensible range."""
    try:
        value = int(base.strip().rstrip("%"))
    except ValueError:
        value = 0
    value = max(-30, min(45, value + delta))
    return f"{value:+d}%"


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class Segment:
    """One voiced part of the script, placed on the video's timeline."""
    index: int
    text: str
    start: float
    end: float
    words: list[Word]


@dataclass
class Voiceover:
    path: Path
    duration: float
    segments: list[Segment]

    @property
    def words(self) -> list[Word]:
        return [word for segment in self.segments for word in segment.words]


def decode(path: Path) -> np.ndarray:
    """Any audio file to mono float samples at SAMPLE_RATE."""
    raw = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-i", str(path), "-f", "s16le",
         "-ac", "1", "-ar", str(SAMPLE_RATE), "-"],
        capture_output=True, check=True, timeout=120,
    ).stdout
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def write_wav(path: Path, samples: np.ndarray) -> None:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())


def trim_silence(samples: np.ndarray, threshold: float = 0.01) -> tuple[np.ndarray, float]:
    """Cuts leading and trailing silence. Returns the samples and how many seconds were cut from the front."""
    loud = np.flatnonzero(np.abs(samples) > threshold)
    if not len(loud):
        return samples, 0.0
    start = max(0, loud[0] - int(0.02 * SAMPLE_RATE))
    end = min(len(samples), loud[-1] + int(0.08 * SAMPLE_RATE))
    return samples[start:end], start / SAMPLE_RATE


async def _edge(text: str, voice: str, rate: str, path: Path, pitch: str = "+0Hz") -> list[Word]:
    import edge_tts

    proxy = os.environ.get("HTTPS_PROXY") or None  # aiohttp ignores proxy env vars unless told
    try:
        communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, boundary="WordBoundary", proxy=proxy)
    except TypeError:  # edge-tts before 7.0 always reported word boundaries
        communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, proxy=proxy)
    words = []
    with open(path, "wb") as audio:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / 1e7  # 100-nanosecond units
                words.append(Word(chunk["text"], start, start + chunk["duration"] / 1e7))
    return words


def estimate_words(text: str, duration: float) -> list[Word]:
    """Spreads words over the clip in proportion to their length, for engines that give no timings."""
    tokens = text.split()
    weights = np.array([len(t) + 2 for t in tokens], dtype=float)
    edges = np.concatenate([[0], np.cumsum(weights)]) / weights.sum() * duration
    return [Word(t, edges[i], edges[i + 1]) for i, t in enumerate(tokens)]


def _gtts(text: str, path: Path, language: str = "en") -> None:
    from gtts import gTTS

    gTTS(text=text, lang=language, tld="com").save(str(path))


def speak(text: str, voice: str, rate: str, folder: Path, name: str,
          pitch: str = "+0Hz") -> tuple[np.ndarray, list[Word]]:
    mp3 = folder / f"{name}.mp3"
    words: list[Word] = []
    try:
        words = asyncio.run(_edge(text, voice, rate, mp3, pitch))
        samples = decode(mp3)
    except Exception as exc:  # noqa: BLE001 - fall back to the second free engine
        log.warning("edge-tts failed (%s); falling back to gTTS", exc)
        _gtts(text, mp3)
        samples = decode(mp3)
        words = []
    samples, cut = trim_silence(samples)
    duration = len(samples) / SAMPLE_RATE
    if not words:
        words = estimate_words(text, duration)
    else:
        words = [Word(w.text, max(0.0, w.start - cut), max(0.0, w.end - cut)) for w in words]
    return samples, words


def synthesize(parts: list[str], voice: str, rate: str, folder: Path, tones: list[str] | None = None) -> Voiceover:
    """Voices each part. With `tones` (one per part: "hook", a scene tone, "outro"), each part gets its own pace,
    pitch and pause; without them every part is voiced the same way with a short fixed gap."""
    folder.mkdir(parents=True, exist_ok=True)
    timeline: list[np.ndarray] = []
    segments: list[Segment] = []
    cursor = 0.0
    for index, text in enumerate(parts):
        text = re.sub(r"\s+", " ", text).strip()
        if tones:
            d_rate, d_pitch, pause = PROSODY.get(tones[index] if index < len(tones) else "", PROSODY["curious"])
            part_rate, pitch = shift_rate(rate, d_rate), f"{d_pitch:+d}Hz"
        else:
            part_rate, pitch, pause = rate, "+0Hz", GAP
        gap = np.zeros(int(pause * SAMPLE_RATE), dtype=np.float32)
        samples, words = speak(text, voice, part_rate, folder, f"part{index:02d}", pitch)
        length = len(samples) / SAMPLE_RATE
        segments.append(Segment(
            index, text, cursor, cursor + length,
            [Word(w.text, cursor + w.start, cursor + w.end) for w in words],
        ))
        timeline += [samples, gap]
        cursor += length + pause
    audio = np.concatenate(timeline)
    peak = float(np.max(np.abs(audio))) or 1.0
    audio = audio * (0.89 / peak)  # about -1 dBFS
    path = folder / "voice.wav"
    write_wav(path, audio)
    log.info("voiceover: %.1f s across %d parts", len(audio) / SAMPLE_RATE, len(parts))
    return Voiceover(path, len(audio) / SAMPLE_RATE, segments)
