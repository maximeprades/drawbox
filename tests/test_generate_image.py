"""Image generation routes every model through AI Gateway."""

import base64
import logging
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
    assert seen["response_format"] == "b64_json"
    assert seen["prompt"].endswith("Child requested: a rocket")
    assert path.endswith(".png")


def test_generate_image_remembers_the_last_page(drawbox_dir, monkeypatch):
    png = _png_bytes()

    def fake_generate(**kwargs):
        return SimpleNamespace(data=[SimpleNamespace(
            b64_json=base64.b64encode(png).decode(), url=None,
        )])

    _gateway_client(monkeypatch, images_generate=fake_generate)
    path = drawbox_core.generate_image("a rocket", model="gpt-image")

    assert drawbox_core.LAST_IMAGE_FILE.exists()
    with open(path, "rb") as f:
        assert drawbox_core.LAST_IMAGE_FILE.read_bytes() == f.read()


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
    assert seen["response_format"] == "b64_json"


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


def test_generate_catalog_model_by_gateway_id(drawbox_dir, monkeypatch):
    png = _png_bytes()
    seen = {}

    def fake_generate(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(data=[SimpleNamespace(
            b64_json=base64.b64encode(png).decode(), url=None,
        )])

    _gateway_client(monkeypatch, images_generate=fake_generate)
    path = drawbox_core.generate_image("a cat", model="bfl/flux-pro-1.1")
    assert seen["model"] == "bfl/flux-pro-1.1"
    assert seen["response_format"] == "b64_json"
    assert path.endswith(".png")


def test_generate_catalog_chat_model_by_gateway_id(drawbox_dir, monkeypatch):
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
    drawbox_core.generate_image("a dog", model="google/gemini-3-pro-image")
    assert seen["model"] == "google/gemini-3-pro-image"
    assert seen["extra_body"]["modalities"] == ["text", "image"]


def test_image_routes_cover_presets_and_catalog():
    for preset in ("nano-banana", "flux-schnell", "gpt-image"):
        assert preset in drawbox_core.IMAGE_ROUTES
    assert set(drawbox_core.GATEWAY_IMAGE_CATALOG.values()) == {"chat", "images"}
    for slug, api in drawbox_core.GATEWAY_IMAGE_CATALOG.items():
        route_api, route_slug, _kwargs = drawbox_core.IMAGE_ROUTES[slug]
        assert route_api == api
        assert route_slug == slug
    assert drawbox_core.SUPPORTED_MODELS == tuple(drawbox_core.IMAGE_ROUTES)


def test_chat_no_image_error_surfaces_model_answer(caplog):
    completion = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content="I cannot draw that"),
        finish_reason="content_filter",
    )])

    with caplog.at_level(logging.ERROR, logger="drawbox"), \
            pytest.raises(RuntimeError) as excinfo:
        drawbox_core._image_bytes_from_chat(completion)

    assert str(excinfo.value).startswith("No image in gateway chat response")
    assert "I cannot draw that" in str(excinfo.value)
    assert "content_filter" in str(excinfo.value)
    assert "I cannot draw that" in caplog.text
