"""Tests for the ESP32 voice box endpoint (/api/voice/generate) and the
shared gateway transcription helper in drawbox_core."""

import base64
import json

import pytest

import drawbox_core
import drawbox_web


FAKE_WAV = b"RIFF" + b"\x00" * 4096  # big enough to pass the min-size gate


def _post_audio(client, data=FAKE_WAV, content_type="audio/wav"):
    return client.post("/api/voice/generate", data=data,
                       content_type=content_type)


def _mock_pipeline(monkeypatch, tmp_path, transcript="a friendly dinosaur"):
    """Stub transcription, generation, and printing; returns the print calls."""
    img = tmp_path / "out.png"
    img.write_bytes(b"fake-png")
    printed = []
    monkeypatch.setattr(drawbox_web, "transcribe_audio",
                        lambda data, media_type="audio/wav": transcript)
    monkeypatch.setattr(drawbox_web, "generate_image", lambda *a, **k: str(img))
    monkeypatch.setattr(drawbox_web, "print_image",
                        lambda path, printer_type=None: printed.append(path))
    return printed


# ── endpoint auth and input gates ─────────────────

def test_voice_requires_pairing(drawbox_dir):
    drawbox_web.app.testing = True
    unpaired = drawbox_web.app.test_client()
    r = unpaired.post("/api/voice/generate", data=FAKE_WAV,
                      content_type="audio/wav")
    assert r.status_code == 401


def test_voice_rejects_tiny_body(client):
    r = _post_audio(client, data=b"RIFF")
    assert r.status_code == 400
    body = r.get_json()
    assert body["ok"] is False
    assert body["code"] == "too_short"
    assert body["voice_key"] == "too_short"


def test_voice_rejects_oversized_body(client, monkeypatch):
    monkeypatch.setattr(drawbox_web, "VOICE_AUDIO_MAX_BYTES", 16)
    r = _post_audio(client)
    assert r.status_code == 413
    assert r.get_json()["code"] == "too_large"


def test_voice_body_backstop_catches_missing_content_length(client, monkeypatch):
    # Bodies without a usable Content-Length (e.g. chunked) bypass the
    # route's header check; the app-wide MAX_CONTENT_LENGTH must catch them
    # and the 413 handler must still answer JSON.
    monkeypatch.setitem(drawbox_web.app.config, "MAX_CONTENT_LENGTH", 16)
    r = _post_audio(client)
    assert r.status_code == 413
    assert r.get_json()["ok"] is False


# ── the happy path ────────────────────────────────

def test_voice_happy_path_generates_prints_and_logs(client, monkeypatch, tmp_path):
    printed = _mock_pipeline(monkeypatch, tmp_path)

    body = _post_audio(client).get_json()

    assert body["ok"] is True
    assert body["transcript"] == "a friendly dinosaur"
    assert body["message"] == drawbox_core.DEFAULT_VOICE_LINES["printing"]["text"]
    assert body["voice_key"] == "printing"
    assert "image" not in body  # the box has no use for megabytes of base64
    assert len(printed) == 1
    events = [json.loads(line) for line in
              drawbox_core.PRINT_LOG_FILE.read_text().splitlines()]
    assert events[-1]["source"] == "esp32"
    assert events[-1]["prompt"] == "a friendly dinosaur"


def test_voice_media_type_passes_through_and_defaults(client, monkeypatch, tmp_path):
    seen = []

    def fake_transcribe(data, media_type="audio/wav"):
        seen.append(media_type)
        return ""

    monkeypatch.setattr(drawbox_web, "transcribe_audio", fake_transcribe)
    _post_audio(client, content_type="audio/mpeg")
    _post_audio(client, content_type="application/octet-stream")
    assert seen == ["audio/mpeg", "audio/wav"]


# ── transcript gates (same rules as the button daemon) ──

def test_voice_short_transcript_is_too_short(client, monkeypatch):
    monkeypatch.setattr(drawbox_web, "transcribe_audio", lambda *a, **k: " x ")
    body = _post_audio(client).get_json()
    assert body["ok"] is False
    assert body["code"] == "too_short"
    assert body["error"] == drawbox_core.DEFAULT_VOICE_LINES["too_short"]["text"]
    assert body["voice_key"] == "too_short"


def test_voice_blocked_transcript_uses_script_line(client, monkeypatch):
    drawbox_core.SAFETY_MODE_FILE.touch()
    monkeypatch.setattr(drawbox_web, "transcribe_audio",
                        lambda *a, **k: "a gun and a knife")
    body = _post_audio(client).get_json()
    assert body["ok"] is False
    assert body["code"] == "rejected"
    assert body["error"] == drawbox_core.DEFAULT_VOICE_LINES["blocked"]["text"]
    assert body["transcript"] == "a gun and a knife"
    assert body["voice_key"] == "blocked"


def test_voice_poop_blocked_when_poop_mode_off(client, monkeypatch):
    client.post("/api/poop-mode", json={"enabled": False})
    monkeypatch.setattr(drawbox_web, "transcribe_audio",
                        lambda *a, **k: "a poopy dinosaur")
    body = _post_audio(client).get_json()
    assert body["ok"] is False
    assert body["error"] == drawbox_core.DEFAULT_VOICE_LINES["poop_blocked"]["text"]
    assert body["voice_key"] == "poop_blocked"


def test_voice_requires_please_when_please_mode_on(client, monkeypatch, tmp_path):
    drawbox_core.PLEASE_MODE_FILE.touch()
    monkeypatch.setattr(drawbox_web, "transcribe_audio",
                        lambda *a, **k: "a happy puppy")
    body = _post_audio(client).get_json()
    assert body["ok"] is False
    assert body["error"] == drawbox_core.DEFAULT_VOICE_LINES["say_please"]["text"]
    assert body["voice_key"] == "say_please"

    _mock_pipeline(monkeypatch, tmp_path, transcript="a happy puppy please")
    assert _post_audio(client).get_json()["ok"] is True


# ── failure paths ─────────────────────────────────

def test_voice_transcription_failure_is_graceful(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("gateway down /home/secret")

    monkeypatch.setattr(drawbox_web, "transcribe_audio", boom)
    r = _post_audio(client)
    assert r.status_code == 502
    body = r.get_json()
    assert body["ok"] is False
    assert body["code"] == "transcribe_failed"
    assert "/home/secret" not in body["error"]
    assert body["voice_key"] == "error"


def test_voice_busy_uses_busy_script_line(client, monkeypatch):
    monkeypatch.setattr(drawbox_web, "transcribe_audio",
                        lambda *a, **k: "a friendly dinosaur")
    assert drawbox_web._gen_lock.acquire(blocking=False)
    try:
        body = _post_audio(client).get_json()
    finally:
        drawbox_web._gen_lock.release()
    assert body["ok"] is False
    assert body["code"] == "busy"
    assert body["error"] == drawbox_core.DEFAULT_VOICE_LINES["busy"]["text"]
    assert body["voice_key"] == "busy"


def test_voice_generation_failure_uses_error_script_line(client, monkeypatch):
    monkeypatch.setattr(drawbox_web, "transcribe_audio",
                        lambda *a, **k: "a friendly dinosaur")
    monkeypatch.setattr(drawbox_web, "generate_image",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("api")))
    body = _post_audio(client).get_json()
    assert body["ok"] is False
    assert body["code"] == "generate_failed"
    assert body["error"] == drawbox_core.DEFAULT_VOICE_LINES["error"]["text"]
    assert body["voice_key"] == "error"


# ── drawbox_core.transcribe_audio ─────────────────

def test_transcribe_audio_sends_v4_payload(drawbox_dir, monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "vck-test")
    calls = []

    def fake_post(url, payload, model_headers):
        calls.append((url, payload, model_headers))
        return {"text": "a dinosaur"}

    monkeypatch.setattr(drawbox_core, "gateway_v4_post", fake_post)
    text = drawbox_core.transcribe_audio(b"wav-bytes", media_type="audio/wav")

    assert text == "a dinosaur"
    url, payload, headers = calls[0]
    assert url == drawbox_core.AI_GATEWAY_TRANSCRIPTION_URL
    assert base64.b64decode(payload["audio"]) == b"wav-bytes"
    assert payload["mediaType"] == "audio/wav"
    assert headers["ai-model-id"] == drawbox_core.GATEWAY_STT_MODEL
    assert headers["ai-transcription-model-specification-version"] == "4"


def test_transcribe_audio_requires_key(drawbox_dir, monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "")
    with pytest.raises(RuntimeError, match="AI_GATEWAY_API_KEY"):
        drawbox_core.transcribe_audio(b"wav-bytes")


def test_transcribe_audio_tolerates_missing_text(drawbox_dir, monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "vck-test")
    monkeypatch.setattr(drawbox_core, "gateway_v4_post",
                        lambda *a, **k: {})
    assert drawbox_core.transcribe_audio(b"wav-bytes") == ""
