"""Conversation-mode infrastructure: session config, draw tool, interceptor,
and the /api/realtime + /api/agent endpoints."""

import json
import time
import types
import urllib.request
from pathlib import Path

import drawbox_core
import drawbox_web


# ── realtime_session_config ───────────────────────

def test_session_config_carries_shared_personality(drawbox_dir):
    cfg = drawbox_core.realtime_session_config()
    assert cfg["instructions"] == drawbox_core.DEFAULT_AGENT_INSTRUCTIONS
    assert cfg["voice"] == "eve"
    assert cfg["turn_detection"]["type"] == "server_vad"
    assert cfg["turn_detection"]["silence_duration_ms"] == \
        drawbox_core.AGENT_SILENCE_MS
    assert cfg["tools"] == [drawbox_core.AGENT_DRAW_TOOL]
    assert cfg["audio"]["input"]["transcription"]["model"] == "grok-transcribe"


def test_session_config_honors_scripts_and_voice(drawbox_dir):
    drawbox_core.save_scripts({"agent_instructions": "Be a pirate."})
    drawbox_core.save_settings({"grok_voice_id": "ara"})
    cfg = drawbox_core.realtime_session_config()
    assert cfg["instructions"] == "Be a pirate."
    assert cfg["voice"] == "ara"


def test_scripts_round_trip_agent_instructions(drawbox_dir):
    drawbox_core.save_scripts({"agent_instructions": "Be nice."})
    assert drawbox_core.load_scripts()["agent_instructions"] == "Be nice."
    # Empty override falls back to the default prompt.
    drawbox_core.save_scripts({"agent_instructions": ""})
    assert drawbox_core.load_scripts()["agent_instructions"] == \
        drawbox_core.DEFAULT_AGENT_INSTRUCTIONS


# ── execute_draw_tool ─────────────────────────────

def _wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_draw_tool_generates_and_prints_in_background(drawbox_dir, monkeypatch):
    calls = {}

    def fake_generate(d, model=None):
        calls["gen"] = (d, model)
        return "page.png"

    monkeypatch.setattr(drawbox_core, "generate_image", fake_generate)
    monkeypatch.setattr(drawbox_core, "print_image",
                        lambda p, printer_type=None: calls.setdefault("printed", p))
    drawbox_core.save_settings({"image_model": "gpt-image"})

    out = drawbox_core.execute_draw_tool("  a happy dragon  ")
    assert out["ok"] is True
    assert _wait_for(lambda: "printed" in calls)
    assert calls["gen"] == ("a happy dragon", "gpt-image")
    assert calls["printed"] == "page.png"


def test_draw_tool_blocks_unsafe_descriptions(drawbox_dir, monkeypatch):
    monkeypatch.setattr(
        drawbox_core, "generate_image",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("gated!")))
    drawbox_core.ensure_safety_mode_default()
    out = drawbox_core.execute_draw_tool("a gun")
    assert out["ok"] is False
    assert out["message"] == drawbox_core.script_line("blocked")


def test_draw_tool_reports_busy_while_locked(drawbox_dir):
    assert drawbox_core._DRAW_TOOL_LOCK.acquire(blocking=False)
    try:
        out = drawbox_core.execute_draw_tool("a cat")
        assert out["ok"] is False
        assert out["message"] == drawbox_core.script_line("busy")
    finally:
        drawbox_core._DRAW_TOOL_LOCK.release()


def test_draw_tool_needs_a_real_description(drawbox_dir):
    assert drawbox_core.execute_draw_tool("")["ok"] is False
    assert drawbox_core.execute_draw_tool(None)["ok"] is False


# ── intercept_transcript ──────────────────────────

def test_intercept_admin_poop_commands(drawbox_dir):
    hit = drawbox_core.intercept_transcript("Admin mode, enable poop mode!")
    assert hit["action"] == "poop_on"
    assert hit["voice_key"] == "poop_mode_enabled"
    assert drawbox_core.poop_mode_enabled() is True
    hit = drawbox_core.intercept_transcript("admin mode disable poop mode")
    assert hit["action"] == "poop_off"
    assert drawbox_core.poop_mode_enabled() is False


def test_intercept_pairing_prints_and_speaks_the_code(drawbox_dir, monkeypatch):
    printed = {}
    monkeypatch.setattr(drawbox_core, "print_image",
                        lambda p, printer_type=None: printed.setdefault("path", p))
    hit = drawbox_core.intercept_transcript("authorize")
    assert hit["action"] == "pairing"
    assert hit["voice_key"] is None
    assert "Pairing mode!" in hit["say"]
    assert "printed the code" in hit["say"]
    assert printed  # the code card went to the printer
    assert drawbox_core.PAIRING_FILE.exists()


def test_intercept_blocklist_and_clean_text(drawbox_dir):
    drawbox_core.ensure_safety_mode_default()
    hit = drawbox_core.intercept_transcript("draw a gun")
    assert hit["action"] == "blocked"
    assert hit["voice_key"] == "blocked"
    assert drawbox_core.intercept_transcript("a friendly dragon") is None


def test_content_block_checks_poop_before_safety(drawbox_dir):
    """The shared gate owns the ordering invariant: poop first, blocklist
    second — matching the historical one-shot flow."""
    drawbox_core.ensure_safety_mode_default()
    drawbox_core.set_poop_mode_enabled(False)
    hit = drawbox_core.content_block("a bloody poop monster")
    assert hit["voice_key"] == "poop_blocked"
    drawbox_core.set_poop_mode_enabled(True)
    hit = drawbox_core.content_block("a bloody poop monster")
    assert hit["voice_key"] == "blocked"
    assert drawbox_core.content_block("a rainbow") is None


# ── endpoints ─────────────────────────────────────

class _FakeSecretResponse:
    def read(self, n=-1):
        return json.dumps({"value": "eph-123", "expires_at": 4102444800}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_realtime_token_requires_conversation_mode(client):
    r = client.post("/api/realtime/token")
    assert r.status_code == 403
    assert r.get_json()["code"] == "conversation_off"


def test_realtime_token_mints_ephemeral_secret(client, monkeypatch):
    drawbox_core.save_settings({"conversation_mode": True})
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return _FakeSecretResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    body = client.post("/api/realtime/token").get_json()
    assert body["ok"] is True
    assert body["token"] == "eph-123"
    assert body["url"].startswith("wss://api.x.ai/v1/realtime")
    assert body["session"]["tools"][0]["name"] == "draw_coloring_page"
    assert body["max_session_s"] == drawbox_core.AGENT_SESSION_MAX_S
    req = captured["req"]
    assert req.full_url == drawbox_core.XAI_CLIENT_SECRETS_URL
    assert req.get_header("Authorization") == "Bearer xai-test"


def test_realtime_token_without_xai_key_is_503(client, monkeypatch):
    drawbox_core.save_settings({"conversation_mode": True})
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    r = client.post("/api/realtime/token")
    assert r.status_code == 503
    assert r.get_json()["code"] == "no_key"


def test_agent_endpoints_403_when_conversation_off(client):
    """The draw path skips the please gate, so it must be unreachable
    outside conversation mode — same 403 as the token endpoint."""
    r = client.post("/api/agent/draw", json={"description": "a cat"})
    assert r.status_code == 403
    assert r.get_json()["code"] == "conversation_off"
    r = client.post("/api/agent/intercept", json={"transcript": "a cat"})
    assert r.status_code == 403


def test_agent_draw_endpoint_gates_and_runs(client, monkeypatch):
    calls = {}
    monkeypatch.setattr(drawbox_web.drawbox_core, "generate_image",
                        lambda d, model=None: calls.setdefault("gen", d) or "p.png")
    monkeypatch.setattr(drawbox_web.drawbox_core, "print_image",
                        lambda p, printer_type=None: calls.setdefault("printed", p))
    drawbox_core.save_settings({"conversation_mode": True})
    drawbox_core.ensure_safety_mode_default()

    blocked = client.post("/api/agent/draw",
                          json={"description": "a gun"}).get_json()
    assert blocked["ok"] is False

    ok = client.post("/api/agent/draw",
                     json={"description": "a rainbow"}).get_json()
    assert ok["ok"] is True
    assert _wait_for(lambda: "printed" in calls)
    assert calls["gen"] == "a rainbow"


def test_agent_intercept_endpoint_returns_clip(client, monkeypatch):
    drawbox_core.save_settings({"conversation_mode": True})
    monkeypatch.setattr(drawbox_core, "synthesize_speech",
                        lambda *a, **k: b"mp3bytes")
    real_run = drawbox_web.subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "ffmpeg":
            Path(cmd[-1]).write_bytes(b"RIFF..wavdata")
            return types.SimpleNamespace(returncode=0)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(drawbox_web.subprocess, "run", fake_run)

    clean = client.post("/api/agent/intercept",
                        json={"transcript": "a friendly cat"}).get_json()
    assert clean["action"] is None

    hit = client.post("/api/agent/intercept",
                      json={"transcript": "admin mode enable poop mode"}).get_json()
    assert hit["action"] == "poop_on"
    assert hit["voice_key"] == "poop_mode_enabled"
    assert len(hit["ack_key"]) == 12
    assert client.get(f"/api/voice/clip?k={hit['ack_key']}").status_code == 200


def test_voice_generate_intercepts_spoken_pairing(client, monkeypatch):
    """Saying "authorize" at the ESP32 box now opens pairing (old drift)."""
    monkeypatch.setattr(drawbox_web, "transcribe_audio",
                        lambda data, media_type="audio/wav": "authorize")
    monkeypatch.setattr(drawbox_web.drawbox_core, "print_image",
                        lambda p, printer_type=None: None)
    monkeypatch.setattr(drawbox_core, "synthesize_speech",
                        lambda *a, **k: b"mp3bytes")
    real_run = drawbox_web.subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "ffmpeg":
            Path(cmd[-1]).write_bytes(b"RIFF..wavdata")
            return types.SimpleNamespace(returncode=0)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(drawbox_web.subprocess, "run", fake_run)

    body = client.post("/api/voice/generate", data=b"R" * 2048,
                       headers={"Content-Type": "audio/wav"}).get_json()
    assert body["code"] == "intercepted"
    assert body["action"] == "pairing"
    assert "Pairing mode!" in body["error"]
    assert len(body["ack_key"]) == 12
    assert drawbox_core.PAIRING_FILE.exists()
