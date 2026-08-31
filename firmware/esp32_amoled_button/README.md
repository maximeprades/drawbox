# DrawBox voice button (ESP32-S3 AMOLED)

A one-button DrawBox remote for the Waveshare **ESP32-S3-Touch-AMOLED-2.16**
(480×480 touch AMOLED, dual mics). Tap the big button, say what you want
("draw me a dinosaur"), and the page prints. The box records a 16 kHz WAV
and POSTs it to the Pi dashboard's `/api/voice/generate`; Whisper, the
safety filter, image generation, and printing all stay on the Pi.

The screen is a big emoji buddy rendered with LVGL 8.4: it blinks and
glances around while idle, goes wide-eyed with its mouth moving to your
voice while listening (progress ring, rippling sound waves), naps under
a spinner while the Pi draws, and shows a joyful or deadpan face with
the transcript for the result. The face itself is pre-rendered on the
host by `gen_face_assets.py` (Pillow, 3x supersampled) into anti-aliased
bitmap frames baked into flash — nothing hand-drawn at runtime, so there
are no vector artifacts. `build.sh` regenerates `face_assets.h` when the
generator changes; the header is gitignored (4+ MB of hex).

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

`build.sh` fetches the display/touch/sensor/LVGL libraries from
Waveshare's own board repo, pinned to a known commit, into the gitignored
`.libs/`, then overlays our `lv_conf.h` (vendor config plus
`LV_USE_SNAPSHOT` for the screenshot hook). Asset generation needs
`python3` with Pillow. The ES7210 mic driver is vendored here (Espressif
MIT), with one behavioral change from the vendor example: gain goes to
MIC1/MIC2 — the board's real microphones per the schematic — not the
MIC3/4 echo-cancellation loopback the example boosts. With the example's
gains the recordings are silence.

## Serial test hooks

The native USB port doubles as a console at 115200:

- `t` — simulate a button press (full record → upload → result cycle)
- `d` — dump the last recording as base64 WAV between marker lines
- `p` — dump a screenshot of the live UI as base64 RGB565 (little-endian)
- `b` — speaker loopback self-test (plays a tone, reports the mic peak)
- `s` — one-line status (state, WiFi, IP, heap, PSRAM, last WAV size)

A full remote test from the Mac, no hands needed:

```bash
(sleep 3 && say "draw me a dinosaur") & \
    python3 dbx_serial.py /dev/cu.usbmodemXXXX 45 t 1
```

## Sound

The onboard ES8311 codec drives the little speaker on the same I2S bus
as the mics (full duplex). After WiFi comes up, the box fetches the
dashboard voice catalog from `GET /api/voice/lines` and prefetches each
line as 16 kHz mono WAV from `GET /api/voice/line?key=&i=`. Clips live
in PSRAM for that boot. The spoken lines and jokes are the same ones the
Pi arcade box uses, same voice included.

State changes play the matching line (`ready`, `listening`, `thinking`,
`printing`, or the `voice_key` from `/api/voice/generate`). While the
generate request is in flight, the box tells one joke after about two
seconds if the catalog has any. Synthesized chirps stay as the fallback
when a line is missing from the cache.

The listen line finishes before the mic opens, so recordings never
contain it. Upstream quirk worth knowing: the DAC power-up hides inside
`es8311_microphone_config`, so that call is required even though we
never use the ES8311's own mic path.

## Behavior notes

- The recording window follows the dashboard's `record_seconds` setting
  (fetched once at boot; defaults to 8 s if the fetch fails).
- Both mics are captured; the louder channel is kept, so covering one mic
  doesn't mute the box.
- Responses show the dashboard's own script lines ("Here it comes!",
  "Hmm, I can't draw that..."), so both boxes share one personality.
- One request at a time: if the other box is mid-generation the server
  answers busy and the screen says so.
