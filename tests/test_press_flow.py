"""The button-press pipeline honors dashboard settings (two-boxes parity).

The web and ESP32 paths read image_model and record_seconds per request;
these tests pin the button daemon to the same contract, plus the ack flow
(personalized line while generating, canned fallback on any failure).
"""

import types

import drawbox
import drawbox_core


def _fake_voice(events):
    return types.SimpleNamespace(
        play=lambda key, block=True: events.append(key),
        speak_once=lambda text, fallback_key=None: events.append(("ack", text)),
        play_jokes_until_done=lambda thread: thread.join(),
    )


def _patch_pipeline(monkeypatch, seen):
    def fake_record(seconds):
        seen["record_seconds"] = seconds
        return "clip.wav"

    def fake_generate(text, model=None):
        seen["model"] = model
        return "page.png"

    monkeypatch.setattr(drawbox, "record_audio", fake_record)
    monkeypatch.setattr(drawbox, "transcribe", lambda path: "a friendly dragon")
    monkeypatch.setattr(drawbox, "generate_image", fake_generate)
    monkeypatch.setattr(drawbox, "print_image",
                        lambda path: seen.__setitem__("printed", path))
    monkeypatch.setattr(drawbox, "log_print_event",
                        lambda prompt, model, duration:
                        seen.__setitem__("logged_model", model))


def test_handle_press_uses_dashboard_model_and_record_seconds(drawbox_dir, monkeypatch):
    drawbox_core.save_settings({"image_model": "gpt-image", "record_seconds": 5,
                                "natural_ack": False})
    seen = {}
    events = []
    _patch_pipeline(monkeypatch, seen)

    drawbox._handle_press(_fake_voice(events))

    assert seen["record_seconds"] == 5
    assert seen["model"] == "gpt-image"
    assert seen["logged_model"] == "gpt-image"
    assert seen["printed"] == "page.png"
    assert "thinking" in events  # natural_ack off → canned line
    assert events[-1] == "done"


def test_handle_press_speaks_personalized_ack(drawbox_dir, monkeypatch):
    seen = {}
    events = []
    _patch_pipeline(monkeypatch, seen)
    monkeypatch.setattr(drawbox_core, "generate_ack_text",
                        lambda transcript: f"Ooh, {transcript}!")

    drawbox._handle_press(_fake_voice(events))

    assert ("ack", "Ooh, a friendly dragon!") in events
    assert "thinking" not in events
    assert events[-1] == "done"


def test_handle_press_falls_back_to_canned_line_when_ack_fails(drawbox_dir, monkeypatch):
    seen = {}
    events = []
    _patch_pipeline(monkeypatch, seen)

    def boom(_transcript):
        raise RuntimeError("no key")

    monkeypatch.setattr(drawbox_core, "generate_ack_text", boom)

    drawbox._handle_press(_fake_voice(events))

    assert "thinking" in events
    assert events[-1] == "done"


def test_handle_press_quiet_recording_plays_too_short(drawbox_dir, monkeypatch):
    events = []
    monkeypatch.setattr(drawbox, "record_audio", lambda seconds: None)
    monkeypatch.setattr(drawbox, "transcribe",
                        lambda path: (_ for _ in ()).throw(
                            AssertionError("must not transcribe silence")))

    drawbox._handle_press(_fake_voice(events))

    assert events == ["listening", "too_short"]
