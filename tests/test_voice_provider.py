"""Voice provider dispatch: Gateway TTS default, Grok via the same Gateway key."""

import base64
import hashlib
import json
import urllib.request
from io import BytesIO

import drawbox
import drawbox_core


class FakeAudioResponse:
    def __init__(self, data=b"fake-mp3-bytes"):
        self._data = BytesIO(data)

    def read(self, n=-1):
        return self._data.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_synthesize_routes_to_gateway_by_default(monkeypatch, tmp_path):
    feedback = drawbox.VoiceFeedback()
    calls = []
    monkeypatch.setattr(feedback, "_gateway_tts",
                        lambda text, out_path: calls.append(text))

    assert feedback._synthesize("hello", str(tmp_path / "out.mp3")) is True
    assert calls == ["hello"]


def test_gateway_tts_request_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(drawbox, "TTS_VOICE_ID", "alloy")
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "vck-test")

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return FakeAudioResponse(json.dumps({
            "audio": base64.b64encode(b"MP3BYTES").decode(),
            "warnings": [],
        }).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    out_path = tmp_path / "gw.mp3"
    feedback = drawbox.VoiceFeedback()
    assert feedback._synthesize("hello kids", str(out_path)) is True

    assert out_path.read_bytes() == b"MP3BYTES"
    req = captured["req"]
    assert req.full_url == drawbox_core.AI_GATEWAY_SPEECH_URL
    assert req.get_header("Authorization") == "Bearer vck-test"
    assert req.get_header("Ai-gateway-protocol-version") == "0.0.1"
    assert req.get_header("Ai-speech-model-specification-version") == "4"
    assert req.get_header("Ai-model-id") == "openai/tts-1"
    body = json.loads(req.data)
    assert body["text"].startswith(drawbox_core.TTS_WAKE_PREFIX)
    assert body["text"].endswith("hello kids")
    assert body["voice"] == "alloy"
    assert body["outputFormat"] == "mp3"


def test_grok_tts_uses_gateway_speech_model(monkeypatch, tmp_path):
    monkeypatch.setattr(drawbox, "GROK_VOICE_ID", "ara")
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "vck-test")

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return FakeAudioResponse(json.dumps({
            "audio": base64.b64encode(b"GROKMP3").decode(),
            "warnings": [],
        }).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    out_path = tmp_path / "grok.mp3"
    feedback = drawbox.VoiceFeedback(provider="grok")
    assert feedback._synthesize("hello kids", str(out_path)) is True

    assert out_path.read_bytes() == b"GROKMP3"
    req = captured["req"]
    assert req.full_url == drawbox_core.AI_GATEWAY_SPEECH_URL
    assert req.get_header("Authorization") == "Bearer vck-test"
    assert req.get_header("Ai-model-id") == "spacexai/grok-tts"
    body = json.loads(req.data)
    assert body["voice"] == "ara"
    assert body["text"].endswith("hello kids")


def test_tts_cache_paths_differ_by_provider(monkeypatch, tmp_path):
    monkeypatch.setattr(drawbox, "CACHE_DIR", tmp_path)
    text = "same line"

    gateway_path = drawbox.VoiceFeedback()._tts_path(text)
    grok_path = drawbox.VoiceFeedback(provider="grok")._tts_path(text)
    assert gateway_path != grok_path

    gateway_historical = hashlib.md5(
        f"{drawbox.TTS_VOICE_ID}:{text}".encode()).hexdigest()[:12]
    assert gateway_path.name == f"{gateway_historical}.mp3"


def test_provider_key_table_matches_supported_providers():
    assert set(drawbox.TTS_PROVIDER_KEYS) == set(drawbox_core.VOICE_PROVIDERS)
    assert set(drawbox.TTS_PROVIDER_KEYS.values()) == {"AI_GATEWAY_API_KEY"}


def test_apply_tts_settings_reads_provider_and_voices(drawbox_dir, monkeypatch):
    for name in ("VOICE_PROVIDER", "TTS_VOICE_ID", "GROK_VOICE_ID",
                 "TTS_STABILITY", "TTS_STYLE"):
        monkeypatch.setattr(drawbox, name, getattr(drawbox, name))

    settings = drawbox_core.load_settings()
    settings.update({
        "voice_provider": "grok",
        "grok_voice_id": "ara",
        "tts_stability": 0.9,
    })
    drawbox_core.save_settings(settings)

    drawbox._apply_tts_settings()

    assert drawbox.VOICE_PROVIDER == "grok"
    assert drawbox.GROK_VOICE_ID == "ara"
    assert drawbox.TTS_STABILITY == 0.9


def test_apply_tts_settings_falls_back_for_unknown_provider(drawbox_dir, monkeypatch):
    monkeypatch.setattr(drawbox, "VOICE_PROVIDER", "grok")

    settings = drawbox_core.load_settings()
    settings["voice_provider"] = "alexa"
    drawbox_core.save_settings(settings)

    drawbox._apply_tts_settings()

    assert drawbox.VOICE_PROVIDER == "gateway"


def test_apply_tts_settings_clamps_retired_elevenlabs(drawbox_dir, monkeypatch):
    monkeypatch.setattr(drawbox, "VOICE_PROVIDER", "gateway")
    settings = drawbox_core.load_settings()
    settings["voice_provider"] = "elevenlabs"
    drawbox_core.save_settings(settings)

    drawbox._apply_tts_settings()

    assert drawbox.VOICE_PROVIDER == "gateway"
