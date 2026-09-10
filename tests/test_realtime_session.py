"""AgentSession policy: moderation both directions, admin interception,
tool-call execution — no socket, no hardware.

Events are the AI SDK's normalized realtime protocol as the Gateway emits
it (kebab-case types, camelCase fields)."""

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


def _audio(response_id, data):
    return {"type": "audio-delta", "responseId": response_id,
            "itemId": "it", "delta": _b64(data), "raw": {}}


def _heard(transcript, item_id=None):
    ev = {"type": "input-transcription-completed", "itemId": item_id or "",
          "transcript": transcript, "raw": {}}
    return ev


def test_audio_deltas_reach_the_speaker(drawbox_dir):
    h = Harness()
    h.feed(_audio("r1", b"\x01\x02\x03\x04"))
    assert h.audio == b"\x01\x02\x03\x04"


def test_legacy_openai_event_names_are_ignored(drawbox_dir):
    """The Gateway never sends snake_case OpenAI/xAI events; if one shows
    up it must not be mistaken for audio or a transcript."""
    h = Harness()
    h.feed({"type": "response.audio.delta", "response_id": "r1",
            "delta": _b64(b"zz")},
           {"type": "session.updated"})
    assert h.audio == b""
    assert h.session.configured is False


def test_tool_call_executes_gated_pipeline(drawbox_dir, monkeypatch):
    seen = {}

    def fake_tool(description):
        seen["desc"] = description
        return {"ok": True, "message": "printing!"}

    monkeypatch.setattr(drawbox_core, "execute_draw_tool", fake_tool)
    h = Harness()
    h.feed({"type": "function-call-arguments-done", "responseId": "r1",
            "itemId": "i1", "callId": "c1", "name": "draw_coloring_page",
            "arguments": json.dumps({"description": "a dragon"}), "raw": {}})

    assert seen["desc"] == "a dragon"
    assert h.sent[0]["type"] == "conversation-item-create"
    item = h.sent[0]["item"]
    assert item["type"] == "function-call-output"
    assert item["callId"] == "c1"
    assert item["name"] == "draw_coloring_page"
    assert json.loads(item["output"]) == {"ok": True, "message": "printing!"}
    assert h.sent[1] == {"type": "response-create"}


def test_unknown_tool_reports_back_without_drawing(drawbox_dir, monkeypatch):
    monkeypatch.setattr(
        drawbox_core, "execute_draw_tool",
        lambda d: (_ for _ in ()).throw(AssertionError("wrong tool ran")))
    h = Harness()
    h.feed({"type": "function-call-arguments-done", "responseId": "r1",
            "itemId": "i1", "callId": "c9", "name": "rm_dash_rf",
            "arguments": "{}", "raw": {}})
    out = json.loads(h.sent[0]["item"]["output"])
    assert out["ok"] is False
    assert "Unknown tool" in out["message"]


def test_blocked_agent_output_is_killed_mid_response(drawbox_dir):
    drawbox_core.ensure_safety_mode_default()
    h = Harness()
    h.feed(
        _audio("r1", b"11"),
        {"type": "audio-transcript-delta", "responseId": "r1",
         "itemId": "it", "delta": "here is a gun for", "raw": {}},
        _audio("r1", b"22"),
    )
    assert h.audio == b"11"       # nothing enqueued after the kill
    assert h.cleared == 1         # unplayed audio dropped
    assert {"type": "response-cancel"} in h.sent
    assert "blocked" in h.spoken
    assert h.session.block_strikes == 1


def test_text_delta_is_moderated_like_audio_transcript(drawbox_dir):
    drawbox_core.ensure_safety_mode_default()
    h = Harness()
    h.feed({"type": "text-delta", "responseId": "r2", "itemId": "it",
            "delta": "a bloody knife", "raw": {}})
    assert "blocked" in h.spoken
    assert h.session.block_strikes == 1


def test_killed_input_also_silences_the_inflight_response(drawbox_dir):
    """A blocklist hit on the KID's words must stop the agent's current
    reply too — later audio deltas for that response stay out of the
    speaker queue (Bugbot, PR #39)."""
    drawbox_core.ensure_safety_mode_default()
    h = Harness()
    h.feed(
        {"type": "response-created", "responseId": "r7", "raw": {}},
        _audio("r7", b"11"),
        _heard("draw a gun"),
        _audio("r7", b"22"),
    )
    assert h.audio == b"11"
    assert h.cleared == 1
    assert "blocked" in h.spoken


def test_response_done_forgets_the_transcript(drawbox_dir):
    h = Harness()
    h.feed({"type": "audio-transcript-delta", "responseId": "r3",
            "itemId": "it", "delta": "hello", "raw": {}})
    assert "r3" in h.session._out_transcripts
    h.feed({"type": "response-done", "responseId": "r3",
            "status": "completed", "raw": {}})
    assert "r3" not in h.session._out_transcripts


def test_error_before_configure_fails_the_session(drawbox_dir):
    h = Harness()
    h.feed({"type": "error", "message": "bad session config",
            "code": "invalid_session", "raw": {}})
    assert h.session.done is True
    assert h.session.failed is True


def test_error_after_configure_is_recoverable(drawbox_dir):
    h = Harness()
    h.feed(
        {"type": "session-updated", "raw": {}},
        {"type": "error", "message": "transient", "raw": {}},
    )
    assert h.session.configured is True
    assert h.session.done is False
    assert h.session.failed is False


def test_speech_started_counts_as_activity(drawbox_dir):
    h = Harness()
    h.session.last_activity = 0
    h.feed({"type": "speech-started", "itemId": "i1", "raw": {}})
    assert h.session.last_activity > 0


def test_two_blocklist_strikes_end_the_session(drawbox_dir):
    drawbox_core.ensure_safety_mode_default()
    h = Harness()
    h.feed(_heard("draw a gun", "i1"))
    assert h.session.done is False
    h.feed(_heard("a bloody sword", "i2"))
    assert h.session.done is True
    assert h.spoken.count("blocked") == 2


def test_duplicate_transcription_snapshots_intercept_once(drawbox_dir, monkeypatch):
    """xAI can emit multiple cumulative completed snapshots per item; one
    utterance must not open two pairing windows or burn two strikes."""
    monkeypatch.setattr(drawbox_core, "print_image",
                        lambda p, printer_type=None: None)
    h = Harness()
    h.feed(_heard("authorize", "i1"), _heard("authorize", "i1"))
    assert len([s for s in h.spoken if "Pairing mode!" in s]) == 1


def test_admin_command_intercepted_deterministically(drawbox_dir, monkeypatch):
    monkeypatch.setattr(drawbox_core, "print_image",
                        lambda p, printer_type=None: None)
    h = Harness()
    h.feed(_heard("authorize", "i1"))
    assert any("Pairing mode!" in s for s in h.spoken)
    assert h.session.done is False  # admin commands are not strikes
    assert drawbox_core.PAIRING_FILE.exists()


def test_clean_conversation_flows_through(drawbox_dir):
    h = Harness()
    h.feed(_heard("a friendly dragon please", "i1"))
    assert h.spoken == []
    assert h.sent == []
    assert h.session.done is False


def test_resample_produces_pcm16_at_agent_rate():
    chunk = np.full((441, 1), 0.5, dtype=np.float32)
    data = drawbox_realtime.resample_to_pcm16(chunk)
    assert len(data) == 240 * 2  # 441 @ 44.1k → 240 @ 24k, 2 bytes each
    vals = np.frombuffer(data, dtype="<i2")
    assert abs(int(vals[10]) - 16383) <= 2


def test_agent_audio_rate_matches_session_config():
    assert drawbox_realtime.AGENT_AUDIO_RATE == \
        drawbox_core.AGENT_AUDIO_FORMAT["rate"] == 24000
