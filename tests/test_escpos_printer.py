"""ESC/POS raster rendering, serial output, and the print_image dispatch."""

import os
import pty
import threading
import time

import pytest
from PIL import Image

import drawbox_core
import drawbox_escpos

INIT = b"\x1b\x40"
GS_V0 = b"\x1d\x76\x30"
FEED = b"\x0a" * drawbox_escpos.FEED_LINES


# ── render_raster ──────────────────────────────────

def test_render_raster_golden():
    img = Image.new("L", (384, 16), 255)
    img.paste(0, (0, 0, 384, 8))
    job = drawbox_escpos.render_raster(img)
    # White rows 8-15 are trimmed by the bbox crop, so the block is 8 tall.
    assert job == (INIT
                   + b"\x1d\x76\x30\x00\x30\x00\x08\x00"
                   + b"\xff" * (48 * 8)
                   + FEED)


def test_render_raster_bands_tall_images():
    job = drawbox_escpos.render_raster(Image.new("L", (384, 600), 0))
    pos = len(INIT)
    heights = []
    while job[pos:pos + 3] == GS_V0:
        assert job[pos + 3:pos + 6] == b"\x00\x30\x00"
        rows = job[pos + 6] + 256 * job[pos + 7]
        assert job[pos + 8:pos + 8 + 48 * rows] == b"\xff" * (48 * rows)
        heights.append(rows)
        pos += 8 + 48 * rows
    assert heights == [255, 255, 90]
    assert sum(heights) * 48 == 48 * 600
    assert job[pos:] == FEED


def test_render_raster_trims_margins_and_resizes():
    img = Image.new("L", (800, 400), 255)
    img.paste(0, (200, 100, 600, 300))
    job = drawbox_escpos.render_raster(img)
    # 400x200 crop scaled to width 384 -> height 192 (0xC0), one block.
    assert job.count(GS_V0) == 1
    assert job.startswith(INIT + b"\x1d\x76\x30\x00\x30\x00\xc0\x00")
    assert job[10:-len(FEED)] == b"\xff" * (48 * 192)


def test_render_raster_blank_image_feeds_only():
    job = drawbox_escpos.render_raster(Image.new("L", (384, 100), 255))
    assert job == INIT + FEED
    assert GS_V0 not in job


# ── serial output ──────────────────────────────────

def test_print_file_writes_job_over_serial(tmp_path):
    img_path = tmp_path / "job.png"
    Image.new("L", (384, 8), 0).save(img_path)
    master_fd, slave_fd = pty.openpty()
    try:
        drawbox_escpos.print_file(str(img_path), os.ttyname(slave_fd))
        job = os.read(master_fd, 65536)
    finally:
        os.close(master_fd)
        os.close(slave_fd)
    assert job.startswith(INIT)
    assert b"\x1d\x76\x30\x00\x30\x00\x08\x00" in job


def test_open_serial_rejects_unsupported_baud():
    with pytest.raises(ValueError):
        drawbox_escpos._open_serial("/dev/null", 12345)


# ── print_image dispatch ───────────────────────────

def test_print_image_dispatches_to_escpos(drawbox_dir, monkeypatch, tmp_path):
    drawbox_core.save_settings({
        "printer_type": "escpos_serial",
        "serial_port": "/dev/fake0",
        "serial_baud": 9600,
    })
    done = threading.Event()
    calls = []

    def fake_print_file(path, port, baud=9600):
        calls.append((path, port, baud))
        done.set()

    monkeypatch.setattr(drawbox_escpos, "print_file", fake_print_file)
    img_path = tmp_path / "page.png"
    Image.new("L", (10, 10), 0).save(img_path)

    drawbox_core.print_image(str(img_path))

    assert done.wait(timeout=5)
    assert calls == [(str(img_path), "/dev/fake0", 9600)]
    # The unlink happens after print_file in the thread's finally.
    for _ in range(100):
        if not img_path.exists():
            break
        time.sleep(0.05)
    assert not img_path.exists()


def test_print_image_defaults_to_lp(drawbox_dir, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(drawbox_core.subprocess, "run",
                        lambda argv, **kw: calls.append(argv))
    img_path = tmp_path / "page.png"
    img_path.write_bytes(b"fake-png")

    drawbox_core.print_image(str(img_path))

    assert calls == [["lp", "-d", drawbox_core.PRINTER_NAME,
                      "-o", "media=Letter", "-o", "fit-to-page", str(img_path)]]
    assert not img_path.exists()


def test_print_image_unknown_type_falls_back_to_lp(drawbox_dir, monkeypatch, tmp_path):
    drawbox_core.save_settings({"printer_type": "bogus"})
    calls = []
    monkeypatch.setattr(drawbox_core.subprocess, "run",
                        lambda argv, **kw: calls.append(argv))
    img_path = tmp_path / "page.png"
    img_path.write_bytes(b"fake-png")

    drawbox_core.print_image(str(img_path))

    assert len(calls) == 1
    assert calls[0][0] == "lp"
    assert not img_path.exists()


# ── CLI test pattern ───────────────────────────────

def test_cli_test_pattern_renders_raster():
    job = drawbox_escpos.render_raster(drawbox_escpos._test_pattern())
    assert GS_V0 in job
