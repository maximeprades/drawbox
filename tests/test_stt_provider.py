"""STT provider dispatch: gateway Whisper (default) vs xAI Grok STT."""

import json
import urllib.request

import pytest

import drawbox_core


class _FakeJsonResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self, n=-1):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_transcribe_audio_dispatches_to_grok(drawbox_dir, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    drawbox_core.save_settings({"stt_provider": "grok"})
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return _FakeJsonResponse({"text": "draw a cat", "language": "en",
                                  "duration": 1.9})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert drawbox_core.transcribe_audio(b"RIFF-fake-wav") == "draw a cat"

    req = captured["req"]
    assert req.full_url == drawbox_core.GROK_STT_URL
    assert req.get_header("Authorization") == "Bearer xai-test"
    ctype = req.get_header("Content-type")
    assert ctype.startswith("multipart/form-data; boundary=")
    boundary = ctype.split("boundary=", 1)[1]
    assert f"--{boundary}".encode() in req.data
    assert b'name="file"' in req.data
    assert b'filename="audio.wav"' in req.data
    assert b"RIFF-fake-wav" in req.data
    # The file part must be terminated by the closing boundary.
    assert req.data.endswith(f"\r\n--{boundary}--\r\n".encode())


def test_grok_stt_requires_xai_key(drawbox_dir, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    drawbox_core.save_settings({"stt_provider": "grok"})
    with pytest.raises(RuntimeError, match="XAI_API_KEY"):
        drawbox_core.transcribe_audio(b"RIFF-fake-wav")


def test_transcribe_audio_defaults_to_gateway(drawbox_dir, monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "vck-test")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return _FakeJsonResponse({"text": "a boat"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert drawbox_core.transcribe_audio(b"RIFF-fake-wav") == "a boat"
    assert captured["req"].full_url == drawbox_core.AI_GATEWAY_TRANSCRIPTION_URL


def test_load_settings_clamps_unknown_stt_provider(drawbox_dir):
    drawbox_core.SETTINGS_FILE.write_text(json.dumps({"stt_provider": "siri"}))
    assert drawbox_core.load_settings()["stt_provider"] == "gateway"


def test_settings_api_round_trips_stt_and_ack(client):
    r = client.post("/api/settings", json={"stt_provider": "grok",
                                           "natural_ack": False})
    assert r.get_json()["ok"] is True
    s = client.get("/api/settings").get_json()
    assert s["stt_provider"] == "grok"
    assert s["natural_ack"] is False
    # Unknown provider is ignored, bad ack type is a 400.
    client.post("/api/settings", json={"stt_provider": "siri"})
    assert client.get("/api/settings").get_json()["stt_provider"] == "grok"
    r = client.post("/api/settings", json={"natural_ack": "yes"})
    assert r.status_code == 400
