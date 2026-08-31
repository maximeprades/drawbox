"""AgentSession policy: moderation both directions, admin interception,
tool-call execution — no socket, no hardware."""

import asyncio
import base64
import json

import numpy as np

import drawbox_core
import drawbox_realtime


class Harness:
    def __init__(self):
        self.sent = []
        self.spoken = []
        self.audio = b""
        self.cleared = 0
        self.session = drawbox_realtime.AgentSession(
            self._send, self._speak, self._enqueue, self._clear)

    async def _send(self, payload):
        self.sent.append(payload)

    async def _speak(self, text):
        self.spoken.append(text)

    def _enqueue(self, data):
        self.audio += data

    def _clear(self):
        self.cleared += 1

    def feed(self, *events):
        async def run():
            for e in events:
                await self.session.handle_event(e)
        asyncio.run(run())


def _b64(data):
    return base64.b64encode(data).decode()


def test_audio_deltas_reach_the_speaker(drawbox_dir):
    h = Harness()
    h.feed({"type": "response.audio.delta", "response_id": "r1",
            "delta": _b64(b"\x01\x02\x03\x04")})
    assert h.audio == b"\x01\x02\x03\x04"


def test_tool_call_executes_gated_pipeline(drawbox_dir, monkeypatch):
    seen = {}

    def fake_tool(description):
        seen["desc"] = description
        return {"ok": True, "message": "printing!"}

    monkeypatch.setattr(drawbox_core, "execute_draw_tool", fake_tool)
    h = Harness()
    h.feed({"type": "response.function_call_arguments.done", "call_id": "c1",
            "name": "draw_coloring_page",
            "arguments": json.dumps({"description": "a dragon"})})

    assert seen["desc"] == "a dragon"
    assert h.sent[0]["type"] == "conversation.item.create"
    assert h.sent[0]["item"]["call_id"] == "c1"
    assert h.sent[0]["item"]["output"] == "printing!"
    assert h.sent[1] == {"type": "response.create"}


def test_unknown_tool_reports_back_without_drawing(drawbox_dir, monkeypatch):
    monkeypatch.setattr(
        drawbox_core, "execute_draw_tool",
        lambda d: (_ for _ in ()).throw(AssertionError("wrong tool ran")))
    h = Harness()
    h.feed({"type": "response.function_call_arguments.done", "call_id": "c9",
            "name": "rm_dash_rf", "arguments": "{}"})
    assert "Unknown tool" in h.sent[0]["item"]["output"]


def test_blocked_agent_output_is_killed_mid_response(drawbox_dir):
    drawbox_core.ensure_safety_mode_default()
    h = Harness()
    h.feed(
        {"type": "response.audio.delta", "response_id": "r1",
         "delta": _b64(b"11")},
        {"type": "response.audio_transcript.delta", "response_id": "r1",
         "delta": "here is a gun for"},
        {"type": "response.audio.delta", "response_id": "r1",
         "delta": _b64(b"22")},
    )
    assert h.audio == b"11"       # nothing enqueued after the kill
    assert h.cleared == 1         # unplayed audio dropped
    assert {"type": "response.cancel"} in h.sent
    assert "blocked" in h.spoken
    assert h.session.block_strikes == 1


def test_killed_input_also_silences_the_inflight_response(drawbox_dir):
    """A blocklist hit on the KID's words must stop the agent's current
    reply too — later audio deltas for that response stay out of the
    speaker queue (Bugbot, PR #39)."""
    drawbox_core.ensure_safety_mode_default()
    h = Harness()
    h.feed(
        {"type": "response.created", "response": {"id": "r7"}},
        {"type": "response.audio.delta", "response_id": "r7",
         "delta": _b64(b"11")},
        {"type": "conversation.item.input_audio_transcription.completed",
         "transcript": "draw a gun"},
        {"type": "response.audio.delta", "response_id": "r7",
         "delta": _b64(b"22")},
    )
    assert h.audio == b"11"
    assert h.cleared == 1
    assert "blocked" in h.spoken


def test_error_before_configure_fails_the_session(drawbox_dir):
    h = Harness()
    h.feed({"type": "error", "error": {"message": "bad session config"}})
    assert h.session.done is True
    assert h.session.failed is True


def test_error_after_configure_is_recoverable(drawbox_dir):
    h = Harness()
    h.feed(
        {"type": "session.updated"},
        {"type": "error", "error": {"message": "transient"}},
    )
    assert h.session.configured is True
    assert h.session.done is False
    assert h.session.failed is False


def test_two_blocklist_strikes_end_the_session(drawbox_dir):
    drawbox_core.ensure_safety_mode_default()
    h = Harness()
    h.feed({"type": "conversation.item.input_audio_transcription.completed",
            "transcript": "draw a gun"})
    assert h.session.done is False
    h.feed({"type": "conversation.item.input_audio_transcription.completed",
            "transcript": "a bloody sword"})
    assert h.session.done is True
    assert h.spoken.count("blocked") == 2


def test_admin_command_intercepted_deterministically(drawbox_dir, monkeypatch):
    monkeypatch.setattr(drawbox_core, "print_image",
                        lambda p, printer_type=None: None)
    h = Harness()
    h.feed({"type": "conversation.item.input_audio_transcription.completed",
            "transcript": "authorize"})
    assert any("Pairing mode!" in s for s in h.spoken)
    assert h.session.done is False  # admin commands are not strikes
    assert drawbox_core.PAIRING_FILE.exists()


def test_clean_conversation_flows_through(drawbox_dir):
    h = Harness()
    h.feed({"type": "conversation.item.input_audio_transcription.completed",
            "transcript": "a friendly dragon please"})
    assert h.spoken == []
    assert h.sent == []
    assert h.session.done is False


def test_resample_produces_pcm16_at_agent_rate():
    chunk = np.full((441, 1), 0.5, dtype=np.float32)
    data = drawbox_realtime.resample_to_pcm16(chunk)
    assert len(data) == 240 * 2  # 441 @ 44.1k → 240 @ 24k, 2 bytes each
    vals = np.frombuffer(data, dtype="<i2")
    assert abs(int(vals[10]) - 16383) <= 2
