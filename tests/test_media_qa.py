
from ytp.fakes import fake_render
from ytp.render.qa import check, parse

REPORT = """Input #0, mov,mp4, from 'v.mp4':
  Duration: 00:00:45.20, start: 0.000000, bitrate: 2400 kb/s
  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(progressive), 1080x1920, 2200 kb/s, 30 fps, 30 tbr
  Stream #0:1[0x2](und): Audio: aac (LC) (mp4a / 0x6134706D), 44100 Hz, stereo, fltp, 192 kb/s
[blackdetect @ 0x1] black_start:0 black_end:0.5 black_duration:0.5
  Integrated loudness:
    I:         -15.1 LUFS
"""


def test_parse_ffmpeg_report():
    info = parse(REPORT)
    assert info == {"duration_s": 45.2, "video_codec": "h264", "width": 1080, "height": 1920, "fps": 30.0,
                    "audio_codec": "aac", "sample_rate": 44100, "loudness_lufs": -15.1, "black_s": 0.5}


def test_real_clip_passes_and_landscape_fails(tmp_path):
    video, _, _ = fake_render(None, "28", tmp_path / "a", "", "")
    report = check(video)
    assert report.passed, report.problems
    import subprocess
    from ytp.render.qa import ffmpeg_exe
    wide = tmp_path / "wide.mp4"
    subprocess.run([ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(video), "-vf", "scale=1920:1080", str(wide)],
                   check=True)
    assert not check(wide).passed
