#!/bin/bash
# DrawBox Health Check — verifies the entire Pi setup
# Run from your Mac:  ./check.sh
# Or on the Pi:       bash check.sh

set -u

# If running from Mac, SSH into the Pi and run there
if [ "$(uname)" = "Darwin" ]; then
    PI="${1:-pi@drawbox.local}"
    echo "🔍 Running DrawBox health check on $PI ..."
    echo ""
    SCRIPT="$(cd "$(dirname "$0")" && pwd)/check.sh"
    ssh "$PI" bash -s < "$SCRIPT"
    exit $?
fi

# ── Running on the Pi ────────────────────────────
PASS=0
FAIL=0
WARN=0

ok()   { echo "  ✅ $1"; PASS=$((PASS+1)); }
fail() { echo "  ❌ $1"; FAIL=$((FAIL+1)); }
warn() { echo "  ⚠️  $1"; WARN=$((WARN+1)); }

echo "╔══════════════════════════════════════════╗"
echo "║       DrawBox Health Check               ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── 1. INTERNET ─────────────────────────────────
echo "🌐 Internet"
if ping -c 1 -W 3 api.openai.com > /dev/null 2>&1; then
    ok "Internet connected (api.openai.com reachable)"
else
    fail "No internet — can't reach api.openai.com"
fi
echo ""

# ── 2. OPENAI API KEY ───────────────────────────
echo "🔑 OpenAI API Key"
# Check systemd service file
SVC_KEY=$(grep -oP 'OPENAI_API_KEY=\K.*' /etc/systemd/system/drawbox.service 2>/dev/null || true)
BASHRC_KEY=$(grep -oP 'OPENAI_API_KEY="\K[^"]+' ~/.bashrc 2>/dev/null || true)
ENV_KEY="${OPENAI_API_KEY:-}"

if [ -n "$SVC_KEY" ] && [ "$SVC_KEY" != "sk-your-actual-key-here" ]; then
    ok "API key found in drawbox.service (${SVC_KEY:0:8}...)"
elif [ -n "$BASHRC_KEY" ] && [ "$BASHRC_KEY" != "sk-your-key-here" ]; then
    ok "API key found in ~/.bashrc (${BASHRC_KEY:0:8}...)"
    warn "Key not in drawbox.service — systemd won't see it. Add Environment=OPENAI_API_KEY=... to the service file"
elif [ -n "$ENV_KEY" ]; then
    ok "API key found in environment (${ENV_KEY:0:8}...)"
else
    fail "No API key found. Set it in drawbox.service or ~/.bashrc"
fi

# Quick API test
KEY="${SVC_KEY:-${BASHRC_KEY:-${ENV_KEY:-}}}"
if [ -n "$KEY" ] && [ "$KEY" != "sk-your-actual-key-here" ] && [ "$KEY" != "sk-your-key-here" ]; then
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
        -H "Authorization: Bearer $KEY" \
        https://api.openai.com/v1/models 2>/dev/null || echo "000")
    if [ "$HTTP_CODE" = "200" ]; then
        ok "API key is valid (OpenAI API responds 200)"
    elif [ "$HTTP_CODE" = "401" ]; then
        fail "API key is INVALID (401 Unauthorized)"
    elif [ "$HTTP_CODE" = "000" ]; then
        warn "Couldn't test API key (network issue?)"
    else
        warn "API returned HTTP $HTTP_CODE"
    fi
fi
echo ""

# ── 2b. REPLICATE API TOKEN ─────────────────────
echo "🔑 Replicate API Token"
SVC_REP=$(grep -oP 'REPLICATE_API_TOKEN=\K.*' /etc/systemd/system/drawbox.service 2>/dev/null || true)
BASHRC_REP=$(grep -oP 'REPLICATE_API_TOKEN="\K[^"]+' ~/.bashrc 2>/dev/null || true)
ENV_REP="${REPLICATE_API_TOKEN:-}"

if [ -n "$SVC_REP" ] && [ "$SVC_REP" != "r8_your-token-here" ]; then
    ok "Replicate token found in drawbox.service (${SVC_REP:0:8}...)"
elif [ -n "$BASHRC_REP" ]; then
    ok "Replicate token found in ~/.bashrc (${BASHRC_REP:0:8}...)"
    warn "Token not in drawbox.service — systemd won't see it. Add Environment=REPLICATE_API_TOKEN=... to the service file"
elif [ -n "$ENV_REP" ]; then
    ok "Replicate token found in environment (${ENV_REP:0:8}...)"
else
    fail "No Replicate token found. Set it in drawbox.service or ~/.bashrc"
fi
echo ""

# ── 2c. GEMINI API KEY (optional) ─────────────────
echo "🔑 Gemini API Key (optional — for Nano Banana 2)"
SVC_GEM=$(grep -oP 'GEMINI_API_KEY=\K.*' /etc/systemd/system/drawbox.service 2>/dev/null || true)
BASHRC_GEM=$(grep -oP 'GEMINI_API_KEY="\K[^"]+' ~/.bashrc 2>/dev/null || true)
ENV_GEM="${GEMINI_API_KEY:-}"

if [ -n "$SVC_GEM" ] && [ "$SVC_GEM" != "" ]; then
    ok "Gemini key found in drawbox.service (${SVC_GEM:0:8}...)"
elif [ -n "$BASHRC_GEM" ]; then
    ok "Gemini key found in ~/.bashrc (${BASHRC_GEM:0:8}...)"
    warn "Key not in drawbox.service — systemd won't see it. Add Environment=GEMINI_API_KEY=... to the service file"
elif [ -n "$ENV_GEM" ]; then
    ok "Gemini key found in environment (${ENV_GEM:0:8}...)"
else
    warn "No Gemini key found (optional — only needed if IMAGE_MODEL=nano-banana)"
fi
echo ""

# ── 3. MICROPHONE ────────────────────────────────
echo "🎙️  Microphone"
if arecord -l 2>/dev/null | grep -qi "USB\|PnP\|CHANGE"; then
    CARD=$(arecord -l 2>/dev/null | grep -i "USB\|PnP\|CHANGE" | head -1)
    ok "USB mic detected: $CARD"
else
    fail "No USB microphone found. Run: arecord -l"
fi

# Test recording
if command -v arecord > /dev/null; then
    if timeout 2 arecord -d 1 -f S16_LE -r 44100 /tmp/drawbox_test.wav > /dev/null 2>&1; then
        ok "Recording works (44100Hz)"
        rm -f /tmp/drawbox_test.wav
    else
        # Try 16000Hz as fallback
        if timeout 2 arecord -d 1 -f S16_LE -r 16000 /tmp/drawbox_test.wav > /dev/null 2>&1; then
            warn "Recording works at 16000Hz but script uses 44100Hz. Check SAMPLE_RATE in drawbox.py"
            rm -f /tmp/drawbox_test.wav
        else
            fail "Cannot record audio. Check ~/.asoundrc and arecord -l"
        fi
    fi
fi
echo ""

# ── 4. SPEAKER ───────────────────────────────────
echo "🔊 Speaker"

# Detect USB speaker and its current card number
SPEAKER_LINE=$(aplay -l 2>/dev/null | grep -i "USB\|Speaker" | head -1)
SPEAKER_CARD=""
if [ -n "$SPEAKER_LINE" ]; then
    SPEAKER_CARD=$(echo "$SPEAKER_LINE" | grep -oP 'card \K\d+' || true)
    ok "USB speaker detected on card $SPEAKER_CARD: $SPEAKER_LINE"
else
    fail "No USB speaker found. Check: aplay -l"
fi

# Check ~/.asoundrc and verify its card number matches
if [ -f ~/.asoundrc ]; then
    ASOUND_CARD=$(grep -oP 'defaults\.pcm\.card\s+\K\d+' ~/.asoundrc 2>/dev/null || true)
    if [ -n "$ASOUND_CARD" ] && [ -n "$SPEAKER_CARD" ]; then
        if [ "$ASOUND_CARD" != "$SPEAKER_CARD" ]; then
            fail "~/.asoundrc says card $ASOUND_CARD but speaker is on card $SPEAKER_CARD — audio is going nowhere!"
            echo "    Fix: printf 'defaults.pcm.card $SPEAKER_CARD\ndefaults.ctl.card $SPEAKER_CARD\n' > ~/.asoundrc"
        else
            ok "~/.asoundrc card $ASOUND_CARD matches speaker"
        fi
    else
        ok "~/.asoundrc exists (audio routing configured)"
    fi
else
    warn "No ~/.asoundrc — audio may play through wrong device. See Step 12 in the guide."
    if [ -n "$SPEAKER_CARD" ]; then
        echo "    Fix: printf 'defaults.pcm.card $SPEAKER_CARD\ndefaults.ctl.card $SPEAKER_CARD\n' > ~/.asoundrc"
    fi
fi

# Check for PipeWire/PulseAudio conflict (grabs ALSA devices away from mpg123)
if pactl info 2>/dev/null | grep -qi "pipewire\|PulseAudio"; then
    warn "PipeWire/PulseAudio is running — may block ALSA access by mpg123. If speaker is silent, try: systemctl --user stop pipewire pipewire-pulse pulseaudio 2>/dev/null; true"
fi

# Actually play a test tone to verify audio comes out
if [ -n "$SPEAKER_CARD" ]; then
    if timeout 3 speaker-test -D plughw:${SPEAKER_CARD},0 -c 1 -t sine -f 440 -l 1 > /dev/null 2>&1; then
        ok "Speaker playback test passed (440Hz tone on card $SPEAKER_CARD)"
    else
        fail "Speaker playback test FAILED on card $SPEAKER_CARD. Manual test: speaker-test -D plughw:${SPEAKER_CARD},0 -c 1 -t sine"
    fi
fi
echo ""

# ── 5. PRINTER ───────────────────────────────────
echo "🖨️  Printer"
PRINTER_TYPE=$(python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.drawbox/web_settings.json'))).get('printer_type') or 'cups')" 2>/dev/null || echo cups)

if [ "$PRINTER_TYPE" = "escpos_serial" ]; then
    SERIAL_PORT=$(python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.drawbox/web_settings.json'))).get('serial_port') or '/dev/ttyUSB0')" 2>/dev/null || echo /dev/ttyUSB0)
    if [ -e "$SERIAL_PORT" ]; then
        ok "Thermal printer port $SERIAL_PORT present"
    else
        fail "Thermal printer port $SERIAL_PORT not found. Check the USB cable and that the bridge firmware is flashed (firmware/atom_printer_bridge)"
    fi
else
    if lpstat -p drawbox-printer > /dev/null 2>&1; then
        STATUS=$(lpstat -p drawbox-printer 2>/dev/null | head -1)
        ok "drawbox-printer configured: $STATUS"
    else
        fail "Printer 'drawbox-printer' not found. Run: lpstat -p -d"
    fi

    if lsusb 2>/dev/null | grep -qi "Brother"; then
        ok "Brother printer connected via USB"
    else
        warn "Brother printer not seen on USB. It may be asleep or disconnected."
    fi
fi
echo ""

# ── 6. GPIO / BUTTON ────────────────────────────
echo "🔴 Button & GPIO"
if python3 -c "from gpiozero import Button" 2>/dev/null; then
    ok "gpiozero installed and importable"
else
    fail "gpiozero not working. Run: pip3 install --break-system-packages gpiozero"
fi
echo ""

# ── 7. PYTHON DEPENDENCIES ──────────────────────
echo "📦 Python Dependencies"
DEPS=("openai" "replicate" "sounddevice" "soundfile" "numpy" "PIL" "gpiozero")
DEP_NAMES=("openai" "replicate" "sounddevice" "soundfile" "numpy" "Pillow" "gpiozero")
for i in "${!DEPS[@]}"; do
    if python3 -c "import ${DEPS[$i]}" 2>/dev/null; then
        ok "${DEP_NAMES[$i]}"
    else
        fail "${DEP_NAMES[$i]} not installed"
    fi
done

# mpg123 for TTS playback
if command -v mpg123 > /dev/null; then
    ok "mpg123 (TTS audio player)"
else
    fail "mpg123 not installed. Run: sudo apt install mpg123"
fi
echo ""

# ── 8. DRAWBOX SCRIPT ───────────────────────────
echo "📝 DrawBox Script"
if [ -f ~/drawbox.py ]; then
    ok "~/drawbox.py exists"

    # Check for gpiozero (not RPi.GPIO)
    if grep -q "from gpiozero import" ~/drawbox.py; then
        ok "Uses gpiozero (correct for Pi 5)"
    elif grep -q "RPi.GPIO" ~/drawbox.py; then
        fail "Still uses RPi.GPIO — this doesn't work on Pi 5! Update to gpiozero"
    else
        warn "Can't determine GPIO library used"
    fi

    # Check sample rate
    if grep -q "SAMPLE_RATE = 44100" ~/drawbox.py; then
        ok "SAMPLE_RATE = 44100 (correct for CHANGEEK mic)"
    elif grep -q "SAMPLE_RATE = 16000" ~/drawbox.py; then
        fail "SAMPLE_RATE = 16000 — should be 44100 for CHANGEEK mic"
    fi

    # Check for continue-in-try/finally bug
    if python3 -c "
import ast, sys
with open('$HOME/drawbox.py') as f:
    tree = ast.parse(f.read())
for node in ast.walk(tree):
    if isinstance(node, ast.Try) and node.finalbody:
        for child in ast.walk(node):
            if isinstance(child, ast.Continue):
                print('FOUND')
                sys.exit(0)
" 2>/dev/null | grep -q "FOUND"; then
        fail "Has 'continue' inside try/finally — is_busy will get stuck! Use if/elif/else chain"
    else
        ok "No continue-in-try/finally bug"
    fi

    # Check for VoiceFeedback class
    if grep -q "class VoiceFeedback" ~/drawbox.py; then
        ok "VoiceFeedback class present"
    else
        warn "VoiceFeedback class not found — using older version?"
    fi

    # Check for safety blocklist
    if grep -q "BLOCKED_WORDS" ~/drawbox.py; then
        ok "Safety blocklist present"
    else
        warn "No BLOCKED_WORDS found — safety filter missing"
    fi
else
    fail "~/drawbox.py not found!"
fi
echo ""

# ── 9. SYSTEMD SERVICES ─────────────────────────
echo "⚙️  Services"
if systemctl is-active drawbox > /dev/null 2>&1; then
    ok "drawbox.service is running"
else
    STATUS=$(systemctl is-active drawbox 2>/dev/null || echo "not found")
    warn "drawbox.service is $STATUS"
fi

if systemctl is-enabled drawbox > /dev/null 2>&1; then
    ok "drawbox.service enabled (starts on boot)"
else
    warn "drawbox.service not enabled — won't start on boot"
fi

if [ -f /etc/systemd/system/drawbox-web.service ]; then
    if systemctl is-active drawbox-web > /dev/null 2>&1; then
        ok "drawbox-web.service is running"
    else
        warn "drawbox-web.service exists but not running"
    fi
else
    warn "drawbox-web.service not installed (web dashboard not deployed)"
fi

# Cloudflare Tunnel
if command -v cloudflared > /dev/null 2>&1; then
    if systemctl is-active cloudflared > /dev/null 2>&1; then
        ok "cloudflared tunnel is running"
    else
        warn "cloudflared installed but service not running"
    fi
else
    warn "cloudflared not installed (optional — for remote access)"
fi

# SSH
if systemctl is-enabled ssh.service > /dev/null 2>&1 && systemctl is-enabled ssh.socket > /dev/null 2>&1; then
    ok "SSH service and socket both enabled"
else
    warn "SSH may die after reboot. Run: sudo systemctl enable ssh.socket && sudo systemctl enable ssh.service"
fi
echo ""

# ── 10. VOICE CACHE ─────────────────────────────
echo "🔊 Voice Cache"
CACHE_DIR="$HOME/.drawbox/voice_cache"
if [ -d "$CACHE_DIR" ]; then
    COUNT=$(find "$CACHE_DIR" -name "*.mp3" | wc -l)
    SIZE=$(du -sh "$CACHE_DIR" 2>/dev/null | cut -f1)
    ok "Voice cache exists: $COUNT files ($SIZE)"
else
    warn "No voice cache yet — will be created on first run"
fi
echo ""

# ── SUMMARY ──────────────────────────────────────
echo "══════════════════════════════════════════"
echo "  Results: ✅ $PASS passed  ❌ $FAIL failed  ⚠️  $WARN warnings"

if [ $FAIL -eq 0 ]; then
    echo "  🎉 DrawBox looks good!"
else
    echo "  🔧 Fix the failures above, then run this check again."
fi
echo "══════════════════════════════════════════"

exit $FAIL
