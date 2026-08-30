# ATOM Printer USB bridge

Turns the M5Stack ATOM Printer (SKU K118) into a dumb USB serial ESC/POS
printer for DrawBox. The stock firmware runs a WiFi AP + MQTT stack and never
exposes the print head over USB; this sketch just forwards bytes between the
USB serial port and the head.

For cable-free printing over the network, flash
`../atom_printer_bridge_wifi/` instead — it keeps this USB pipe and adds a
TCP one.

## Flashing

With the Arduino IDE:

1. Install the **esp32** boards package (Boards Manager).
2. Select board **M5Stack-ATOM** — the board id appears as `m5stack_atom` or
   `m5stack-atom` depending on the esp32 core version.
3. Pick the ATOM's serial port and hit Upload.

Or with arduino-cli (after `arduino-cli core install esp32:esp32`):

```bash
arduino-cli compile --fqbn esp32:esp32:m5stack_atom --upload -p /dev/ttyUSB0 firmware/atom_printer_bridge
```

## Notes

- The printer must be on its DC 12V ≥2.5A supply — on USB power alone the
  output is faint or blank.
- If nothing prints, swap `HEAD_RX`/`HEAD_TX` (33/23) and reflash.
- Restore the stock firmware anytime with
  [M5Burner](https://docs.m5stack.com/en/guide/hobby_kit/atom_printer/usage).

## Smoke test

```bash
python3 drawbox_escpos.py --port /dev/ttyUSB0
```

On macOS the port looks like `/dev/cu.usbserial-XXXX`.
