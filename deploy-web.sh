#!/bin/bash
# DrawBox Web Dashboard — one-command deploy from your Mac
# Usage: ./deploy-web.sh [pi-host]
#   pi-host  = Pi address (default: pi@drawbox.local)

set -e

PI="${1:-pi@drawbox.local}"
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "🚀 Deploying DrawBox Web Dashboard to $PI"
echo "   From: $DIR"
echo ""

# ── 1. Copy files ────────────────────────────────
echo "📦 Copying files to Pi..."
scp "$DIR/drawbox_web.py" \
    "$DIR/drawbox-guide.html" \
    "$DIR/drawbox-simulator.html" \
    "$DIR/check.sh" \
    "$PI":~/
echo "   ✅ Files copied"

# ── 2. Install Flask + set up service (all in one SSH) ──
echo "🔧 Setting up on Pi..."
ssh "$PI" bash -s << 'REMOTE'
set -e

# Install Flask, gunicorn, replicate if missing
python3 -c "import flask" 2>/dev/null || {
    echo "   Installing Flask..."
    sudo pip3 install --break-system-packages flask
}
python3 -c "import gunicorn" 2>/dev/null || {
    echo "   Installing gunicorn..."
    sudo pip3 install --break-system-packages gunicorn
}
python3 -c "import replicate" 2>/dev/null || {
    echo "   Installing replicate..."
    sudo pip3 install --break-system-packages replicate
}
echo "   ✅ Flask + gunicorn + replicate ready"

# Sudoers for service control (idempotent)
sudo tee /etc/sudoers.d/drawbox-web > /dev/null << 'SUDOERS'
pi ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart drawbox, /usr/bin/systemctl stop drawbox, /usr/bin/systemctl start drawbox, /usr/bin/systemctl restart drawbox-web, /usr/bin/systemctl stop drawbox-web, /usr/sbin/reboot, /usr/bin/nmcli dev wifi connect *
SUDOERS
sudo chmod 440 /etc/sudoers.d/drawbox-web
echo "   ✅ Sudoers configured"

# Figure out the OpenAI API key
OPENAI_KEY=""
OPENAI_KEY=$(grep -oP 'OPENAI_API_KEY=\K.*' /etc/systemd/system/drawbox.service 2>/dev/null || true)
if [ -z "$OPENAI_KEY" ]; then
    OPENAI_KEY=$(grep -oP 'OPENAI_API_KEY="\K[^"]+' ~/.bashrc 2>/dev/null || true)
fi
if [ -z "$OPENAI_KEY" ]; then
    echo "   ⚠️  No OpenAI API key found! Edit the service file later:"
    echo "      sudo nano /etc/systemd/system/drawbox-web.service"
    OPENAI_KEY="sk-your-actual-key-here"
fi

# Figure out the Replicate API token
REP_KEY=""
REP_KEY=$(grep -oP 'REPLICATE_API_TOKEN=\K.*' /etc/systemd/system/drawbox.service 2>/dev/null || true)
if [ -z "$REP_KEY" ]; then
    REP_KEY=$(grep -oP 'REPLICATE_API_TOKEN="\K[^"]+' ~/.bashrc 2>/dev/null || true)
fi
if [ -z "$REP_KEY" ]; then
    REP_KEY="${REPLICATE_API_TOKEN:-}"
fi
if [ -z "$REP_KEY" ]; then
    echo "   ⚠️  No Replicate API token found! Edit the service file later:"
    echo "      sudo nano /etc/systemd/system/drawbox-web.service"
    REP_KEY="r8_your-token-here"
fi

# Figure out the Gemini API key
GEM_KEY=""
GEM_KEY=$(grep -oP 'GEMINI_API_KEY=\K.*' /etc/systemd/system/drawbox.service 2>/dev/null || true)
if [ -z "$GEM_KEY" ]; then
    GEM_KEY=$(grep -oP 'GEMINI_API_KEY="\K[^"]+' ~/.bashrc 2>/dev/null || true)
fi
if [ -z "$GEM_KEY" ]; then
    GEM_KEY="${GEMINI_API_KEY:-}"
fi
if [ -z "$GEM_KEY" ]; then
    echo "   ⚠️  No Gemini API key found (optional — needed for Nano Banana 2 model)"
    GEM_KEY=""
fi

# Create systemd service
sudo tee /etc/systemd/system/drawbox-web.service > /dev/null << EOF
[Unit]
Description=DrawBox Web Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
Environment=OPENAI_API_KEY=$OPENAI_KEY
Environment=REPLICATE_API_TOKEN=$REP_KEY
Environment=GEMINI_API_KEY=$GEM_KEY
WorkingDirectory=/home/pi
ExecStart=/usr/local/bin/gunicorn --bind 0.0.0.0:5000 --workers 2 --threads 4 --timeout 120 drawbox_web:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
echo "   ✅ Service file created"

# Enable and (re)start
sudo systemctl daemon-reload
sudo systemctl enable drawbox-web
sudo systemctl restart drawbox-web
echo "   ✅ Service started"

# Quick health check
sleep 2
if curl -s -o /dev/null -w "%{http_code}" http://localhost:5000 | grep -q 200; then
    echo ""
    echo "=========================================="
    echo "  🎉 Dashboard is live!"
    echo "  Open: http://drawbox.local:5000"
    echo "=========================================="
else
    echo ""
    echo "   ⚠️  Service started but not responding yet."
    echo "   Check: sudo systemctl status drawbox-web"
fi
REMOTE

echo ""
echo "Done! Open http://drawbox.local:5000 in your browser."
