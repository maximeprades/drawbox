"""Voice provider dispatch: Gateway TTS default, ElevenLabs and Grok optional."""

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


def test_elevenlabs_tts_request_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(drawbox, "ELEVENLABS_VOICE_ID", "voice123")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-test")

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return FakeAudioResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    out_path = tmp_path / "el.mp3"
    feedback = drawbox.VoiceFeedback(provider="elevenlabs")
    assert feedback._synthesize("hello kids", str(out_path)) is True

    assert out_path.read_bytes() == b"fake-mp3-bytes"
    req = captured["req"]
    assert req.full_url == "https://api.elevenlabs.io/v1/text-to-speech/voice123"
    assert req.get_header("Xi-api-key") == "el-test"
    body = json.loads(req.data)
    assert body["text"] == "... hello kids"
    assert body["voice_settings"]["stability"] == drawbox.TTS_STABILITY


def test_grok_tts_request_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(drawbox, "GROK_VOICE_ID", "ara")
    monkeypatch.setenv("XAI_API_KEY", "xai-test")

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return FakeAudioResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    out_path = tmp_path / "grok.mp3"
    feedback = drawbox.VoiceFeedback(provider="grok")
    assert feedback._synthesize("hello kids", str(out_path)) is True

    assert out_path.read_bytes() == b"fake-mp3-bytes"
    req = captured["req"]
    assert req.full_url == "https://api.x.ai/v1/tts"
    assert req.get_header("Authorization") == "Bearer xai-test"
    body = json.loads(req.data)
    assert body["voice_id"] == "ara"
    assert body["text"] == "... hello kids"


def test_tts_cache_paths_differ_by_provider(monkeypatch, tmp_path):
    monkeypatch.setattr(drawbox, "CACHE_DIR", tmp_path)
    text = "same line"

    gateway_path = drawbox.VoiceFeedback()._tts_path(text)
    eleven_path = drawbox.VoiceFeedback(provider="elevenlabs")._tts_path(text)
    grok_path = drawbox.VoiceFeedback(provider="grok")._tts_path(text)
    assert len({gateway_path, eleven_path, grok_path}) == 3

    # Both pre-existing on-disk cache formats survive the provider switch.
    gateway_historical = hashlib.md5(
        f"{drawbox.TTS_VOICE_ID}:{text}".encode()).hexdigest()[:12]
    assert gateway_path.name == f"{gateway_historical}.mp3"
    eleven_historical = hashlib.md5(
        f"{drawbox.ELEVENLABS_VOICE_ID}:{drawbox.TTS_STABILITY}:"
        f"{drawbox.TTS_STYLE}:{text}".encode()
    ).hexdigest()[:12]
    assert eleven_path.name == f"{eleven_historical}.mp3"


def test_provider_key_table_matches_supported_providers():
    assert set(drawbox.TTS_PROVIDER_KEYS) == set(drawbox_core.VOICE_PROVIDERS)


def test_apply_tts_settings_reads_provider_and_voices(drawbox_dir, monkeypatch):
    # setattr-to-current-value registers the globals `_apply_tts_settings`
    # mutates, so monkeypatch restores them after the test.
    for name in ("VOICE_PROVIDER", "TTS_VOICE_ID", "ELEVENLABS_VOICE_ID",
                 "GROK_VOICE_ID", "TTS_STABILITY", "TTS_STYLE"):
        monkeypatch.setattr(drawbox, name, getattr(drawbox, name))

    settings = drawbox_core.load_settings()
    settings.update({
        "voice_provider": "grok",
        "grok_voice_id": "ara",
        "elevenlabs_voice_id": "voice123",
        "tts_stability": 0.9,
    })
    drawbox_core.save_settings(settings)

    drawbox._apply_tts_settings()

    assert drawbox.VOICE_PROVIDER == "grok"
    assert drawbox.GROK_VOICE_ID == "ara"
    assert drawbox.ELEVENLABS_VOICE_ID == "voice123"
    assert drawbox.TTS_STABILITY == 0.9


def test_apply_tts_settings_falls_back_for_unknown_provider(drawbox_dir, monkeypatch):
    monkeypatch.setattr(drawbox, "VOICE_PROVIDER", "grok")

    settings = drawbox_core.load_settings()
    settings["voice_provider"] = "alexa"
    drawbox_core.save_settings(settings)

    drawbox._apply_tts_settings()

    assert drawbox.VOICE_PROVIDER == "gateway"
