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
# drawbox.py ships together with the web files: pairing (button + spoken
# code) spans both services, so partial deploys could lock you out.
echo "📦 Copying files to Pi..."
# Glob the python modules so a new drawbox_*.py can never be left behind
# (a stale copy list bricked deployed boxes when drawbox_escpos.py landed).
scp "$DIR"/drawbox*.py \
    "$DIR/drawbox-guide.html" \
    "$DIR/drawbox-simulator.html" \
    "$DIR/check.sh" \
    "$PI":~/
ssh "$PI" "mkdir -p ~/templates"
scp "$DIR/templates/index.html" "$PI":~/templates/
echo "   ✅ Files copied"

# ── 2. Install Flask + set up service (all in one SSH) ──
echo "🔧 Setting up on Pi..."
ssh "$PI" bash -s << 'REMOTE'
set -e

# Install Flask + gunicorn if missing
python3 -c "import flask" 2>/dev/null || {
    echo "   Installing Flask..."
    sudo pip3 install --break-system-packages flask
}
python3 -c "import gunicorn" 2>/dev/null || {
    echo "   Installing gunicorn..."
    sudo pip3 install --break-system-packages gunicorn
}
echo "   ✅ Flask + gunicorn ready"

# Clone repo for software updates (skip if already exists)
if [ ! -d ~/drawbox-repo ]; then
    echo "   Cloning repo for software updates..."
    if git clone https://github.com/maximeprades/drawbox.git ~/drawbox-repo 2>&1; then
        echo "   ✅ Repo cloned to ~/drawbox-repo"
    else
        echo "   ⚠️  Could not clone repo (no git or no network). Software Update will not work."
        echo "   You can clone manually later: git clone https://github.com/maximeprades/drawbox.git ~/drawbox-repo"
    fi
else
    echo "   ✅ Repo already exists at ~/drawbox-repo"
fi

# Sudoers for service control (idempotent)
sudo tee /etc/sudoers.d/drawbox-web > /dev/null << 'SUDOERS'
pi ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart drawbox, /usr/bin/systemctl stop drawbox, /usr/bin/systemctl start drawbox, /usr/bin/systemctl restart drawbox-web, /usr/bin/systemctl stop drawbox-web, /usr/sbin/reboot, /usr/bin/nmcli dev wifi connect *, /usr/bin/nmcli con add *, /usr/bin/nmcli con modify *, /usr/bin/nmcli con delete *
SUDOERS
sudo chmod 440 /etc/sudoers.d/drawbox-web
echo "   ✅ Sudoers configured"

# Seed API keys file from existing service env vars (one-time migration)
mkdir -p ~/.drawbox
chmod 700 ~/.drawbox
if [ ! -f ~/.drawbox/api_keys.json ]; then
    echo "   Migrating API keys to ~/.drawbox/api_keys.json..."
    GATEWAY_KEY=$(grep -oP 'AI_GATEWAY_API_KEY=\K.*' /etc/systemd/system/drawbox.service 2>/dev/null || true)
    [ -z "$GATEWAY_KEY" ] && GATEWAY_KEY=$(grep -oP 'AI_GATEWAY_API_KEY=\K.*' /etc/systemd/system/drawbox-web.service 2>/dev/null || true)
    # Pass keys via env vars (NOT shell interpolation into the source) so
    # values containing quotes or shell metacharacters can't break the script.
    GATEWAY_KEY="$GATEWAY_KEY" python3 - <<'PYTHON'
import json, os
keys = {}
v = os.environ.get("GATEWAY_KEY", "").strip()
if v:
    keys["ai_gateway"] = v
with open(os.path.expanduser("~/.drawbox/api_keys.json"), "w") as f:
    json.dump(keys, f, indent=2)
PYTHON
    chmod 600 ~/.drawbox/api_keys.json
    if [ -s ~/.drawbox/api_keys.json ] && [ "$(cat ~/.drawbox/api_keys.json)" != "{}" ]; then
        echo "   ✅ API key migrated (manage it from Settings > API Keys in the dashboard)"
    else
        echo "   ⚠️  No AI Gateway key found. Add it from the dashboard: Settings > API Keys"
    fi
else
    echo "   ✅ API keys file already exists"
fi

# Create systemd service (keys are loaded from ~/.drawbox/api_keys.json by the app)
sudo tee /etc/systemd/system/drawbox-web.service > /dev/null << 'EOF'
[Unit]
Description=DrawBox Web Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
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
# Restart the button service too so voice pairing matches the deployed web code
sudo systemctl restart drawbox 2>/dev/null || true
echo "   ✅ Services restarted"

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
