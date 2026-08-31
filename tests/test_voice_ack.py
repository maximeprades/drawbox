"""Two-phase (ack) voice flow for firmware >= 1.6.0 and the ack helper."""

import types
from pathlib import Path
from types import SimpleNamespace

import pytest

import drawbox_core
import drawbox_web


def _patch_ack_stack(monkeypatch, ack_text="Ooh, a happy dragon coming up!"):
    """Stub the ack LLM, TTS, and ffmpeg so no network or binaries run."""
    monkeypatch.setattr(drawbox_core, "generate_ack_text", lambda t: ack_text)
    monkeypatch.setattr(drawbox_core, "synthesize_speech",
                        lambda *a, **k: b"mp3bytes")
    real_run = drawbox_web.subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "ffmpeg":
            Path(cmd[-1]).write_bytes(b"RIFF..wavdata")
            return types.SimpleNamespace(returncode=0)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(drawbox_web.subprocess, "run", fake_run)


def _patch_generation(monkeypatch):
    monkeypatch.setattr(drawbox_web, "transcribe_audio",
                        lambda data, media_type="audio/wav": "a happy dragon")
    monkeypatch.setattr(drawbox_web, "generate_image",
                        lambda desc, model=None: "page.png")
    monkeypatch.setattr(drawbox_web, "print_image",
                        lambda path, printer_type=None: None)


def _post_audio(client, ack=True):
    headers = {"Content-Type": "audio/wav"}
    if ack:
        headers["X-DrawBox-Ack"] = "1"
    return client.post("/api/voice/generate", data=b"R" * 2048,
                       headers=headers)


def test_ack_flow_returns_ack_key_then_result(client, monkeypatch):
    _patch_ack_stack(monkeypatch)
    _patch_generation(monkeypatch)

    body = _post_audio(client).get_json()
    assert body["ok"] is True
    assert body["transcript"] == "a happy dragon"
    assert len(body["ack_key"]) == 12
    assert body["job"]

    clip = client.get(f"/api/voice/clip?k={body['ack_key']}")
    assert clip.status_code == 200
    assert clip.data == b"RIFF..wavdata"

    result = client.get(
        f"/api/voice/result?id={body['job']}&timeout=10").get_json()
    assert result["ok"] is True
    assert result["voice_key"] == "printing"
    assert result["transcript"] == "a happy dragon"


def test_ack_flow_survives_ack_synthesis_failure(client, monkeypatch):
    _patch_generation(monkeypatch)

    def boom(_transcript):
        raise RuntimeError("no key")

    monkeypatch.setattr(drawbox_core, "generate_ack_text", boom)

    body = _post_audio(client).get_json()
    assert body["ok"] is True
    assert body["ack_key"] is None  # box falls back to its canned line
    result = client.get(
        f"/api/voice/result?id={body['job']}&timeout=10").get_json()
    assert result["ok"] is True


def test_ack_flow_skips_ack_when_setting_off(client, monkeypatch):
    _patch_generation(monkeypatch)
    called = []
    monkeypatch.setattr(drawbox_core, "generate_ack_text",
                        lambda t: called.append(t) or "nope")
    drawbox_core.save_settings({"natural_ack": False})

    body = _post_audio(client).get_json()
    assert body["ok"] is True
    assert body["ack_key"] is None
    assert called == []
    # Drain the job thread before teardown unpatches the generation stack.
    assert client.get(
        f"/api/voice/result?id={body['job']}&timeout=10").get_json()["ok"]


def test_ack_flow_rejections_return_final_result_directly(client, monkeypatch):
    _patch_generation(monkeypatch)
    monkeypatch.setattr(drawbox_web, "transcribe_audio",
                        lambda data, media_type="audio/wav": "draw a gun")
    drawbox_core.ensure_safety_mode_default()

    body = _post_audio(client).get_json()
    assert body["ok"] is False
    assert body["code"] == "rejected"
    assert body["voice_key"] == "blocked"
    assert "job" not in body


def test_legacy_flow_unchanged_without_header(client, monkeypatch):
    _patch_generation(monkeypatch)

    body = _post_audio(client, ack=False).get_json()
    assert body["ok"] is True
    assert body["voice_key"] == "printing"
    assert "job" not in body
    assert "ack_key" not in body


def test_ack_flow_refuses_while_another_job_runs(client, monkeypatch):
    """The busy check reads the on-disk job slot, so the OTHER gunicorn
    worker (or a second box) cannot clobber an in-flight job."""
    import time as _t

    import drawbox_web as web
    _patch_generation(monkeypatch)
    web._write_secure_json(web._voice_job_path(),
                           {"id": "aaaa", "status": "running", "ts": _t.time()})

    body = _post_audio(client).get_json()
    assert body["ok"] is False
    assert body["code"] == "busy"

    # A stale running job (crashed worker) no longer blocks.
    web._write_secure_json(web._voice_job_path(),
                           {"id": "aaaa", "status": "running",
                            "ts": _t.time() - 9999})
    body = _post_audio(client).get_json()
    assert body["ok"] is True
    assert client.get(
        f"/api/voice/result?id={body['job']}&timeout=10").get_json()["ok"]


def test_job_slot_is_claimed_before_ack_synthesis(client, monkeypatch):
    """The running slot must exist while the (seconds-long) ack synthesis
    runs, or a concurrent request slips past the busy check."""
    _patch_generation(monkeypatch)
    seen = {}

    def ack_probe(_transcript):
        job = drawbox_web._read_voice_job()
        seen["status_during_ack"] = job and job.get("status")
        raise RuntimeError("skip synthesis")

    monkeypatch.setattr(drawbox_core, "generate_ack_text", ack_probe)

    body = _post_audio(client).get_json()
    assert seen["status_during_ack"] == "running"
    assert client.get(
        f"/api/voice/result?id={body['job']}&timeout=10").get_json()["ok"]


def test_voice_result_without_job_is_404(client):
    r = client.get("/api/voice/result?timeout=0")
    assert r.status_code == 404
    assert r.get_json()["code"] == "no_job"


def test_voice_clip_rejects_bad_keys(client):
    assert client.get("/api/voice/clip?k=../../etc/passwd").status_code == 404
    assert client.get("/api/voice/clip?k=abcdefabcdef").status_code == 404


# ── generate_ack_text ─────────────────────────────

def _ack_client(monkeypatch, content):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "vck-test")
    seen = {}

    def fake_create(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=content))])

    class FakeClient:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=fake_create))

    monkeypatch.setattr(drawbox_core, "OpenAI", FakeClient)
    drawbox_core.apply_api_keys()
    return seen


def test_generate_ack_text_returns_one_clean_line(drawbox_dir, monkeypatch):
    seen = _ack_client(monkeypatch, '"Ooh, a purple dinosaur!"\nSecond line')
    assert drawbox_core.generate_ack_text("a purple dinosaur") == \
        "Ooh, a purple dinosaur!"
    assert seen["model"] == drawbox_core.ACK_MODEL
    assert seen["messages"][1]["content"] == "a purple dinosaur"


def test_generate_ack_text_raises_on_empty_reply(drawbox_dir, monkeypatch):
    _ack_client(monkeypatch, "")
    with pytest.raises(RuntimeError):
        drawbox_core.generate_ack_text("a cat")


def test_generate_ack_text_requires_key(drawbox_dir, monkeypatch):
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    drawbox_core.apply_api_keys()
    with pytest.raises(RuntimeError, match="AI_GATEWAY_API_KEY"):
        drawbox_core.generate_ack_text("a cat")
