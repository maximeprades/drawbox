"""STT provider dispatch: gateway Whisper (default) vs Grok STT via Gateway."""

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
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "vck-test")
    drawbox_core.save_settings({"stt_provider": "grok"})
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return _FakeJsonResponse({"text": "draw a cat", "language": "en",
                                  "duration": 1.9})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert drawbox_core.transcribe_audio(b"RIFF-fake-wav") == "draw a cat"

    req = captured["req"]
    assert req.full_url == drawbox_core.AI_GATEWAY_TRANSCRIPTION_URL
    assert req.get_header("Authorization") == "Bearer vck-test"
    assert req.get_header("Ai-model-id") == "xai/grok-stt"
    body = json.loads(req.data)
    assert body["mediaType"] == "audio/wav"


def test_grok_stt_requires_gateway_key(drawbox_dir, monkeypatch):
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    drawbox_core.save_settings({"stt_provider": "grok"})
    with pytest.raises(RuntimeError, match="AI_GATEWAY_API_KEY"):
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
    assert captured["req"].get_header("Ai-model-id") == "openai/whisper-1"


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
