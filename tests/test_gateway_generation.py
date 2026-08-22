"""Vercel AI Gateway image generation (mocked HTTP)."""

import base64
import json
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

import drawbox_core


def _tiny_png_b64():
    buf = BytesIO()
    Image.new("L", (8, 8), 255).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _fake_urlopen(payload, calls):
    def fake(req, timeout=None):
        calls.append((req, timeout))
        return FakeResponse(payload)
    return fake


def test_gateway_image_model_uses_images_generations(drawbox_dir, monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "vck-test")
    calls = []
    payload = {"data": [{"b64_json": _tiny_png_b64()}]}
    monkeypatch.setattr(drawbox_core, "urlopen", _fake_urlopen(payload, calls))

    path = drawbox_core.generate_image("a cat", model="bfl/flux-pro-1.1")

    req, timeout = calls[0]
    assert req.full_url == "https://ai-gateway.vercel.sh/v1/images/generations"
    assert timeout == 120
    assert req.get_header("Authorization") == "Bearer vck-test"
    body = json.loads(req.data)
    assert body["model"] == "bfl/flux-pro-1.1"
    assert "a cat" in body["prompt"]
    assert body["response_format"] == "b64_json"
    with Image.open(path) as img:
        assert img.size == (drawbox_core.CANVAS_W, drawbox_core.CANVAS_H)
    Path(path).unlink()


def test_gateway_chat_model_uses_chat_completions(drawbox_dir, monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "vck-test")
    calls = []
    payload = {"choices": [{"finish_reason": "stop", "message": {"images": [
        {"type": "image_url",
         "image_url": {"url": "data:image/png;base64," + _tiny_png_b64()}},
    ]}}]}
    monkeypatch.setattr(drawbox_core, "urlopen", _fake_urlopen(payload, calls))

    path = drawbox_core.generate_image(
        "a dog", model="google/gemini-3.1-flash-image-preview")

    req, _timeout = calls[0]
    assert req.full_url == "https://ai-gateway.vercel.sh/v1/chat/completions"
    body = json.loads(req.data)
    assert body["model"] == "google/gemini-3.1-flash-image-preview"
    assert body["messages"][0]["role"] == "user"
    assert "a dog" in body["messages"][0]["content"]
    with Image.open(path) as img:
        assert img.size == (drawbox_core.CANVAS_W, drawbox_core.CANVAS_H)
    Path(path).unlink()


def test_gateway_model_without_key_raises(drawbox_dir, monkeypatch):
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="AI_GATEWAY_API_KEY"):
        drawbox_core.generate_image("cat", model="bfl/flux-pro-1.1")


def test_unsupported_model_still_raises(drawbox_dir):
    with pytest.raises(ValueError, match="unsupported model"):
        drawbox_core.generate_image("cat", model="not-a-model")


def test_model_registry_shape():
    for legacy in ("nano-banana", "flux-schnell", "gpt-image"):
        assert legacy in drawbox_core.SUPPORTED_MODELS
    for chat_model in drawbox_core.GATEWAY_CHAT_IMAGE_MODELS:
        assert chat_model in drawbox_core.SUPPORTED_MODELS
    assert drawbox_core.GATEWAY_CHAT_IMAGE_MODELS <= frozenset(
        drawbox_core.GATEWAY_IMAGE_MODELS)
