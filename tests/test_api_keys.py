"""API key precedence: file > env > empty."""

import json

import drawbox_core


def test_keys_default_to_empty(drawbox_dir, monkeypatch):
    for v in ("AI_GATEWAY_API_KEY", "ELEVENLABS_API_KEY", "XAI_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    keys = drawbox_core._load_api_keys()
    assert keys == {"ai_gateway": "", "elevenlabs": "", "xai": ""}


def test_keys_from_env(drawbox_dir, monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "vck-env")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-env")
    monkeypatch.setenv("XAI_API_KEY", "xai-env")
    keys = drawbox_core._load_api_keys()
    assert keys["ai_gateway"] == "vck-env"
    assert keys["elevenlabs"] == "el-env"
    assert keys["xai"] == "xai-env"


def test_keys_file_overrides_env(drawbox_dir, monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "vck-env")
    drawbox_core.API_KEYS_FILE.write_text(json.dumps({"ai_gateway": "vck-file"}))
    keys = drawbox_core._load_api_keys()
    assert keys["ai_gateway"] == "vck-file"


def test_apply_api_keys_rebuilds_client(drawbox_dir, monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "vck-test")
    drawbox_core.apply_api_keys()
    assert drawbox_core.AI_GATEWAY_API_KEY == "vck-test"
    assert drawbox_core.client is not None
    assert str(drawbox_core.client.base_url).rstrip("/") == drawbox_core.AI_GATEWAY_BASE_URL

    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    drawbox_core.API_KEYS_FILE.unlink(missing_ok=True)
    drawbox_core.apply_api_keys()
    assert drawbox_core.AI_GATEWAY_API_KEY == ""
    assert drawbox_core.client is None


def test_corrupted_keys_file_falls_back_to_env(drawbox_dir, monkeypatch):
    drawbox_core.API_KEYS_FILE.write_text("{ not json")
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "vck-fallback")
    keys = drawbox_core._load_api_keys()
    assert keys["ai_gateway"] == "vck-fallback"


def test_old_provider_keys_are_ignored(drawbox_dir, monkeypatch):
    for v in ("AI_GATEWAY_API_KEY", "ELEVENLABS_API_KEY", "XAI_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    drawbox_core.API_KEYS_FILE.write_text(json.dumps({
        "openai": "sk-old",
        "replicate": "r8-old",
        "gemini": "AIza-old",
        "elevenlabs": "el-old",
    }))
    keys = drawbox_core._load_api_keys()
    # Retired direct-image keys stay dead; elevenlabs lives on for the
    # elevenlabs voice provider.
    assert keys == {"ai_gateway": "", "elevenlabs": "el-old", "xai": ""}


def test_resolve_tts_voice():
    assert drawbox_core.resolve_tts_voice("nova") == "nova"
    assert drawbox_core.resolve_tts_voice("NOVA") == "nova"
    assert drawbox_core.resolve_tts_voice("xNtG3W2oqJs0cJZuTyBc") == "alloy"
    assert drawbox_core.resolve_tts_voice("") == "alloy"


def test_mask_key():
    assert drawbox_core.mask_key("") == ""
    assert drawbox_core.mask_key("short") == "****"
    assert drawbox_core.mask_key("sk-abcdef123456", head=4) == "sk-a…"
    assert drawbox_core.mask_key("sk-abcdef123456", head=4, tail=4) == "sk-a…3456"
