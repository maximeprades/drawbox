"""Image generation routes every model through AI Gateway."""

import base64
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

import drawbox_core


def _png_bytes(size=(16, 16), color=0):
    buf = BytesIO()
    Image.new("L", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _gateway_client(monkeypatch, images_generate=None, chat_create=None):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "vck-test")

    class FakeClient:
        def __init__(self, **_kwargs):
            self.images = SimpleNamespace(generate=images_generate)
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=chat_create),
            )

    monkeypatch.setattr(drawbox_core, "OpenAI", FakeClient)
    drawbox_core.apply_api_keys()


def test_generate_image_requires_gateway_key(drawbox_dir, monkeypatch):
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    drawbox_core.apply_api_keys()
    with pytest.raises(RuntimeError, match="AI_GATEWAY_API_KEY"):
        drawbox_core.generate_image("a cat", model="nano-banana")


def test_generate_image_rejects_unknown_model(drawbox_dir, monkeypatch):
    _gateway_client(monkeypatch)
    with pytest.raises(ValueError, match="unsupported model"):
        drawbox_core.generate_image("a cat", model="not-a-model")


def test_generate_gpt_image_uses_gateway_slug(drawbox_dir, monkeypatch):
    png = _png_bytes()
    seen = {}

    def fake_generate(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(data=[SimpleNamespace(
            b64_json=base64.b64encode(png).decode(), url=None,
        )])

    _gateway_client(monkeypatch, images_generate=fake_generate)
    path = drawbox_core.generate_image("a rocket", model="gpt-image")
    assert seen["model"] == "openai/gpt-image-2"
    assert seen["prompt"].endswith("Child requested: a rocket")
    assert path.endswith(".png")


def test_generate_flux_uses_gateway_slug(drawbox_dir, monkeypatch):
    png = _png_bytes()
    seen = {}

    def fake_generate(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(data=[SimpleNamespace(
            b64_json=base64.b64encode(png).decode(), url=None,
        )])

    _gateway_client(monkeypatch, images_generate=fake_generate)
    drawbox_core.generate_image("a boat", model="flux-schnell")
    assert seen["model"] == "bfl/flux-schnell"


def test_generate_nano_banana_uses_chat_modalities(drawbox_dir, monkeypatch):
    png = _png_bytes()
    data_url = "data:image/png;base64," + base64.b64encode(png).decode()
    seen = {}

    def fake_create(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            images=[SimpleNamespace(
                type="image_url",
                image_url=SimpleNamespace(url=data_url),
            )],
            content=None,
        ))])

    _gateway_client(monkeypatch, chat_create=fake_create)
    drawbox_core.generate_image("a kitty", model="nano-banana")
    assert seen["model"] == "google/gemini-3.1-flash-image-preview"
    assert seen["extra_body"]["modalities"] == ["text", "image"]
