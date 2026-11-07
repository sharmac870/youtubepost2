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


def test_script_is_written_shot_by_shot_and_narration_is_derived():
    raw = sample_script("UPI limits")
    raw["scenes"][0]["shots"][0]["search"] = "phone  payment!!"
    raw["scenes"][1]["tone"] = "shouty"
    s = ScriptJSON.model_validate(raw)
    assert s.scenes[0].narration.startswith("Here is the short version. UPI limits has taken over")
    assert s.scenes[0].shots[0].search == "phone payment"
    assert s.scenes[1].tone == "curious"                       # unknown tone falls back
    assert s.hook_show and s.word_count >= 60


def test_old_scripts_without_shots_still_load_and_render_one_line_per_scene():
    raw = sample_script("x")
    raw.pop("hook_show")
    for scene in raw["scenes"]:
        scene["narration"] = " ".join(shot["say"] for shot in scene.pop("shots"))
    s = ScriptJSON.model_validate(raw)
    lines = engine.RenderScript.of(s).lines()
    assert len(lines) == len(s.scenes) + 2 and lines[1]["say"] == s.scenes[0].narration


def test_every_sentence_is_its_own_line_with_its_own_picture():
    s = ScriptJSON.model_validate(sample_script("UPI limits"))
    lines = engine.RenderScript.of(s).lines()
    assert len(lines) == 1 + sum(len(sc.shots) for sc in s.scenes) + 1
    assert lines[2]["say"].startswith("UPI limits has taken over") and lines[2]["search"][0] == "india street phones"
    assert [line["scene"] for line in lines][:4] == [-1, 0, 0, 1] and lines[-1]["scene"] == -2


def test_pexels_search_picks_a_small_mp4_and_a_thumbnail():
    def handler(request):
        assert request.headers["Authorization"] == "k"
        assert request.url.params["orientation"] == "portrait"
        return httpx.Response(200, json={"videos": [{"id": 1, "url": "https://pexels.com/v/1", "duration": 9,
                                                     "image": "https://x/i.jpg", "user": {"name": "A"},
                                                     "video_pictures": [{"picture": "https://x/p0.jpg"},
                                                                        {"picture": "https://x/p1.jpg"}],
                                                     "video_files": [
            {"file_type": "video/mp4", "link": "https://x/4k.mp4", "width": 2160, "height": 3840},
            {"file_type": "video/mp4", "link": "https://x/hd.mp4", "width": 1080, "height": 1920},
            {"file_type": "video/mp4", "link": "https://x/sd.mp4", "width": 360, "height": 640}]}]})
    p = footage.Pexels("k", httpx.Client(transport=httpx.MockTransport(handler)))
    [clip] = p.search("mumbai train")
    assert clip.file_url == "https://x/hd.mp4" and clip.thumb == "https://x/p1.jpg" and clip.query == "mumbai train"


class Library:
    """A stock library whose clip ids say what they show."""
    name = "lib"

    def __init__(self, path="clip.mp4"):
        self.path = str(path)

    def search(self, q, portrait=True):
        return [footage.Clip("lib", f"{q}|{n}", "u", self.path, 1080, 1920, 8.0, query=q, thumb=f"https://t/{q}/{n}")
                for n in ("wrong", "right", "also-right")]


class PickyEditor:
    """Stands in for the vision check: only clips marked right fit their sentence."""
    min_score = 6

    def __init__(self):
        self.calls = []

    def rank(self, say, show, clips):
        self.calls.append(say)
        return sorted(((i, 8 if "right" in c.id else 2) for i, c in enumerate(clips)), key=lambda x: -x[1])


def _no_download(stock):
    stock.download = lambda clip: bool(setattr(clip, "path", clip.file_url)) or True
    return stock


def test_pick_uses_only_clips_the_editor_accepts_and_never_reuses_one(tmp_path):
    stock = _no_download(footage.Footage([Library()], tmp_path, judge=PickyEditor()))
    first = stock.pick("UPI failed", "phone", ["phone"])
    second = stock.pick("UPI failed again", "phone", ["phone"])
    third = stock.pick("and again", "phone", ["phone"])
    assert first.id == "phone|right" and first.score == 8 and first.said == "UPI failed"
    assert second.id == "phone|also-right" and third is None      # the wrong clip is never used
    stock.write_credits(tmp_path / "c.json")
    assert [c["said"] for c in json.loads((tmp_path / "c.json").read_text())] == ["UPI failed", "UPI failed again"]


def test_no_fitting_clip_falls_back_to_an_ai_image_then_to_nothing(tmp_path):
    class Rejects:
        min_score = 6

        def rank(self, say, show, clips):
            return [(i, 1) for i in range(len(clips))]

    class Images:
        def generate(self, prompt, portrait, cache):
            return footage.Clip("fal-flux", "img", "", "", 0, 0, 0.0, path=str(cache / "x.jpg"))

    with_images = _no_download(footage.Footage([Library()], tmp_path, judge=Rejects(), images=Images()))
    picked = with_images.pick("a sentence", "a picture", ["q"])
    assert picked.provider == "fal-flux" and picked.still and picked.said == "a sentence"
    without = _no_download(footage.Footage([Library()], tmp_path, judge=Rejects()))
    assert without.pick("a sentence", "a picture", ["q"]) is None


def test_claude_judge_sends_thumbnails_and_reads_scores():
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 10, 10)).save(buf, "JPEG")
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=buf.getvalue())))
    sent = {}

    class Client:
        class messages:
            @staticmethod
            def create(**kw):
                sent.update(kw)
                text = json.dumps({"scores": [{"clip": 0, "score": 3}, {"clip": 1, "score": 9}, {"clip": 7, "score": 10}]})
                return type("R", (), {"content": [type("B", (), {"type": "text", "text": text})()],
                                      "usage": type("U", (), {"input_tokens": 900, "output_tokens": 30})()})()

    usage = []
    judge = footage.ClaudeJudge(Client(), "claude-haiku-5-5", http=http, on_usage=lambda a, b: usage.append((a, b)))
    clips = [footage.Clip("lib", str(i), "", "", 1, 1, 5, thumb=f"https://t/{i}") for i in range(2)]
    assert judge.rank("A train is late", "a train platform", clips) == [(1, 9), (0, 3)]   # unknown clip 7 ignored
    images = [c for c in sent["messages"][0]["content"] if c["type"] == "image"]
    assert len(images) == 2 and "A train is late" in sent["messages"][0]["content"][0]["text"]
    assert usage == [(900, 30)]


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


def test_footage_render_cuts_to_each_sentences_picture_exactly_when_it_is_spoken(tmp_path, silent_voice,
                                                                                   monkeypatch):
    from ytp.render.qa import check

    clip = _test_clip(tmp_path / "clip.mp4")
    planned = {}
    real_plan = engine.plan_beats

    def spy(lines, voice, stock, fallback):
        beats = real_plan(lines, voice, _no_download(stock), fallback)
        planned.update(lines=lines, voice=voice, beats=beats, credits=list(stock.credits))
        return beats

    monkeypatch.setattr(engine, "plan_beats", spy)
    editor = PickyEditor()
    script = ScriptJSON.model_validate(sample_script("UPI limits"))
    video, thumb, seconds = engine.render(script, "25", tmp_path / "out", "v", "+10%", 58,
                                          providers=lambda: [Library(clip)], judge=lambda: editor)
    report = check(video)
    assert report.passed, report.problems
    lines, voice, beats = planned["lines"], planned["voice"], planned["beats"]
    spoken = [line["say"] for line in lines if line["scene"] != -2]
    assert list(dict.fromkeys(editor.calls)) == spoken   # every sentence got its own look, in order
    starts = [0.0] + [seg.start for seg in voice.segments[1:len(spoken)]]
    beat_starts = [b.start for b in beats]
    assert all(any(abs(s - b) < 1e-6 for b in beat_starts) for s in starts)   # cut lands on each sentence
    assert all(c.said in spoken and "right" in c.id for c in planned["credits"])
    assert len({p for p in silent_voice}) > 2                      # pace and pitch vary by tone
    assert thumb.exists() and seconds > 5


def test_footage_style_without_key_and_classic_style_render(tmp_path, silent_voice):
    script = ScriptJSON.model_validate(sample_script("UPI limits"))
    video, _, _ = engine.render(script, "28", tmp_path / "a", "v", "+10%", 58, providers=list)
    assert video.exists() and not (tmp_path / "a" / "credits.json").exists()
    silent_voice.clear()
    video, _, _ = engine.render(script, "28", tmp_path / "b", "v", "+10%", 58, style="classic")
    assert video.exists() and set(silent_voice) == {("+10%", "+0Hz")}   # classic voice is unchanged
