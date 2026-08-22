"""Voice provider dispatch: ElevenLabs default, Grok (xAI) optional."""

import hashlib
import json
import sys
import types
import urllib.request
from io import BytesIO

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
import drawbox_core


def test_synthesize_routes_to_elevenlabs_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(drawbox, "VOICE_PROVIDER", "elevenlabs")
    feedback = drawbox.VoiceFeedback()
    calls = []
    monkeypatch.setattr(feedback, "_elevenlabs_tts",
                        lambda text, out_path: calls.append(text))

    assert feedback._synthesize("hello", str(tmp_path / "out.mp3")) is True
    assert calls == ["hello"]


def test_grok_tts_request_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(drawbox, "VOICE_PROVIDER", "grok")
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
    feedback = drawbox.VoiceFeedback()
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
    feedback = drawbox.VoiceFeedback()
    text = "same line"

    monkeypatch.setattr(drawbox, "VOICE_PROVIDER", "elevenlabs")
    eleven_path = feedback._tts_path(text)
    historical = hashlib.md5(
        f"{drawbox.TTS_VOICE_ID}:{drawbox.TTS_STABILITY}:"
        f"{drawbox.TTS_STYLE}:{text}".encode()
    ).hexdigest()[:12]
    assert eleven_path.name == f"{historical}.mp3"

    monkeypatch.setattr(drawbox, "VOICE_PROVIDER", "grok")
    assert feedback._tts_path(text) != eleven_path


def test_apply_tts_settings_falls_back_for_unknown_provider(drawbox_dir, monkeypatch):
    for name in ("VOICE_PROVIDER", "GROK_VOICE_ID", "TTS_VOICE_ID",
                 "TTS_STABILITY", "TTS_STYLE"):
        monkeypatch.setattr(drawbox, name, getattr(drawbox, name))
    monkeypatch.setattr(drawbox, "VOICE_PROVIDER", "grok")

    settings = drawbox_core.load_settings()
    settings["voice_provider"] = "alexa"
    drawbox_core.save_settings(settings)

    drawbox._apply_tts_settings()

    assert drawbox.VOICE_PROVIDER == "elevenlabs"
