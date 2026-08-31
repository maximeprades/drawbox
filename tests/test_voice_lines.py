"""Voice-line manifest and on-demand TTS for the ESP32 box."""

import types
from pathlib import Path

import drawbox_core
import drawbox_web


def test_tts_cache_key_matches_daemon_historical_hashes():
    """Pin the 12-hex prefixes the Pi daemon already uses on disk.

    Plaintext formulas (must stay byte-identical):
      gateway    md5("{voice_id}:{text}")
               = md5("alloy:hello kids")
      elevenlabs md5("{voice_id}:{stability}:{style}:{text}")
               = md5("voice123:0.5:0.0:hello kids")
      grok       md5("grok:{voice_id}:{text}")
               = md5("grok:eve:hello kids")
    """
    text = "hello kids"
    assert drawbox_core.tts_cache_key(text, "gateway", "alloy") == "6cad69b2bf31"
    assert drawbox_core.tts_cache_key(
        text, "elevenlabs", "voice123", 0.5, 0.0) == "155610e07050"
    assert drawbox_core.tts_cache_key(text, "grok", "eve") == "e807ca23e09e"


def test_voice_lines_manifest_default_scripts(client):
    body = client.get("/api/voice/lines").get_json()
    assert body["thinking"] == 5
    assert body["jokes"] == 10
    assert body["listening"] == 1
    assert len(body["cache_hash"]) == 12
    assert all(c in "0123456789abcdef" for c in body["cache_hash"])


def test_voice_lines_requires_pairing(drawbox_dir):
    drawbox_web.app.testing = True
    unpaired = drawbox_web.app.test_client()
    r = unpaired.get("/api/voice/lines")
    assert r.status_code == 401


def _patch_tts(monkeypatch, synth=None):
    """Stub synthesis and ffmpeg; ffmpeg writes a tiny WAV to its output path."""
    calls = []

    def fake_synth(text, provider, voice_id, stability=0.5, style=0.0,
                   similarity_boost=0.75):
        calls.append(text)
        return b"mp3bytes"

    monkeypatch.setattr(drawbox_core, "synthesize_speech", synth or fake_synth)
    real_run = drawbox_web.subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "ffmpeg":
            Path(cmd[-1]).write_bytes(b"RIFF..wavdata")
            return types.SimpleNamespace(returncode=0)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(drawbox_web.subprocess, "run", fake_run)
    return calls


def test_voice_line_happy_path_and_cache(client, monkeypatch):
    calls = _patch_tts(monkeypatch)

    r = client.get("/api/voice/line?key=listening")
    assert r.status_code == 200
    assert r.mimetype == "audio/wav"
    assert r.data == b"RIFF..wavdata"
    assert len(calls) == 1

    r2 = client.get("/api/voice/line?key=listening")
    assert r2.status_code == 200
    assert r2.data == b"RIFF..wavdata"
    assert len(calls) == 1


def test_voice_line_joke(client, monkeypatch):
    _patch_tts(monkeypatch)
    r = client.get("/api/voice/line?key=joke&i=0")
    assert r.status_code == 200
    assert r.mimetype == "audio/wav"
    assert r.data == b"RIFF..wavdata"


def test_voice_line_unknown_key(client):
    r = client.get("/api/voice/line?key=not_a_line")
    assert r.status_code == 404
    body = r.get_json()
    assert body["ok"] is False
    assert body.get("error")


def test_voice_line_out_of_range_variant(client):
    r = client.get("/api/voice/line?key=listening&i=9")
    assert r.status_code == 404
    assert r.get_json()["ok"] is False


def test_voice_line_synthesis_failure_is_503(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no key")

    monkeypatch.setattr(drawbox_core, "synthesize_speech", boom)
    r = client.get("/api/voice/line?key=listening")
    assert r.status_code == 503
    body = r.get_json()
    assert body["ok"] is False
    assert body.get("error")
