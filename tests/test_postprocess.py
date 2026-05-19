"""Image post-processing: thresholding + fit-on-letter canvas."""

import io
import os

from PIL import Image

from drawbox_core import CANVAS_H, CANVAS_MARGIN, CANVAS_W, _postprocess


def _gradient_png(size=(512, 512)):
    """Return PNG bytes of a horizontal black→white gradient."""
    img = Image.new("L", size)
    for x in range(size[0]):
        for y in range(size[1]):
            img.putpixel((x, y), x * 255 // (size[0] - 1))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_postprocess_outputs_letter_size():
    path = _postprocess(_gradient_png())
    try:
        out = Image.open(path)
        assert out.size == (CANVAS_W, CANVAS_H)
    finally:
        os.unlink(path)


def test_postprocess_is_overwhelmingly_pure_bw():
    """The threshold step collapses to 1-bit, but the LANCZOS resize that
    follows reintroduces antialiased greys at edges. Those greys must stay a
    tiny fraction of total pixels — otherwise the threshold isn't doing its
    job."""
    path = _postprocess(_gradient_png())
    try:
        out = Image.open(path).convert("L")
        data = list(out.getdata())
        pure = sum(1 for p in data if p in (0, 255))
        assert pure / len(data) > 0.99
    finally:
        os.unlink(path)


def test_postprocess_keeps_margin():
    """The image content must not touch the canvas edges."""
    path = _postprocess(_gradient_png((100, 100)))
    try:
        out = Image.open(path).convert("L")
        w, h = out.size
        # Sample the canvas border rectangle — it should all be white (255).
        top = [out.getpixel((x, 0)) for x in range(0, w, 50)]
        bottom = [out.getpixel((x, h - 1)) for x in range(0, w, 50)]
        left = [out.getpixel((0, y)) for y in range(0, h, 50)]
        right = [out.getpixel((w - 1, y)) for y in range(0, h, 50)]
        assert set(top + bottom + left + right) == {255}
        # And the centre must have some black ink.
        cx, cy = w // 2, h // 2
        samples = [out.getpixel((cx + dx, cy + dy))
                   for dx in (-20, 0, 20) for dy in (-20, 0, 20)]
        assert 0 in samples
    finally:
        os.unlink(path)


def test_postprocess_margin_constant_sanity():
    # Just guards that the margin is non-trivial and smaller than half the canvas.
    assert 10 <= CANVAS_MARGIN < CANVAS_W // 2
