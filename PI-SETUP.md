# DrawBox Pi Setup Guide

How to SSH into your Raspberry Pi and update DrawBox with the new FLUX Schnell image generation.

---

## 1. Find Your Pi on the Network

Your Pi should be reachable at `drawbox.local` if mDNS is working. Test it:

```bash
ping drawbox.local
```

If that doesn't work, find the Pi's IP address from your router's admin page (usually `192.168.1.1`), or connect a monitor to the Pi and run `hostname -I`.

## 2. SSH Into the Pi

From your Mac's Terminal:

```bash
ssh pi@drawbox.local
```

- Default username: `pi`
- Enter your password when prompted (the default Raspberry Pi OS password is `raspberry` if you haven't changed it)
- If you've never SSH'd before, type `yes` when it asks about the host fingerprint

If `drawbox.local` doesn't resolve, use the IP directly:

```bash
ssh pi@192.168.1.XXX
```

## 3. Get a Replicate API Token

1. Go to [replicate.com](https://replicate.com) in your browser
2. Sign up (you can use GitHub login)
3. Go to **Account Settings** (click your avatar top-right) > **API tokens**
4. Click **Create token**
5. Copy the token — it looks like `r8_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`

Replicate gives free credits to start. FLUX Schnell costs ~$0.003 per image (~330 images per dollar).

## 4. Install the Replicate Python Package

On the Pi (via SSH):

```bash
sudo pip3 install --break-system-packages replicate
```

Verify it installed:

```bash
python3 -c "import replicate; print('OK')"
```

## 5. Pull the Latest Code

On the Pi:

```bash
cd ~/
```

If you have the repo cloned:

```bash
cd drawbox && git pull && cp drawbox.py drawbox_web.py drawbox-simulator.html ~/
```

Or copy files from your Mac (run this **from your Mac**, not the Pi):

```bash
cd /Users/maximep/Documents/Code/drawbox
scp drawbox.py drawbox_web.py drawbox-simulator.html check.sh deploy-web.sh pi@drawbox.local:~/
```

## 6. Add the Replicate Token to the Service Files

The DrawBox systemd services need environment variables. Edit the main service:

```bash
sudo nano /etc/systemd/system/drawbox.service
```

Find the `[Service]` section and add the `REPLICATE_API_TOKEN` line. It should look like:

```ini
[Service]
Type=simple
User=pi
Environment=OPENAI_API_KEY=sk-your-openai-key-here
Environment=REPLICATE_API_TOKEN=r8_your-replicate-token-here
WorkingDirectory=/home/pi
ExecStart=/usr/bin/python3 /home/pi/drawbox.py
Restart=on-failure
RestartSec=10
```

Save: `Ctrl+O`, `Enter`, `Ctrl+X`

Now do the same for the web dashboard service:

```bash
sudo nano /etc/systemd/system/drawbox-web.service
```

Add the same `Environment=REPLICATE_API_TOKEN=r8_your-replicate-token-here` line under `[Service]`.

## 7. Reload and Restart Services

```bash
sudo systemctl daemon-reload
sudo systemctl restart drawbox
sudo systemctl restart drawbox-web
```

Verify they're running:

```bash
sudo systemctl status drawbox
sudo systemctl status drawbox-web
```

Both should say **active (running)**.

## 8. Run the Health Check

```bash
bash ~/check.sh
```

This verifies: internet, API keys (OpenAI + Replicate), mic, speaker, printer, GPIO, Python deps, services, and voice cache.

## 9. Clear the Old Voice Cache (One-Time)

The voice lines changed, so the old cached audio files are stale. Clear them to force re-generation:

```bash
rm -rf ~/.drawbox/voice_cache
```

The cache will rebuild automatically on next startup (~2 minutes for 50 jokes + 15 voice lines).

## 10. Test It

Press the red button and say something! Image generation should now take ~2-5 seconds instead of 30-60 seconds.

Check the logs if anything goes wrong:

```bash
journalctl -u drawbox -f
```

(Press `Ctrl+C` to stop following the log.)

---

## Quick Reference

| Task | Command |
|------|---------|
| SSH into Pi | `ssh pi@drawbox.local` |
| Check service status | `sudo systemctl status drawbox` |
| Restart DrawBox | `sudo systemctl restart drawbox` |
| Restart web dashboard | `sudo systemctl restart drawbox-web` |
| View live logs | `journalctl -u drawbox -f` |
| View web dashboard logs | `journalctl -u drawbox-web -f` |
| Run health check | `bash ~/check.sh` |
| Edit main service file | `sudo nano /etc/systemd/system/drawbox.service` |
| Edit web service file | `sudo nano /etc/systemd/system/drawbox-web.service` |
| Reload after editing services | `sudo systemctl daemon-reload` |
| Reboot Pi | `sudo reboot` |

## One-Command Deploy (Alternative)

If you prefer, the deploy script handles everything automatically from your Mac:

```bash
./deploy-web.sh
```

This copies files to the Pi, installs dependencies, and restarts the web dashboard service. You'll still need to manually add `REPLICATE_API_TOKEN` to the main `drawbox.service` file (Step 6).
