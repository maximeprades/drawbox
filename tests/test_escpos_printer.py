"""ESC/POS raster rendering, serial/TCP output, and print_image dispatch."""

import os
import pty
import socket
import threading
import time
import types

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


def test_start_print_pumps_job_in_background(tmp_path):
    img_path = tmp_path / "job.png"
    Image.new("L", (384, 8), 0).save(img_path)
    master_fd, slave_fd = pty.openpty()
    try:
        sent = drawbox_escpos.start_print(str(img_path), os.ttyname(slave_fd))
        assert sent > 0
        os.set_blocking(master_fd, False)
        received = b""
        deadline = time.monotonic() + 5
        while len(received) < sent and time.monotonic() < deadline:
            try:
                received += os.read(master_fd, 65536)
            except BlockingIOError:
                time.sleep(0.01)
    finally:
        os.close(master_fd)
        os.close(slave_fd)
    assert received.startswith(INIT)
    assert len(received) == sent


# ── TCP output ─────────────────────────────────────

def _tcp_sink():
    """Listening socket that collects everything one client sends."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    received = bytearray()

    def run():
        conn, _ = srv.accept()
        with conn:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                received.extend(chunk)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return srv, thread, received


def test_print_file_tcp_writes_job(tmp_path):
    img_path = tmp_path / "job.png"
    Image.new("L", (384, 8), 0).save(img_path)
    srv, thread, received = _tcp_sink()
    try:
        sent = drawbox_escpos.print_file_tcp(
            str(img_path), "127.0.0.1", srv.getsockname()[1])
        thread.join(timeout=5)
    finally:
        srv.close()
    assert bytes(received).startswith(INIT)
    assert len(received) == sent


def test_start_print_tcp_pumps_job_in_background(tmp_path):
    img_path = tmp_path / "job.png"
    Image.new("L", (384, 8), 0).save(img_path)
    srv, thread, received = _tcp_sink()
    try:
        sent = drawbox_escpos.start_print_tcp(
            str(img_path), "127.0.0.1", srv.getsockname()[1])
        assert sent > 0
        thread.join(timeout=5)
    finally:
        srv.close()
    assert bytes(received).startswith(INIT)
    assert len(received) == sent


def test_pump_tcp_sizes_write_timeout_to_job():
    """Regression: sendall gets one deadline for the whole job and the
    bridge drains at ~960 B/s, so the 10 s connect timeout would truncate
    any page larger than the socket buffers. The write deadline must scale
    with job size."""
    calls = []
    sock = types.SimpleNamespace(
        settimeout=lambda t: calls.append(("timeout", t)),
        sendall=lambda job: calls.append(("sendall", len(job))),
        close=lambda: calls.append(("close", None)),
    )
    job = b"\x00" * 96_000  # a tall page: ~100 s of drain at 9600 baud
    drawbox_escpos._pump_tcp(sock, job)
    assert calls[0] == ("timeout", drawbox_escpos._write_timeout(len(job)))
    assert calls[0][1] >= 240
    assert calls[1] == ("sendall", len(job))
    assert calls[-1] == ("close", None)


def test_start_print_tcp_refused_connection_raises(tmp_path):
    img_path = tmp_path / "job.png"
    Image.new("L", (384, 8), 0).save(img_path)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()  # nothing listens here now
    with pytest.raises(OSError):
        drawbox_escpos.start_print_tcp(str(img_path), "127.0.0.1", port)


# ── print_image dispatch ───────────────────────────

def test_print_image_dispatches_to_escpos(drawbox_dir, monkeypatch, tmp_path):
    drawbox_core.save_settings({
        "printer_type": "escpos_serial",
        "serial_port": "/dev/fake0",
        "serial_baud": 9600,
    })
    calls = []
    monkeypatch.setattr(drawbox_escpos, "start_print",
                        lambda path, port, baud: calls.append((path, port, baud)))
    img_path = tmp_path / "page.png"
    Image.new("L", (10, 10), 0).save(img_path)

    drawbox_core.print_image(str(img_path))

    assert calls == [(str(img_path), "/dev/fake0", 9600)]
    assert not img_path.exists()


def test_print_image_escpos_failure_raises_and_unlinks(drawbox_dir, tmp_path):
    drawbox_core.save_settings({
        "printer_type": "escpos_serial",
        "serial_port": "/dev/nonexistent-drawbox-test",
        "serial_baud": 9600,
    })
    img_path = tmp_path / "page.png"
    Image.new("L", (10, 10), 0).save(img_path)

    with pytest.raises(OSError):
        drawbox_core.print_image(str(img_path))

    assert not img_path.exists()


def test_print_image_dispatches_to_escpos_tcp(drawbox_dir, monkeypatch, tmp_path):
    drawbox_core.save_settings({
        "printer_type": "escpos_tcp",
        "tcp_host": "printer.lan",
        "tcp_port": 9101,
    })
    calls = []
    monkeypatch.setattr(drawbox_escpos, "start_print_tcp",
                        lambda path, host, port: calls.append((path, host, port)))
    img_path = tmp_path / "page.png"
    Image.new("L", (10, 10), 0).save(img_path)

    drawbox_core.print_image(str(img_path))

    assert calls == [(str(img_path), "printer.lan", 9101)]
    assert not img_path.exists()


def test_print_image_tcp_failure_raises_and_unlinks(drawbox_dir, tmp_path):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()  # guaranteed-dead port
    drawbox_core.save_settings({
        "printer_type": "escpos_tcp",
        "tcp_host": "127.0.0.1",
        "tcp_port": port,
    })
    img_path = tmp_path / "page.png"
    Image.new("L", (10, 10), 0).save(img_path)

    with pytest.raises(OSError):
        drawbox_core.print_image(str(img_path))

    assert not img_path.exists()


def test_print_image_override_beats_saved_type(drawbox_dir, monkeypatch, tmp_path):
    drawbox_core.save_settings({"printer_type": "cups"})
    calls = []
    monkeypatch.setattr(drawbox_escpos, "start_print",
                        lambda path, port, baud: calls.append((path, port, baud)))
    img_path = tmp_path / "page.png"
    Image.new("L", (10, 10), 0).save(img_path)

    drawbox_core.print_image(str(img_path), printer_type="escpos_serial")

    assert len(calls) == 1
    assert calls[0][1] == "/dev/ttyUSB0"
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


def test_bogus_printer_type_normalizes_to_cups(drawbox_dir, monkeypatch, tmp_path):
    drawbox_core.save_settings({"printer_type": "bogus"})
    assert drawbox_core.load_settings()["printer_type"] == "cups"

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
