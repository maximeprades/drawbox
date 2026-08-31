#!/bin/bash
# Build (and optionally flash) the DrawBox voice button firmware.
#
#   ./build.sh              compile only
#   ./build.sh flash        compile + upload (default port below)
#   ./build.sh flash /dev/cu.usbmodemXXXX
#
# Display, touch, and sensor libraries come from Waveshare's own board
# repo, pinned to the commit these sources were written against.
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
LIBS_REPO="https://github.com/waveshareteam/ESP32-S3-Touch-AMOLED-2.16"
LIBS_SHA="225a62bff11b5d0a0b607873860d39485a9a9685"
CACHE="$DIR/.libs/ws216"
# ESP32-S3R8: octal PSRAM; module flash is 16 MB. CDC on boot gives the
# serial console over the same native USB port used for flashing.
FQBN="esp32:esp32:esp32s3:PSRAM=opi,FlashSize=16M,PartitionScheme=app3M_fat9M_16MB,CDCOnBoot=cdc"
PORT="${2:-/dev/cu.usbmodem1101}"

if [ ! -f "$DIR/wifi_credentials.h" ]; then
    echo "wifi_credentials.h missing — copy wifi_credentials.h.example and fill it in." >&2
    exit 1
fi

if [ ! -d "$CACHE/.git" ]; then
    git clone --quiet "$LIBS_REPO" "$CACHE"
fi
git -C "$CACHE" checkout --quiet -- .
git -C "$CACHE" -c advice.detachedHead=false checkout --quiet "$LIBS_SHA"
# Our lv_conf overlay: vendor's config plus LV_USE_SNAPSHOT for the
# serial screenshot hook.
cp "$DIR/lv_conf.h" "$CACHE/examples/arduino/libraries/lv_conf.h"

# Conversation-mode spike ('w' serial hook) needs a websocket client.
# Best-effort: the sketch builds without it (__has_include guard).
if ! arduino-cli lib list 2>/dev/null | grep -q "ArduinoWebsockets"; then
    arduino-cli lib install "ArduinoWebsockets@0.5.4" || \
        echo "ArduinoWebsockets install failed — spike hook will be a no-op" >&2
fi

# The face bitmaps are generated, not committed (4+ MB of hex).
if [ ! -f "$DIR/face_assets.h" ] || [ "$DIR/gen_face_assets.py" -nt "$DIR/face_assets.h" ]; then
    echo "generating face assets..."
    python3 "$DIR/gen_face_assets.py" || {
        echo "asset generation failed — needs python3 with Pillow" >&2
        exit 1
    }
fi

case "${1:-build}" in
    build)
        arduino-cli compile --fqbn "$FQBN" \
            --libraries "$CACHE/examples/arduino/libraries" "$DIR"
        ;;
    flash)
        arduino-cli compile --fqbn "$FQBN" \
            --libraries "$CACHE/examples/arduino/libraries" \
            --upload -p "$PORT" "$DIR"
        ;;
    *)
        echo "usage: $0 [build|flash] [port]" >&2
        exit 1
        ;;
esac
