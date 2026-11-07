"""The footage render style: shot lists, stock search, beats, the host, prosody and a full render offline."""
import json
import subprocess
from pathlib import Path

import httpx
import numpy as np
import pytest

from ytp.common.models import ScriptJSON
from ytp.fakes import sample_script
from ytp.render import engine
from ytp.render.engine import builder, footage, mascot, tts


def test_script_keeps_shot_list_and_tone_and_cleans_them():
    raw = sample_script("UPI limits")
    raw["scenes"][0]["broll"] = ["phone  payment!!", "Phone payment", "a", "b", "c"]
    raw["scenes"][1]["tone"] = "shouty"
    s = ScriptJSON.model_validate(raw)
    assert s.scenes[0].broll == ["phone payment", "a", "b"]   # deduped, cleaned, max 3
    assert s.scenes[1].tone == "curious"                       # unknown tone falls back
    assert s.hook_broll


def test_old_scripts_without_shot_list_still_load():
    raw = sample_script("x")
    raw.pop("hook_broll")
    for scene in raw["scenes"]:
        scene.pop("broll"), scene.pop("tone")
    s = ScriptJSON.model_validate(raw)
    assert s.hook_broll == [] and s.scenes[0].broll == [] and s.scenes[0].tone == "curious"


def test_pexels_search_picks_a_small_mp4_that_fills_the_frame():
    def handler(request):
        assert request.headers["Authorization"] == "k"
        assert request.url.params["orientation"] == "portrait"
        return httpx.Response(200, json={"videos": [{"id": 1, "url": "https://pexels.com/v/1", "duration": 9,
                                                     "user": {"name": "A"}, "video_files": [
            {"file_type": "video/mp4", "link": "https://x/4k.mp4", "width": 2160, "height": 3840},
            {"file_type": "video/mp4", "link": "https://x/hd.mp4", "width": 1080, "height": 1920},
            {"file_type": "video/mp4", "link": "https://x/sd.mp4", "width": 360, "height": 640}]}]})
    p = footage.Pexels("k", httpx.Client(transport=httpx.MockTransport(handler)))
    [clip] = p.search("mumbai train")
    assert clip.file_url == "https://x/hd.mp4" and clip.duration == 9 and clip.query == "mumbai train"


def test_footage_never_reuses_a_clip_and_survives_failed_searches(tmp_path):
    class Broken:
        name = "broken"

        def search(self, *a, **k):
            raise httpx.ConnectError("down")

    class Two:
        name = "two"

        def search(self, q, portrait=True):
            return [footage.Clip("two", str(i), "u", f"https://x/{i}.mp4", 1080, 1920, 8, query=q) for i in (1, 2)]

    stock = footage.Footage([Broken(), Two()], tmp_path)
    stock.download = lambda clip: bool(setattr(clip, "path", "p")) or True
    first, second, third = stock.find(["a"]), stock.find(["a"]), stock.find(["a"])
    assert {first.id, second.id} == {"1", "2"} and third is None
    stock.write_credits(tmp_path / "c.json")
    assert [c["id"] for c in json.loads((tmp_path / "c.json").read_text())] == [first.id, second.id]


def test_beats_are_two_to_three_seconds_and_cut_on_words():
    shot = builder.Shot("scene", 10.0, 18.0, "x")
    words = [tts.Word(str(i), 10 + i * 0.4, 10 + i * 0.4 + 0.3) for i in range(20)]
    beats = builder.split_beats(shot, words)
    assert len(beats) == 3 and beats[0][0] == 10.0 and beats[-1][1] == 18.0
    assert all(any(abs(b[0] - w.start) < 1e-9 for w in words) for b in beats[1:])


def test_prosody_shifts_rate_within_bounds():
    assert tts.shift_rate("+10%", 8) == "+18%"
    assert tts.shift_rate("+40%", 20) == "+45%"
    assert tts.shift_rate("bad", -6) == "-6%"


def test_lipsync_opens_the_mouth_only_while_speaking():
    sr = 44100
    samples = np.concatenate([np.zeros(sr), 0.5 * np.sin(np.arange(sr) / sr * 2 * np.pi * 200)]).astype(np.float32)
    sync = mascot.Lipsync(samples, sr, [tts.Word("hello", 1.0, 2.0)])
    assert sync.mouth(0.5) == (0, False)
    assert sync.mouth(1.2)[0] >= 2
    img = mascot.draw(mascot.pose_at(1.2, sync, "excited"), (64, 150, 255), 300)
    assert img.size[1] == 300 and img.mode == "RGBA"


@pytest.fixture
def silent_voice(monkeypatch):
    """edge-tts needs the network; tests voice each part as a tone of the right length."""
    calls = []

    def speak(text, voice, rate, folder, name, pitch="+0Hz"):
        calls.append((rate, pitch))
        seconds = max(1.0, len(text.split()) * 0.33)
        t = np.arange(int(seconds * tts.SAMPLE_RATE)) / tts.SAMPLE_RATE
        samples = (0.3 * np.sin(2 * np.pi * 180 * t) * (np.sin(2 * np.pi * 3 * t) > 0)).astype(np.float32)
        return samples, tts.estimate_words(text, seconds)

    monkeypatch.setattr(tts, "speak", speak)
    return calls


def _test_clip(path: Path) -> Path:
    subprocess.run([builder.imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=640x360:rate=30:duration=3", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def test_footage_render_end_to_end(tmp_path, silent_voice):
    from ytp.render.qa import check

    clip = _test_clip(tmp_path / "clip.mp4")

    class Local:
        name = "local"

        def search(self, q, portrait=True):
            return [footage.Clip("local", f"{q}-{i}", "u", str(clip), 640, 360, 3.0, query=q) for i in range(30)]

    footage.Footage.download, original = (lambda self, c: bool(setattr(c, "path", c.file_url)) or True,
                                          footage.Footage.download)
    try:
        script = ScriptJSON.model_validate(sample_script("UPI limits"))
        video, thumb, seconds = engine.render(script, "25", tmp_path / "out", "v", "+10%", 58,
                                              providers=lambda: [Local()])
    finally:
        footage.Footage.download = original
    report = check(video)
    assert report.passed, report.problems
    credits = json.loads((tmp_path / "out" / "credits.json").read_text())
    assert len(credits) >= 6                        # a clip every 2-3 s, not one per scene
    assert len({p for p in silent_voice}) > 2       # pace and pitch vary by tone
    assert thumb.exists() and seconds > 5


def test_footage_style_without_key_and_classic_style_render(tmp_path, silent_voice):
    script = ScriptJSON.model_validate(sample_script("UPI limits"))
    video, _, _ = engine.render(script, "28", tmp_path / "a", "v", "+10%", 58, providers=list)
    assert video.exists() and not (tmp_path / "a" / "credits.json").exists()
    silent_voice.clear()
    video, _, _ = engine.render(script, "28", tmp_path / "b", "v", "+10%", 58, style="classic")
    assert video.exists() and set(silent_voice) == {("+10%", "+0Hz")}   # classic voice is unchanged
