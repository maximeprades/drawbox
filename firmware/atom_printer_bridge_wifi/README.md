# ATOM Printer WiFi bridge

Turns the M5Stack ATOM Printer (SKU K118) into a network ESC/POS printer for
DrawBox. The sketch joins your WiFi and forwards one raw TCP connection on
port 9100 to the print head, so the Pi prints to it with no cable. The USB
serial pipe from `atom_printer_bridge` keeps working as a fallback.

The status LED shows what it is doing: red = no WiFi, green = WiFi up,
blue = a client is printing.

## Setup

1. Copy `wifi_credentials.h.example` to `wifi_credentials.h` and fill in your
   SSID and password. The file is gitignored — never commit it. The network
   must be 2.4 GHz; the ESP32 has no 5 GHz radio.
2. Flash (after `arduino-cli core install esp32:esp32`):

```bash
arduino-cli compile --fqbn esp32:esp32:m5stack_atom --upload \
    -p /dev/cu.usbserial-XXXX firmware/atom_printer_bridge_wifi
```

If the upload dies switching to 1500000 baud (some ATOM units carry an FTDI
clone that cannot do it), pin the speed:

```bash
arduino-cli compile --fqbn esp32:esp32:m5stack_atom:UploadSpeed=115200 --upload \
    -p /dev/cu.usbserial-XXXX firmware/atom_printer_bridge_wifi
```

3. On boot the sketch prints its IP over USB serial at 9600, and announces
   itself over mDNS as `drawbox-atom.local`.

## DrawBox settings

In the dashboard Settings, set printer type to **Thermal receipt over WiFi**,
host `drawbox-atom.local` (or the IP), port `9100`.

## Notes

- The head still needs its DC 12V ≥2.5A supply — WiFi does not power the
  motor. On USB power alone the output is faint or blank.
- One print job at a time; a second connection waits until the first closes.
- Smoke test from any machine on the LAN:

```bash
python3 drawbox_escpos.py --host drawbox-atom.local
```
