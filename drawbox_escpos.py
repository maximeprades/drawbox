#!/usr/bin/env python3
"""ESC/POS raster rendering + serial output for thermal receipt printers.

Drives the M5Stack ATOM Printer (SKU K118) — an ESP32-driven 58mm ESC/POS
serial thermal printer — and any compatible ESC/POS serial printer. The ATOM
ships with an AP/MQTT firmware that never exposes the print head over USB, so
it must be flashed once with the bridge sketch in
``firmware/atom_printer_bridge/`` (one command, see its README). After that it
behaves like a dumb USB serial printer.

Only the Python stdlib and Pillow are needed — no pyserial, no python-escpos.

Hardware smoke test (prints a built-in test pattern):

    python3 drawbox_escpos.py --port /dev/ttyUSB0
"""

import argparse
import fcntl
import logging
import os
import termios
import threading

from PIL import Image, ImageOps

log = logging.getLogger("drawbox.escpos")

PRINTER_WIDTH_DOTS = 384
BAND_ROWS = 255  # max rows per GS v 0 block
FEED_LINES = 4

INIT = b"\x1b\x40"
FEED_BYTES = b"\x0a" * FEED_LINES


def render_raster(img):
    """Render a PIL image into a complete ESC/POS print job (bytes)."""
    gray = img.convert("L")
    bbox = ImageOps.invert(gray).getbbox()
    if bbox is None:
        log.warning("image is entirely white — printing paper feed only")
        return INIT + FEED_BYTES
    gray = gray.crop(bbox)
    w, h = gray.size
    gray = gray.resize(
        (PRINTER_WIDTH_DOTS, max(1, round(h * PRINTER_WIDTH_DOTS / w))),
        Image.LANCZOS,
    )
    # PIL packs mode "1" with white=1; ESC/POS raster wants black=1.
    data = bytes(b ^ 0xFF for b in gray.convert("1").tobytes())
    row_bytes = PRINTER_WIDTH_DOTS // 8
    job = [INIT]
    for start in range(0, len(data), row_bytes * BAND_ROWS):
        band = data[start:start + row_bytes * BAND_ROWS]
        rows = len(band) // row_bytes
        # GS v 0: mode, then width in bytes and height in dots, little-endian.
        job.append(b"\x1d\x76\x30\x00" + bytes([
            row_bytes & 0xFF, row_bytes >> 8, rows & 0xFF, rows >> 8,
        ]))
        job.append(band)
    job.append(FEED_BYTES)
    return b"".join(job)


def _open_serial(port, baud):
    """Open ``port`` as a raw 8N1 serial fd at ``baud``, no flow control."""
    speed = getattr(termios, "B%d" % baud, None)
    if speed is None:
        raise ValueError("unsupported baud rate: %s" % baud)
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY)
    try:
        cc = termios.tcgetattr(fd)[6]
        iflag = oflag = lflag = 0
        cflag = termios.CS8 | termios.CLOCAL | termios.CREAD
        termios.tcsetattr(fd, termios.TCSANOW,
                          [iflag, oflag, cflag, lflag, speed, speed, cc])
    except Exception:
        os.close(fd)
        raise
    return fd


def _pump(fd, job):
    """Write ``job`` to ``fd`` and drain it. Always closes ``fd``."""
    try:
        # flock, not a threading lock: the button daemon and the web
        # dashboard print from separate processes, and interleaved writes
        # would corrupt the raster mid-job.
        fcntl.flock(fd, fcntl.LOCK_EX)
        view = memoryview(job)
        while view:
            view = view[os.write(fd, view):]
        termios.tcdrain(fd)
    finally:
        os.close(fd)  # also releases the flock


def _pump_logged(fd, job):
    """Thread body — no caller left to catch, so log."""
    try:
        _pump(fd, job)
    except Exception:
        log.error("serial print failed mid-write", exc_info=True)


def print_file(path, port, baud=9600):
    """Print the image at ``path`` over ``port``, blocking until the whole
    job is written. Returns the job byte count."""
    with Image.open(path) as img:
        job = render_raster(img)
    _pump(_open_serial(port, baud), job)
    return len(job)


def start_print(path, port, baud=9600):
    """Print the image at ``path`` over ``port`` from a background thread.

    Every predictable failure — missing or corrupt image, missing port,
    unsupported baud — raises here, before the thread starts; only
    mid-write I/O errors are log-only. Returns the job byte count.
    """
    with Image.open(path) as img:
        job = render_raster(img)
    fd = _open_serial(port, baud)
    threading.Thread(target=_pump_logged, args=(fd, job), daemon=True).start()
    return len(job)


def _test_pattern():
    """Solid bar + checkerboard + gradient — exercises solid fill, fine
    detail, and dithering on real hardware."""
    img = Image.new("L", (PRINTER_WIDTH_DOTS, 120), 255)
    img.paste(0, (0, 0, PRINTER_WIDTH_DOTS, 40))
    for y in range(40, 80):
        for x in range(PRINTER_WIDTH_DOTS):
            if (x // 8 + y // 8) % 2 == 0:
                img.putpixel((x, y), 0)
    for y in range(80, 120):
        for x in range(PRINTER_WIDTH_DOTS):
            img.putpixel((x, y), x * 255 // (PRINTER_WIDTH_DOTS - 1))
    return img


def _main():
    parser = argparse.ArgumentParser(
        description="Print an image (or a built-in test pattern) to an "
                    "ESC/POS serial thermal printer.")
    parser.add_argument("--port", required=True,
                        help="serial device, e.g. /dev/ttyUSB0 (Linux) or "
                             "/dev/cu.usbserial-XXXX (macOS)")
    parser.add_argument("--baud", type=int, default=9600)
    parser.add_argument("image", nargs="?",
                        help="image file to print (default: test pattern)")
    args = parser.parse_args()
    if args.image:
        sent = print_file(args.image, args.port, args.baud)
    else:
        job = render_raster(_test_pattern())
        _pump(_open_serial(args.port, args.baud), job)
        sent = len(job)
    print(f"sent {sent} bytes to {args.port}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    _main()
