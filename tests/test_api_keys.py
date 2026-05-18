"""API key precedence: file > env > empty."""

import json

import drawbox_core


def test_keys_default_to_empty(drawbox_dir, monkeypatch):
    for v in ("OPENAI_API_KEY", "REPLICATE_API_TOKEN",
              "GEMINI_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    keys = drawbox_core._load_api_keys()
    assert keys == {"openai": "", "replicate": "", "gemini": "", "elevenlabs": ""}


def test_keys_from_env(drawbox_dir, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r8-env")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-env")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-env")
    keys = drawbox_core._load_api_keys()
    assert keys["openai"] == "sk-env"
    assert keys["replicate"] == "r8-env"
    assert keys["gemini"] == "AIza-env"
    assert keys["elevenlabs"] == "el-env"


def test_keys_file_overrides_env(drawbox_dir, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    drawbox_core.API_KEYS_FILE.write_text(json.dumps({"openai": "sk-file"}))
    keys = drawbox_core._load_api_keys()
    assert keys["openai"] == "sk-file"


def test_keys_env_fills_gaps_when_file_missing_a_key(drawbox_dir, monkeypatch):
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r8-env")
    drawbox_core.API_KEYS_FILE.write_text(json.dumps({"openai": "sk-file"}))
    keys = drawbox_core._load_api_keys()
    assert keys["openai"] == "sk-file"
    assert keys["replicate"] == "r8-env"


def test_apply_api_keys_rebuilds_client(drawbox_dir, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    drawbox_core.apply_api_keys()
    assert drawbox_core.OPENAI_API_KEY == "sk-test"
    assert drawbox_core.client is not None

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    drawbox_core.API_KEYS_FILE.unlink(missing_ok=True)
    drawbox_core.apply_api_keys()
    assert drawbox_core.OPENAI_API_KEY == ""
    assert drawbox_core.client is None


def test_corrupted_keys_file_falls_back_to_env(drawbox_dir, monkeypatch):
    drawbox_core.API_KEYS_FILE.write_text("{ not json")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fallback")
    keys = drawbox_core._load_api_keys()
    assert keys["openai"] == "sk-fallback"


def test_mask_key():
    assert drawbox_core.mask_key("") == ""
    assert drawbox_core.mask_key("short") == "****"
    assert drawbox_core.mask_key("sk-abcdef123456", head=4) == "sk-a…"
    assert drawbox_core.mask_key("sk-abcdef123456", head=4, tail=4) == "sk-a…3456"
