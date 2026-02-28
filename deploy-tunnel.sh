#!/bin/bash
# DrawBox Cloudflare Tunnel Setup — run from your Mac
# Usage: ./deploy-tunnel.sh <pi-host> <tunnel-name> <domain>
#
# Example:
#   ./deploy-tunnel.sh pi@drawbox.local kitchen drawbox.example.com
#
# This creates:
#   kitchen.drawbox.example.com     → Pi's Flask dashboard (port 5000)
#   kitchen-ssh.drawbox.example.com → Pi's SSH (port 22)
#
# Prerequisites:
#   - cloudflared CLI installed on your Mac (brew install cloudflared)
#   - Logged in to Cloudflare (cloudflared tunnel login)
#   - Domain added to Cloudflare

set -e

if [ $# -lt 3 ]; then
    echo "Usage: ./deploy-tunnel.sh <pi-host> <tunnel-name> <domain>"
    echo ""
    echo "  pi-host      SSH destination (e.g., pi@drawbox.local)"
    echo "  tunnel-name  Short name for this Pi (e.g., kitchen, bedroom)"
    echo "  domain       Your Cloudflare domain (e.g., drawbox.example.com)"
    echo ""
    echo "Example:"
    echo "  ./deploy-tunnel.sh pi@drawbox.local kitchen drawbox.example.com"
    exit 1
fi

PI="$1"
NAME="$2"
DOMAIN="$3"
TUNNEL_NAME="drawbox-${NAME}"
DASHBOARD_HOST="${NAME}.${DOMAIN}"
SSH_HOST="${NAME}-ssh.${DOMAIN}"

echo ""
echo "=========================================="
echo "  DrawBox Cloudflare Tunnel Setup"
echo "=========================================="
echo "  Pi:        $PI"
echo "  Tunnel:    $TUNNEL_NAME"
echo "  Dashboard: https://$DASHBOARD_HOST"
echo "  SSH:       https://$SSH_HOST"
echo "=========================================="
echo ""

# ── 1. Create tunnel (on Mac) ─────────────────
echo "1. Creating Cloudflare tunnel: $TUNNEL_NAME"
if cloudflared tunnel list | grep -q "$TUNNEL_NAME"; then
    echo "   Tunnel '$TUNNEL_NAME' already exists, reusing it."
else
    cloudflared tunnel create "$TUNNEL_NAME"
    echo "   ✅ Tunnel created"
fi

# Get tunnel ID and credentials path
TUNNEL_ID=$(cloudflared tunnel list --output json | python3 -c "
import json,sys
for t in json.load(sys.stdin):
    if t['name'] == '$TUNNEL_NAME':
        print(t['id']); break
")
CRED_FILE="$HOME/.cloudflared/${TUNNEL_ID}.json"

if [ -z "$TUNNEL_ID" ]; then
    echo "   ❌ Could not find tunnel ID. Run: cloudflared tunnel list"
    exit 1
fi
echo "   Tunnel ID: $TUNNEL_ID"

# ── 2. Route DNS ──────────────────────────────
echo ""
echo "2. Setting up DNS routes"
cloudflared tunnel route dns "$TUNNEL_NAME" "$DASHBOARD_HOST" 2>/dev/null || echo "   (DNS for $DASHBOARD_HOST may already exist)"
cloudflared tunnel route dns "$TUNNEL_NAME" "$SSH_HOST" 2>/dev/null || echo "   (DNS for $SSH_HOST may already exist)"
echo "   ✅ DNS routes configured"

# ── 3. Install cloudflared on Pi ──────────────
echo ""
echo "3. Installing cloudflared on Pi..."
ssh "$PI" bash -s << 'INSTALL'
set -e
if command -v cloudflared > /dev/null 2>&1; then
    echo "   cloudflared already installed: $(cloudflared --version)"
else
    echo "   Downloading cloudflared for arm64..."
    curl -sL -o /tmp/cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb
    sudo dpkg -i /tmp/cloudflared.deb
    rm /tmp/cloudflared.deb
    echo "   ✅ cloudflared installed: $(cloudflared --version)"
fi
INSTALL

# ── 4. Copy credentials to Pi ────────────────
echo ""
echo "4. Copying tunnel credentials to Pi..."
ssh "$PI" "sudo mkdir -p /etc/cloudflared"
scp "$CRED_FILE" "${PI}:/tmp/tunnel-creds.json"
ssh "$PI" "sudo mv /tmp/tunnel-creds.json /etc/cloudflared/${TUNNEL_ID}.json && sudo chmod 600 /etc/cloudflared/${TUNNEL_ID}.json"
echo "   ✅ Credentials copied to /etc/cloudflared/"

# ── 5. Create config on Pi ───────────────────
echo ""
echo "5. Creating tunnel config on Pi..."
ssh "$PI" bash -s << EOF
set -e
sudo tee /etc/cloudflared/config.yml > /dev/null << 'YAML'
tunnel: ${TUNNEL_ID}
credentials-file: /etc/cloudflared/${TUNNEL_ID}.json

ingress:
  - hostname: ${DASHBOARD_HOST}
    service: http://localhost:5000
  - hostname: ${SSH_HOST}
    service: ssh://localhost:22
  - service: http_status:404
YAML
echo "   ✅ Config created at /etc/cloudflared/config.yml"
EOF

# ── 6. Install and start service ─────────────
echo ""
echo "6. Installing cloudflared as systemd service..."
ssh "$PI" bash -s << 'SERVICE'
set -e
# Stop and uninstall existing service if present
sudo systemctl stop cloudflared 2>/dev/null || true
sudo cloudflared service uninstall 2>/dev/null || true
# Install service (reads /etc/cloudflared/config.yml)
sudo cloudflared service install
sudo systemctl enable cloudflared
sudo systemctl start cloudflared
sleep 3
if systemctl is-active cloudflared > /dev/null 2>&1; then
    echo "   ✅ cloudflared service running"
else
    echo "   ⚠️  Service may still be starting. Check: sudo systemctl status cloudflared"
fi
SERVICE

# ── 7. Verify ────────────────────────────────
echo ""
echo "7. Verifying tunnel..."
sleep 3
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "https://${DASHBOARD_HOST}/api/status" 2>/dev/null || echo "000")
if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "302" ]; then
    echo "   ✅ Tunnel is live!"
else
    echo "   ⚠️  Got HTTP $HTTP_CODE — tunnel may need a moment to propagate."
    echo "   Try: curl https://${DASHBOARD_HOST}/api/status"
fi

echo ""
echo "=========================================="
echo "  ✅ Tunnel setup complete!"
echo ""
echo "  Dashboard: https://$DASHBOARD_HOST"
echo "  SSH:       https://$SSH_HOST"
echo ""
echo "  Set up Cloudflare Access to protect these URLs:"
echo "  https://one.dash.cloudflare.com → Access → Applications"
echo "  Create a self-hosted app for *.${DOMAIN}"
echo "=========================================="
