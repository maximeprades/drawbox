"""Audio recording fallback behavior."""

import base64
import json
import logging
import urllib.error
import urllib.request

import httpx
import numpy as np
import pytest
from openai import RateLimitError

import drawbox
import drawbox_core


def _rate_limit_error(retry_after="120"):
    request = httpx.Request("POST", "https://ai-gateway.vercel.sh/v1/audio/speech")
    response = httpx.Response(
        429, request=request, headers={"retry-after": retry_after},
    )
    return RateLimitError("Too Many Requests", response=response, body=None)


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


def test_record_audio_stops_early_after_speech_ends(monkeypatch):
    """VAD: speech then 1.6s of quiet must end a 10s window early.

    Time is derived from captured samples, so the fake stream finishes
    instantly even though the requested window is 10 seconds.
    """
    devices = [
        {"name": "USB PnP Sound Device: Audio (hw:3,0)", "max_input_channels": 1},
    ]
    written = {}

    monkeypatch.setattr(drawbox.sd, "query_devices", lambda device=None: devices if device is None else devices[device])
    monkeypatch.setattr(drawbox.sd.default, "device", [-1, None], raising=False)
    monkeypatch.setattr(drawbox.time, "sleep", lambda _seconds: None)

    loud = np.full((int(0.3 * drawbox.SAMPLE_RATE), 1), 0.5, dtype=np.float32)
    quiet = np.full((int(1.6 * drawbox.SAMPLE_RATE), 1),
                    drawbox.QUIET_PEAK / 3, dtype=np.float32)

    class TalkThenQuietStream:
        def __init__(self, samplerate, channels, callback, device):
            self.callback = callback

        def __enter__(self):
            self.callback(loud, len(loud), None, None)
            self.callback(quiet, len(quiet), None, None)
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(drawbox.sd, "InputStream", TalkThenQuietStream)
    monkeypatch.setattr(
        drawbox.sf, "write",
        lambda path, audio, sample_rate: written.setdefault("seconds", len(audio) / sample_rate))

    assert drawbox.record_audio(seconds=10) is not None
    # The take is the 1.9s delivered, not the 10s window.
    assert written["seconds"] < 2.0


def test_record_audio_rejects_room_tone(monkeypatch):
    """A quiet take must not reach Whisper (it hallucinates words from
    near-silence — the ESP32 box once printed invented Japanese), and it
    must not trigger device fallback (each retry costs a full window)."""
    devices = [
        {"name": "USB PnP Sound Device: Audio (hw:3,0)", "max_input_channels": 1},
        {"name": "Built-in Mic", "max_input_channels": 1},
    ]
    attempts = []

    monkeypatch.setattr(drawbox.sd, "query_devices", lambda device=None: devices if device is None else devices[device])
    monkeypatch.setattr(drawbox.sd.default, "device", [-1, None], raising=False)
    monkeypatch.setattr(drawbox.time, "sleep", lambda _seconds: None)

    class QuietInputStream:
        def __init__(self, samplerate, channels, callback, device):
            self.callback = callback
            attempts.append(device)

        def __enter__(self):
            audio = np.full((drawbox.SAMPLE_RATE, 1), drawbox.QUIET_PEAK / 3,
                            dtype=np.float32)
            self.callback(audio, len(audio), None, None)
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(drawbox.sd, "InputStream", QuietInputStream)

    assert drawbox.record_audio(seconds=1) is None
    assert attempts == [0]


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


def test_tts_rate_limit_stops_additional_cache_requests(monkeypatch, tmp_path, caplog):
    feedback = drawbox.VoiceFeedback()
    monkeypatch.setattr(drawbox, "CACHE_DIR", tmp_path)
    attempts = []

    def rate_limited(text, _out_path):
        attempts.append(text)
        raise _rate_limit_error()

    monkeypatch.setattr(feedback, "_gateway_tts", rate_limited)

    with caplog.at_level(logging.WARNING, logger="drawbox"):
        assert feedback._generate_one("first line") is None
        assert feedback._generate_one("second line") is None

    assert attempts == ["first line"]
    assert feedback._tts_rate_limit_remaining() > 0
    assert "rate-limited (HTTP 429)" in caplog.text


def test_gateway_tts_http_429_triggers_rate_limit(monkeypatch, tmp_path, caplog):
    feedback = drawbox.VoiceFeedback()
    monkeypatch.setattr(drawbox, "CACHE_DIR", tmp_path)
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "vck-test")
    attempts = []

    def rate_limited_urlopen(req, timeout=None):
        attempts.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests",
                                     {"Retry-After": "120"}, None)

    monkeypatch.setattr(urllib.request, "urlopen", rate_limited_urlopen)

    with caplog.at_level(logging.WARNING, logger="drawbox"):
        assert feedback._generate_one("first line") is None
        assert feedback._generate_one("second line") is None

    assert attempts == [drawbox_core.AI_GATEWAY_SPEECH_URL]
    assert feedback._tts_rate_limit_remaining() > 0
    assert "rate-limited (HTTP 429)" in caplog.text


def test_gateway_tts_http_error_warns_without_rate_limit(monkeypatch, tmp_path, caplog):
    feedback = drawbox.VoiceFeedback()
    monkeypatch.setattr(drawbox, "CACHE_DIR", tmp_path)
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "vck-test")

    def not_found_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", not_found_urlopen)

    with caplog.at_level(logging.WARNING, logger="drawbox"):
        assert feedback._generate_one("a line") is None

    assert feedback._tts_rate_limit_remaining() == 0
    assert "gateway TTS HTTP 404 failed" in caplog.text


def test_live_tts_uses_espeak_during_rate_limit(monkeypatch):
    feedback = drawbox.VoiceFeedback()
    feedback._tts_rate_limited_until = drawbox.time.time() + 30
    spoken = []

    monkeypatch.setattr(
        feedback,
        "_gateway_tts",
        lambda *_args: (_ for _ in ()).throw(AssertionError("should not call Gateway TTS")),
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

    feedback = drawbox.VoiceFeedback()
    cached_line = feedback._tts_path("already cached")
    cached_option = feedback._tts_path("cached option")
    cached_joke = feedback._tts_path("cached joke")
    for cached_path in (cached_line, cached_option, cached_joke):
        cached_path.write_bytes(b"mp3")

    attempts = []

    def rate_limited(self, text, _out_path):
        attempts.append(text)
        raise _rate_limit_error()

    monkeypatch.setattr(drawbox.VoiceFeedback, "_gateway_tts", rate_limited)

    feedback.warm_up()

    assert attempts == ["needs network"]
    assert "first" not in feedback._cache
    assert feedback._cache["second"] == cached_line
    assert feedback._cache["multi"] == [cached_option]
    assert feedback._joke_paths == [cached_joke]


def test_play_falls_back_to_live_tts_for_uncached_key(monkeypatch):
    feedback = drawbox.VoiceFeedback()
    played_live = []

    monkeypatch.setattr(drawbox, "VOICE_LINES", {"missing": "fallback line"})
    monkeypatch.setattr(feedback, "_play_live", lambda text: played_live.append(text))

    feedback.play("missing")

    assert played_live == ["fallback line"]


def test_transcribe_posts_wav_and_unlinks_recording(drawbox_dir, monkeypatch, tmp_path):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "vck-test")
    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"RIFF-fake-wav")

    captured = {}

    class FakeJsonResponse:
        def read(self, n=-1):
            return json.dumps({"text": "draw a cat"}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return FakeJsonResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert drawbox.transcribe(str(clip)) == "draw a cat"

    req = captured["req"]
    assert req.full_url == drawbox_core.AI_GATEWAY_TRANSCRIPTION_URL
    assert req.get_header("Authorization") == "Bearer vck-test"
    assert req.get_header("Ai-gateway-protocol-version") == "0.0.1"
    assert req.get_header("Ai-transcription-model-specification-version") == "4"
    assert req.get_header("Ai-model-id") == "openai/whisper-1"
    body = json.loads(req.data)
    assert body["audio"] == base64.b64encode(b"RIFF-fake-wav").decode()
    assert body["mediaType"] == "audio/wav"
    assert not clip.exists()


def test_transcribe_unlinks_recording_when_gateway_fails(drawbox_dir, monkeypatch, tmp_path):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "vck-test")
    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"RIFF-fake-wav")

    def failing_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", failing_urlopen)

    with pytest.raises(urllib.error.HTTPError):
        drawbox.transcribe(str(clip))

    assert not clip.exists()
