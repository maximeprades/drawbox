#!/bin/bash
# DrawBox Web Dashboard — one-command deploy from your Mac
# Usage: ./deploy-web.sh [pi-host] [api-key]
#   pi-host  = Pi address (default: pi@drawbox.local)
#   api-key  = OpenAI API key (default: reads from Pi's ~/.bashrc)

set -e

PI="${1:-pi@drawbox.local}"
API_KEY="${2:-}"
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
ssh "$PI" bash -s "$API_KEY" << 'REMOTE'
set -e
API_KEY="$1"

# Install Flask if missing
python3 -c "import flask" 2>/dev/null || {
    echo "   Installing Flask..."
    sudo pip3 install --break-system-packages flask
}
echo "   ✅ Flask ready"

# Sudoers for service control (idempotent)
sudo tee /etc/sudoers.d/drawbox-web > /dev/null << 'SUDOERS'
pi ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart drawbox, /usr/bin/systemctl stop drawbox, /usr/bin/systemctl start drawbox, /usr/bin/systemctl restart drawbox-web, /usr/bin/systemctl stop drawbox-web, /usr/sbin/reboot
SUDOERS
sudo chmod 440 /etc/sudoers.d/drawbox-web
echo "   ✅ Sudoers configured"

# Figure out the API key
if [ -z "$API_KEY" ]; then
    # Try to read from existing drawbox.service
    API_KEY=$(grep -oP 'OPENAI_API_KEY=\K.*' /etc/systemd/system/drawbox.service 2>/dev/null || true)
fi
if [ -z "$API_KEY" ]; then
    # Try ~/.bashrc
    API_KEY=$(grep -oP 'OPENAI_API_KEY="\K[^"]+' ~/.bashrc 2>/dev/null || true)
fi
if [ -z "$API_KEY" ]; then
    echo "   ⚠️  No API key found! Edit the service file later:"
    echo "      sudo nano /etc/systemd/system/drawbox-web.service"
    API_KEY="sk-your-actual-key-here"
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
Environment=OPENAI_API_KEY=$API_KEY
WorkingDirectory=/home/pi
ExecStart=/usr/bin/python3 /home/pi/drawbox_web.py
Restart=on-failure
RestartSec=10

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
