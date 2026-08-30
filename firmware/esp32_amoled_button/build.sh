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
git -C "$CACHE" -c advice.detachedHead=false checkout --quiet "$LIBS_SHA"

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
