# DrawBox voice button (ESP32-S3 AMOLED)

A one-button DrawBox remote for the Waveshare **ESP32-S3-Touch-AMOLED-2.16**
(480×480 touch AMOLED, dual mics). Tap the big button, say what you want
("draw me a dinosaur"), and the page prints. The box records a 16 kHz WAV
and POSTs it to the Pi dashboard's `/api/voice/generate`; Whisper, the
safety filter, image generation, and printing all stay on the Pi.

The screen walks through the same states the arcade-button box speaks:
idle button → "I'm listening!" with a progress ring → "Drawing..." →
result (with the transcript it heard) → back to idle.

## Setup

1. Copy `wifi_credentials.h.example` to `wifi_credentials.h` and fill in
   WiFi (2.4 GHz only), the DrawBox host, and a device token. The file is
   gitignored — never commit it.
2. Mint the token (physical route: press the arcade button, say
   "authorize"; or over SSH):

```bash
CODE=$(ssh pi@drawbox.local "cd /home/pi && python3 -c \
    'import drawbox_core; print(drawbox_core.open_pairing_window())'")
curl -X POST http://drawbox.local:5000/api/pair \
    -H 'Content-Type: application/json' \
    -d "{\"code\":\"$CODE\",\"name\":\"ESP32 voice button\"}"
```

3. Build and flash (needs `arduino-cli` with the `esp32:esp32` core):

```bash
./build.sh flash /dev/cu.usbmodemXXXX
```

`build.sh` fetches the display/touch/sensor libraries from Waveshare's own
board repo, pinned to a known commit, into the gitignored `.libs/`. The
ES7210 mic driver is vendored here (Espressif MIT), with one behavioral
change from the vendor example: gain goes to MIC1/MIC2 — the board's real
microphones per the schematic — not the MIC3/4 echo-cancellation loopback
the example boosts. With the example's gains the recordings are silence.

## Serial test hooks

The native USB port doubles as a console at 115200:

- `t` — simulate a button press (full record → upload → result cycle)
- `d` — dump the last recording as base64 WAV between marker lines
- `s` — one-line status (state, WiFi, IP, heap, PSRAM, last WAV size)

A full remote test from the Mac, no hands needed:

```bash
(sleep 3 && say "draw me a dinosaur") & \
    python3 dbx_serial.py /dev/cu.usbmodemXXXX 45 t 1
```

## Behavior notes

- The recording window follows the dashboard's `record_seconds` setting
  (fetched once at boot; defaults to 8 s if the fetch fails).
- Both mics are captured; the louder channel is kept, so covering one mic
  doesn't mute the box.
- Responses show the dashboard's own script lines ("Here it comes!",
  "Hmm, I can't draw that..."), so both boxes share one personality.
- One request at a time: if the other box is mid-generation the server
  answers busy and the screen says so.
