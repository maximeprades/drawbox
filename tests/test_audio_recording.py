"""Audio recording fallback behavior."""

import logging
import sys
import types
from urllib.error import HTTPError

import numpy as np

# The cloud test image does not have the native PortAudio library installed.
# Provide the tiny sounddevice surface this test patches before importing the
# Pi runtime module.
fake_sounddevice = types.SimpleNamespace(
    default=types.SimpleNamespace(device=[-1, None]),
    PortAudioError=RuntimeError,
    query_devices=lambda device=None: [],
    InputStream=None,
)
sys.modules.setdefault("sounddevice", fake_sounddevice)

import drawbox


def test_candidate_input_devices_prioritizes_usb_then_default(monkeypatch):
    devices = [
        {"name": "Built-in Output", "max_input_channels": 0},
        {"name": "USB PnP Sound Device: Audio (hw:3,0)", "max_input_channels": 1},
        {"name": "Built-in Mic", "max_input_channels": 1},
    ]
    monkeypatch.setattr(drawbox.sd, "query_devices", lambda device=None: devices if device is None else devices[device])
    monkeypatch.setattr(drawbox.sd.default, "device", [2, None], raising=False)

    assert drawbox._candidate_input_devices() == [1, 2, None]


def test_candidate_input_devices_ignores_invalid_default_when_no_inputs(monkeypatch):
    devices = [
        {"name": "Built-in Output", "max_input_channels": 0},
    ]
    monkeypatch.setattr(drawbox.sd, "query_devices", lambda device=None: devices if device is None else devices[device])
    monkeypatch.setattr(drawbox.sd.default, "device", [-1, None], raising=False)

    assert drawbox._candidate_input_devices() == []


def test_record_audio_tries_next_device_after_open_failure(monkeypatch, tmp_path):
    devices = [
        {"name": "USB PnP Sound Device: Audio (hw:3,0)", "max_input_channels": 1},
        {"name": "USB PnP Sound Device: Audio (hw:2,0)", "max_input_channels": 1},
    ]
    attempts = []
    in_callback = {"value": False}

    def query_devices(device=None):
        if in_callback["value"]:
            raise AssertionError("query_devices must not run from audio callback")
        return devices if device is None else devices[device]

    monkeypatch.setattr(drawbox.sd, "query_devices", query_devices)
    monkeypatch.setattr(drawbox.sd.default, "device", [-1, None], raising=False)
    monkeypatch.setattr(drawbox.time, "sleep", lambda _seconds: None)

    class FakeInputStream:
        def __init__(self, samplerate, channels, callback, device):
            self.callback = callback
            self.device = device
            attempts.append(device)

        def __enter__(self):
            if self.device == 0:
                raise drawbox.sd.PortAudioError("stale ALSA card")
            audio = np.ones((drawbox.SAMPLE_RATE, 1), dtype=np.float32)
            in_callback["value"] = True
            self.callback(audio, len(audio), None, "input overflow")
            in_callback["value"] = False
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(drawbox.sd, "InputStream", FakeInputStream)
    monkeypatch.setattr(drawbox.sf, "write", lambda path, audio, sample_rate: None)

    path = drawbox.record_audio(seconds=1)

    assert attempts == [0, 1]
    assert path is not None


def test_record_audio_returns_none_when_all_devices_fail(monkeypatch):
    devices = [
        {"name": "USB PnP Sound Device: Audio (hw:3,0)", "max_input_channels": 1},
    ]

    monkeypatch.setattr(drawbox.sd, "query_devices", lambda device=None: devices if device is None else devices[device])
    monkeypatch.setattr(drawbox.sd.default, "device", [-1, None], raising=False)

    class FailingInputStream:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            raise drawbox.sd.PortAudioError("illegal device")

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(drawbox.sd, "InputStream", FailingInputStream)

    assert drawbox.record_audio(seconds=1) is None


def _voice_feedback_without_warmup(tmp_path):
    feedback = object.__new__(drawbox.VoiceFeedback)
    feedback._cache = {}
    feedback._joke_paths = []
    feedback._silence_path = None
    feedback._tts_rate_limited_until = 0.0
    feedback._tts_rate_limit_logged = False
    return feedback


def test_tts_rate_limit_stops_additional_cache_requests(monkeypatch, tmp_path, caplog):
    feedback = _voice_feedback_without_warmup(tmp_path)
    monkeypatch.setattr(drawbox, "CACHE_DIR", tmp_path)
    attempts = []

    def rate_limited(text, _out_path):
        attempts.append(text)
        raise HTTPError(
            url="https://api.elevenlabs.io",
            code=429,
            msg="Too Many Requests",
            hdrs={"Retry-After": "120"},
            fp=None,
        )

    monkeypatch.setattr(feedback, "_elevenlabs_tts", rate_limited)

    with caplog.at_level(logging.WARNING, logger="drawbox"):
        assert feedback._generate_one("first line") is None
        assert feedback._generate_one("second line") is None

    assert attempts == ["first line"]
    assert feedback._tts_rate_limit_remaining() > 0
    assert "rate-limited (HTTP 429)" in caplog.text


def test_live_tts_uses_espeak_during_rate_limit(monkeypatch, tmp_path):
    feedback = _voice_feedback_without_warmup(tmp_path)
    feedback._tts_rate_limited_until = drawbox.time.time() + 30
    spoken = []

    monkeypatch.setattr(
        feedback,
        "_elevenlabs_tts",
        lambda *_args: (_ for _ in ()).throw(AssertionError("should not call ElevenLabs")),
    )
    monkeypatch.setattr(drawbox.subprocess, "run", lambda args, check=False: spoken.append(args))

    feedback._play_live("hello")

    assert spoken == [["espeak", "hello"]]


def test_warm_up_loads_disk_cache_after_rate_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(drawbox, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(drawbox, "VOICE_LINES", {
        "first": "needs network",
        "second": "already cached",
        "multi": ["missing option", "cached option"],
    })
    monkeypatch.setattr(drawbox, "KIDS_JOKES", ["cached joke"])
    monkeypatch.setattr(drawbox.VoiceFeedback, "_ensure_silence_file", lambda self: None)

    helper = _voice_feedback_without_warmup(tmp_path)
    cached_line = helper._tts_path("already cached")
    cached_option = helper._tts_path("cached option")
    cached_joke = helper._tts_path("cached joke")
    for cached_path in (cached_line, cached_option, cached_joke):
        cached_path.write_bytes(b"mp3")

    attempts = []

    def rate_limited(self, text, _out_path):
        attempts.append(text)
        raise HTTPError(
            url="https://api.elevenlabs.io",
            code=429,
            msg="Too Many Requests",
            hdrs={"Retry-After": "120"},
            fp=None,
        )

    monkeypatch.setattr(drawbox.VoiceFeedback, "_elevenlabs_tts", rate_limited)

    feedback = drawbox.VoiceFeedback()

    assert attempts == ["needs network"]
    assert "first" not in feedback._cache
    assert feedback._cache["second"] == cached_line
    assert feedback._cache["multi"] == [cached_option]
    assert feedback._joke_paths == [cached_joke]


def test_play_falls_back_for_empty_cache_list(monkeypatch, tmp_path):
    feedback = _voice_feedback_without_warmup(tmp_path)
    feedback._cache = {"empty": []}
    played_live = []

    monkeypatch.setattr(drawbox, "VOICE_LINES", {"empty": "fallback line"})
    monkeypatch.setattr(feedback, "_play_live", lambda text: played_live.append(text))

    feedback.play("empty")

    assert played_live == ["fallback line"]
