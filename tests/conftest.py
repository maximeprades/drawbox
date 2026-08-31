"""Test fixtures.

Every test gets an isolated ``~/.drawbox`` directory under a ``tmp_path`` so
nothing leaks between tests (and your real settings file stays intact).
We patch the module-level path constants on each of the modules that hold
them — this is simpler and faster than reloading the modules.
"""

import os
import sys
import types
from pathlib import Path

import pytest

# Make the project root importable as `drawbox_core`, `drawbox_web`.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The cloud test image does not have the native PortAudio library, which makes
# `import sounddevice` (and so `import drawbox`) fail. Register the tiny stub
# surface the tests patch before any test module imports the Pi runtime.
fake_sounddevice = types.SimpleNamespace(
    default=types.SimpleNamespace(device=[-1, None]),
    PortAudioError=RuntimeError,
    query_devices=lambda device=None: [],
    InputStream=None,
)
sys.modules.setdefault("sounddevice", fake_sounddevice)


@pytest.fixture
def fake_journal(monkeypatch):
    """Replace journalctl's Popen with a real pipe; yields the write fd.

    The /api/logs route selects and os.reads the raw fd, so the fake must
    be a real pipe, not an iterator. Close the fd to make "journalctl"
    exit, which ends the stream.
    """
    import drawbox_web

    r_fd, w_fd = os.pipe()
    proc = types.SimpleNamespace(
        stdout=os.fdopen(r_fd, "rb"),
        kill=lambda: None,
        wait=lambda timeout=None: 0,
    )
    monkeypatch.setattr(drawbox_web.subprocess, "Popen", lambda *a, **k: proc)
    yield w_fd
    try:
        os.close(w_fd)
    except OSError:
        pass


@pytest.fixture
def drawbox_dir(tmp_path, monkeypatch):
    """Redirect all on-disk paths to a temp directory. Returns its Path."""
    import drawbox_core

    dx = tmp_path / "drawbox"
    dx.mkdir()
    overrides = {
        "DRAWBOX_DIR": dx,
        "API_KEYS_FILE": dx / "api_keys.json",
        "SETTINGS_FILE": dx / "web_settings.json",
        "PLEASE_MODE_FILE": dx / "please_mode",
        "SAFETY_MODE_FILE": dx / "safety_mode",
        "PRINT_LOG_FILE": dx / "print_log.jsonl",
        "SCRIPTS_FILE": dx / "voice_scripts.json",
        "CACHE_DIR": dx / "voice_cache",
        "PAIRING_FILE": dx / "pairing.json",
        "PAIRED_DEVICES_FILE": dx / "paired_devices.json",
        "LAST_IMAGE_FILE": dx / "last_generated.png",
    }
    for name, value in overrides.items():
        monkeypatch.setattr(drawbox_core, name, value)

    # Apply the same overrides on drawbox_web — it imported the names at
    # module load time, so they're locals there.
    if "drawbox_web" in sys.modules:
        import drawbox_web
        for name, value in overrides.items():
            if hasattr(drawbox_web, name):
                monkeypatch.setattr(drawbox_web, name, value)
        # Cross-test state: pairing throttle and status cache would leak
        # 429s / stale payloads between tests otherwise.
        drawbox_web._PAIR_ATTEMPTS.clear()
        drawbox_web._DEVICE_STATUS.clear()
        drawbox_web._STATUS_CACHE.update(ts=0.0, payload=None)

    yield dx


@pytest.fixture
def client(drawbox_dir, monkeypatch):
    """Flask test client paired as a device, with an isolated drawbox dir."""
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("REPLICATE_API_TOKEN", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    import drawbox_core
    import drawbox_web
    drawbox_core.apply_api_keys()
    drawbox_web.app.testing = True
    test_client = drawbox_web.app.test_client()
    # Pair through the real flow so every test exercises the token guard.
    code = drawbox_core.open_pairing_window()
    token = drawbox_core.redeem_pairing_code(code, "tests")
    test_client.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    return test_client
