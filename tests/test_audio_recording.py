"""Audio recording fallback behavior."""

import sys
import types

import numpy as np

# The cloud test image does not have the native PortAudio library installed.
# Provide the tiny sounddevice surface this test patches before importing the
# Pi runtime module.
fake_sounddevice = types.SimpleNamespace(
    default=types.SimpleNamespace(device=[-1, None]),
    PortAudioError=RuntimeError,
    query_devices=lambda device=None: [],
    InputStream=None,
)
sys.modules.setdefault("sounddevice", fake_sounddevice)

import drawbox


def test_candidate_input_devices_prioritizes_usb_then_default(monkeypatch):
    devices = [
        {"name": "Built-in Output", "max_input_channels": 0},
        {"name": "USB PnP Sound Device: Audio (hw:3,0)", "max_input_channels": 1},
        {"name": "Built-in Mic", "max_input_channels": 1},
    ]
    monkeypatch.setattr(drawbox.sd, "query_devices", lambda device=None: devices if device is None else devices[device])
    monkeypatch.setattr(drawbox.sd.default, "device", [2, None], raising=False)

    assert drawbox._candidate_input_devices() == [1, 2, None]


def test_record_audio_tries_next_device_after_open_failure(monkeypatch, tmp_path):
    devices = [
        {"name": "USB PnP Sound Device: Audio (hw:3,0)", "max_input_channels": 1},
        {"name": "USB PnP Sound Device: Audio (hw:2,0)", "max_input_channels": 1},
    ]
    attempts = []
    in_callback = {"value": False}

    def query_devices(device=None):
        if in_callback["value"]:
            raise AssertionError("query_devices must not run from audio callback")
        return devices if device is None else devices[device]

    monkeypatch.setattr(drawbox.sd, "query_devices", query_devices)
    monkeypatch.setattr(drawbox.sd.default, "device", [-1, None], raising=False)
    monkeypatch.setattr(drawbox.time, "sleep", lambda _seconds: None)

    class FakeInputStream:
        def __init__(self, samplerate, channels, callback, device):
            self.callback = callback
            self.device = device
            attempts.append(device)

        def __enter__(self):
            if self.device == 0:
                raise drawbox.sd.PortAudioError("stale ALSA card")
            audio = np.ones((drawbox.SAMPLE_RATE, 1), dtype=np.float32)
            in_callback["value"] = True
            self.callback(audio, len(audio), None, "input overflow")
            in_callback["value"] = False
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(drawbox.sd, "InputStream", FakeInputStream)
    monkeypatch.setattr(drawbox.sf, "write", lambda path, audio, sample_rate: None)

    path = drawbox.record_audio(seconds=1)

    assert attempts == [0, 1]
    assert path is not None


def test_record_audio_returns_none_when_all_devices_fail(monkeypatch):
    devices = [
        {"name": "USB PnP Sound Device: Audio (hw:3,0)", "max_input_channels": 1},
    ]

    monkeypatch.setattr(drawbox.sd, "query_devices", lambda device=None: devices if device is None else devices[device])
    monkeypatch.setattr(drawbox.sd.default, "device", [-1, None], raising=False)

    class FailingInputStream:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            raise drawbox.sd.PortAudioError("illegal device")

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(drawbox.sd, "InputStream", FailingInputStream)

    assert drawbox.record_audio(seconds=1) is None
