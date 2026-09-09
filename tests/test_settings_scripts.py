"""Settings + voice-script load/save round-trips."""

import json

import drawbox_core


def test_load_settings_returns_defaults_when_missing(drawbox_dir):
    s = drawbox_core.load_settings()
    assert s["coloring_prompt"] == drawbox_core.DEFAULT_COLORING_PROMPT
    assert s["record_seconds"] == 10
    assert s["tts_voice_id"] == "alloy"
    assert s["voice_provider"] == "gateway"
    assert s["tts_stability"] == 0.5


def test_save_then_load_round_trips(drawbox_dir):
    drawbox_core.save_settings({"record_seconds": 7, "tts_voice_id": "nova"})
    s = drawbox_core.load_settings()
    assert s["record_seconds"] == 7
    assert s["tts_voice_id"] == "nova"
    # Other defaults remain
    assert s["coloring_prompt"] == drawbox_core.DEFAULT_COLORING_PROMPT


def test_load_settings_resolves_unknown_voice_and_drops_dead_keys(drawbox_dir):
    drawbox_core.SETTINGS_FILE.write_text(json.dumps({
        "tts_voice_id": "xNtG3W2oqJs0cJZuTyBc",
        "record_seconds": 12,
        "whisper_language": "en",
    }))
    s = drawbox_core.load_settings()
    # Gateway voice ids are clamped to a known OpenAI voice; the historical
    # ElevenLabs id lives in elevenlabs_voice_id now.
    assert s["tts_voice_id"] == "alloy"
    assert s["record_seconds"] == 12
    assert "whisper_language" not in s


def test_load_settings_clamps_unknown_voice_provider(drawbox_dir):
    drawbox_core.SETTINGS_FILE.write_text(json.dumps({"voice_provider": "alexa"}))
    assert drawbox_core.load_settings()["voice_provider"] == "gateway"


def test_load_settings_clamps_stale_image_model(drawbox_dir, monkeypatch):
    # A model removed from the gateway catalog must not brick the button.
    drawbox_core.SETTINGS_FILE.write_text(json.dumps({"image_model": "dall-e-1"}))
    monkeypatch.setattr(drawbox_core, "IMAGE_MODEL", "flux-schnell")
    assert drawbox_core.load_settings()["image_model"] == "flux-schnell"
    # And a garbage env default falls through to the safe preset.
    monkeypatch.setattr(drawbox_core, "IMAGE_MODEL", "also-removed")
    assert drawbox_core.load_settings()["image_model"] == "nano-banana"


def test_load_settings_clamps_record_seconds(drawbox_dir):
    drawbox_core.SETTINGS_FILE.write_text(json.dumps({"record_seconds": 999}))
    assert drawbox_core.load_settings()["record_seconds"] == 30
    drawbox_core.SETTINGS_FILE.write_text(json.dumps({"record_seconds": 1}))
    assert drawbox_core.load_settings()["record_seconds"] == 3
    drawbox_core.SETTINGS_FILE.write_text(json.dumps({"record_seconds": "nope"}))
    assert drawbox_core.load_settings()["record_seconds"] == 10


def test_corrupted_settings_falls_back_to_defaults(drawbox_dir):
    drawbox_core.SETTINGS_FILE.write_text("not json")
    s = drawbox_core.load_settings()
    assert s["coloring_prompt"] == drawbox_core.DEFAULT_COLORING_PROMPT


def test_settings_with_non_dict_payload(drawbox_dir):
    drawbox_core.SETTINGS_FILE.write_text(json.dumps([1, 2, 3]))
    s = drawbox_core.load_settings()
    assert s == dict(drawbox_core.DEFAULT_SETTINGS)


def test_default_scripts_match_defaults(drawbox_dir):
    s = drawbox_core.default_scripts()
    assert set(s["voice_lines"]) == set(drawbox_core.DEFAULT_VOICE_LINES)
    assert s["jokes"] == list(drawbox_core.DEFAULT_JOKES)


def test_save_scripts_sanitizes_types(drawbox_dir):
    drawbox_core.save_scripts({
        "voice_lines": {"ready": "Hi", "bad": 123, 42: "oops"},
        "jokes": ["A joke", "", 42, "Another"],
    })
    saved = json.loads(drawbox_core.SCRIPTS_FILE.read_text())
    assert saved["voice_lines"] == {"ready": "Hi"}
    assert saved["jokes"] == ["A joke", "", "Another"]


def test_save_scripts_caps_lengths(drawbox_dir):
    drawbox_core.save_scripts({
        "voice_lines": {"ready": "x" * 1000},
        "jokes": ["y" * 1000] * 200,
    })
    saved = json.loads(drawbox_core.SCRIPTS_FILE.read_text())
    assert len(saved["voice_lines"]["ready"]) == 500
    assert len(saved["jokes"]) == 100
    assert all(len(j) <= 300 for j in saved["jokes"])


def test_load_scripts_falls_back_on_bad_file(drawbox_dir):
    drawbox_core.SCRIPTS_FILE.write_text("not json")
    s = drawbox_core.load_scripts()
    assert set(s["voice_lines"]) == set(drawbox_core.DEFAULT_VOICE_LINES)
