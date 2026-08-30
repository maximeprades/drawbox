# ATOM Printer bridge (WiFi + USB)

The one firmware for the M5Stack ATOM Printer (SKU K118) in DrawBox. The
stock factory firmware runs a WiFi AP + MQTT stack and never exposes the
print head over USB, so it must be replaced with this sketch (restore stock
anytime with [M5Burner](https://docs.m5stack.com/en/guide/hobby_kit/atom_printer/usage)).

With a `wifi_credentials.h` present, the sketch joins your WLAN, announces
itself over mDNS as `drawbox-atom.local`, and forwards one raw TCP
connection on port 9100 to the print head, so the Pi prints with no cable.
Without the header it compiles as a plain USB-serial-to-head pipe
(`escpos_serial` over the USB cable) and nothing more.

The status LED shows what it is doing: red = no WiFi (or USB-only mode),
green = WiFi up, blue = a client is printing.

## Setup

1. Copy `wifi_credentials.h.example` to `wifi_credentials.h` and fill in
   your SSID and password. The file is gitignored — never commit it. The
   network must be 2.4 GHz; the ESP32 has no 5 GHz radio. (Skip this step
   entirely for a USB-only bridge.)
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

3. On boot the sketch prints its status over USB serial at 9600: the IP
   and mDNS name on success, disconnect reason codes and a network scan if
   it cannot join.

## DrawBox settings

In the dashboard Settings, set printer type to **M5Stack ATOM thermal —
WiFi**, host `drawbox-atom.local` (or the IP), port `9100`. For the USB
cable path instead, pick the USB serial printer type and set the serial
port.

## Notes

- The head still needs its DC 12V ≥2.5A supply — on USB power alone the
  output is faint or blank.
- One print job at a time; a second connection waits until the first
  closes. A client that goes quiet for 60 s is dropped so a crashed peer
  cannot wedge the printer.
- Port 9100 is unauthenticated, as raw printing always is: anyone on your
  LAN can print. Fine for a home network; do not port-forward it.
- While a TCP client is connected, USB input is not forwarded to the head
  (jobs cannot interleave).
- Smoke test from any machine on the LAN, from the repo root:

```bash
python3 drawbox_escpos.py --host drawbox-atom.local
```
