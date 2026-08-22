"""Voice provider dispatch: ElevenLabs default, Grok (xAI) optional."""

import hashlib
import json
import urllib.request
from io import BytesIO

import drawbox
import drawbox_core


def test_synthesize_routes_to_elevenlabs_by_default(monkeypatch, tmp_path):
    feedback = drawbox.VoiceFeedback()
    calls = []
    monkeypatch.setattr(feedback, "_elevenlabs_tts",
                        lambda text, out_path: calls.append(text))

    assert feedback._synthesize("hello", str(tmp_path / "out.mp3")) is True
    assert calls == ["hello"]


def test_grok_tts_request_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(drawbox, "GROK_VOICE_ID", "ara")
    monkeypatch.setattr(drawbox_core, "XAI_API_KEY", "xai-test")

    captured = {}

    class FakeResponse:
        def __init__(self):
            self._data = BytesIO(b"fake-mp3-bytes")

        def read(self, n=-1):
            return self._data.read(n)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return FakeResponse()

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

    eleven_path = drawbox.VoiceFeedback()._tts_path(text)
    historical = hashlib.md5(
        f"{drawbox.TTS_VOICE_ID}:{drawbox.TTS_STABILITY}:"
        f"{drawbox.TTS_STYLE}:{text}".encode()
    ).hexdigest()[:12]
    assert eleven_path.name == f"{historical}.mp3"

    assert drawbox.VoiceFeedback(provider="grok")._tts_path(text) != eleven_path


def test_provider_key_table_matches_supported_providers():
    assert set(drawbox.TTS_PROVIDER_KEYS) == set(drawbox_core.VOICE_PROVIDERS)


def test_apply_tts_settings_falls_back_for_unknown_provider(drawbox_dir, monkeypatch):
    # setattr-to-current-value registers the globals `_apply_tts_settings`
    # mutates, so monkeypatch restores them after the test.
    for name in ("VOICE_PROVIDER", "GROK_VOICE_ID", "TTS_VOICE_ID",
                 "TTS_STABILITY", "TTS_STYLE"):
        monkeypatch.setattr(drawbox, name, getattr(drawbox, name))
    monkeypatch.setattr(drawbox, "VOICE_PROVIDER", "grok")

    settings = drawbox_core.load_settings()
    settings["voice_provider"] = "alexa"
    drawbox_core.save_settings(settings)

    drawbox._apply_tts_settings()

    assert drawbox.VOICE_PROVIDER == "elevenlabs"
