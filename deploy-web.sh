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
scp "$DIR/drawbox_core.py" \
    "$DIR/drawbox_web.py" \
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
pi ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart drawbox, /usr/bin/systemctl stop drawbox, /usr/bin/systemctl start drawbox, /usr/bin/systemctl restart drawbox-web, /usr/bin/systemctl stop drawbox-web, /usr/sbin/reboot, /usr/bin/nmcli dev wifi connect *
SUDOERS
sudo chmod 440 /etc/sudoers.d/drawbox-web
echo "   ✅ Sudoers configured"

# Seed API keys file from existing service env vars (one-time migration)
mkdir -p ~/.drawbox
if [ ! -f ~/.drawbox/api_keys.json ]; then
    echo "   Migrating API keys to ~/.drawbox/api_keys.json..."
    OPENAI_KEY=$(grep -oP 'OPENAI_API_KEY=\K.*' /etc/systemd/system/drawbox.service 2>/dev/null || true)
    [ -z "$OPENAI_KEY" ] && OPENAI_KEY=$(grep -oP 'OPENAI_API_KEY=\K.*' /etc/systemd/system/drawbox-web.service 2>/dev/null || true)
    REP_KEY=$(grep -oP 'REPLICATE_API_TOKEN=\K.*' /etc/systemd/system/drawbox.service 2>/dev/null || true)
    [ -z "$REP_KEY" ] && REP_KEY=$(grep -oP 'REPLICATE_API_TOKEN=\K.*' /etc/systemd/system/drawbox-web.service 2>/dev/null || true)
    GEM_KEY=$(grep -oP 'GEMINI_API_KEY=\K.*' /etc/systemd/system/drawbox.service 2>/dev/null || true)
    [ -z "$GEM_KEY" ] && GEM_KEY=$(grep -oP 'GEMINI_API_KEY=\K.*' /etc/systemd/system/drawbox-web.service 2>/dev/null || true)
    python3 -c "
import json
keys = {}
o, r, g = '''$OPENAI_KEY''', '''$REP_KEY''', '''$GEM_KEY'''
if o: keys['openai'] = o
if r: keys['replicate'] = r
if g: keys['gemini'] = g
open('/home/pi/.drawbox/api_keys.json', 'w').write(json.dumps(keys, indent=2))
"
    if [ -s ~/.drawbox/api_keys.json ]; then
        echo "   ✅ API keys migrated (manage them from Settings > API Keys in the dashboard)"
    else
        echo "   ⚠️  No API keys found. Add them from the dashboard: Settings > API Keys"
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
