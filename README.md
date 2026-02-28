# DrawBox

**Say it. Draw it. Print it.**

Your kid presses a big red button, says what they want to draw, and a coloring page prints out in 30 seconds. No screen. No app. Just a button, a voice, and a printer.

<p align="center">
  <img src="docs/drawbox.jpg" alt="DrawBox" width="500">
</p>

Built with a Raspberry Pi 5, OpenAI (voice), and your choice of image model — all in a cardboard box with googly eyes.

**~3 hours to build | ~$135 in parts + printer | ~$0.02 per page**

---

## How It Works

```
Kid presses button → "I'm listening!"
    → Kid says "a happy dinosaur with flowers!"
    → Whisper transcribes speech
    → Safety filter (100-word blocklist)
    → Image model generates coloring page
    → Pillow converts to clean line art
    → "Here it comes!"
    → Brother laser printer prints it
    → "All done! Press the button when you want another one!"
```

The whole cycle takes ~10 seconds.

## Image Models

DrawBox supports three image generation backends. Set `IMAGE_MODEL` in your service file:

| Model | Env Value | Speed | Cost | Notes |
|-------|-----------|-------|------|-------|
| **FLUX Schnell** | `flux-schnell` | ~3s | ~$0.003 | Default. Via Replicate. Fastest. |
| **Nano Banana 2** | `nano-banana` | ~5s | Free (quota) | Google Gemini 2.5 Flash Image generation |
| **GPT Image** | `gpt-image` | ~15s | ~$0.02 | OpenAI. Best quality, slowest. |

You can switch models from the web dashboard's Settings panel without restarting.

## Parts

| Part | What It Does | ~Price |
|------|-------------|--------|
| [PiShop Raspberry Pi 5 Budget Kit (4GB)](https://www.amazon.com/s?k=PiShop+Raspberry+Pi+5+Budget+Kit+4GB) | Pi + power supply + SD card + case | $75 |
| [Brother HL-L2405W](https://www.brother-usa.com/products/hll2405w) | B&W laser printer, USB | $100 |
| [EG STARTS 100mm Arcade Button](https://www.amazon.com/dp/B01LZMANZ7) | Big red dome button | $10 |
| [CHANGEEK USB Mic](https://www.amazon.com/dp/B0B4MJQ81C) | Gooseneck USB microphone | $10 |
| USB Sound Bar Speaker | USB-powered speaker (Pi 5 has no headphone jack) | $10 |
| [Pi 5 Active Cooler](https://www.amazon.com/s?k=Raspberry+Pi+5+Official+Active+Cooler) | Blower fan + heatsink (recommended) | $5 |
| [UGREEN USB-A to USB-B Cable](https://www.amazon.com/s?k=UGREEN+USB+A+to+USB+B+Printer+Cable) | Connects Pi to printer | $7 |
| [Female Spade Crimp Terminals](https://www.amazon.com/s?k=female+spade+crimp+terminal+4.8mm) | Connects wires to button's 4.8mm spade terminals | $8 |
| [5" Round Ventilation Grille](https://www.amazon.com/s?k=5+inch+round+ventilation+grille) | Speaker grille for the enclosure | $6 |
| Cardboard + hot glue | The enclosure | $5 |

You may also want: a [micro HDMI to HDMI adapter](https://www.amazon.com/s?k=UGREEN+Micro+HDMI+to+HDMI) for initial Pi setup with a monitor, and a [USB-C SD card reader](https://www.amazon.com/s?k=uni+SD+card+reader+USB+C) for flashing the SD card.

Tools: box cutter, metal ruler, pencil, hot glue gun, wire strippers (for spade terminals).

## Quick Start

### 1. Open the Build Guide

Open `drawbox-guide.html` in any browser. It's a single self-contained HTML file — 20 steps across 6 phases with embedded code, wiring diagrams, SVG cut templates, and troubleshooting.

### 2. Follow the 20 Steps

| Phase | Steps | What You Do |
|-------|-------|-------------|
| Getting Started | 1 | Verify all parts |
| Phase 1 — Pi Setup | 2-4 | Flash SD, SSH in, install dependencies |
| Phase 2 — Printer | 5-6 | Set up CUPS, test print |
| Phase 3 — Software | 7-10 | API key, create `drawbox.py`, manual test, auto-start service |
| Phase 4 — Hardware | 11-13 | Wire button (GPIO 17), plug in mic & speaker, integration test |
| Phase 5 — Enclosure | 14-17 | Cut panels, build box, finishing touches |
| Phase 6 — Finishing Up | 18-20 | WiFi portability, cost cap, web dashboard |

### 3. Deploy the Web Dashboard

```bash
./deploy-web.sh
```

One command copies everything to the Pi, installs Flask + gunicorn, creates the systemd service, and starts the dashboard at `http://drawbox.local:5000`.

### 4. Run the Health Check

```bash
./check.sh
```

Verifies: internet, API key, mic, speaker, printer, GPIO, Python deps, script correctness, services, and voice cache.

## Remote Access (Optional)

Access your DrawBox from anywhere using a Cloudflare Tunnel — no open ports, no dynamic DNS, $0/month on Cloudflare's free tier.

```bash
./deploy-tunnel.sh pi@drawbox.local mybox yourdomain.com
```

This creates:
- `https://mybox.yourdomain.com` — web dashboard (protected by Cloudflare Access)
- `https://mybox-ssh.yourdomain.com` — browser-based SSH terminal

**Prerequisites:** [cloudflared CLI](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) installed on your Mac, logged in (`cloudflared tunnel login`), and a domain on Cloudflare.

After deploying, set up [Cloudflare Access](https://one.dash.cloudflare.com) to protect your tunnel with email OTP or another identity provider.

### Hub Page

`drawbox-hub.html` is a standalone page for monitoring multiple DrawBox Pis from one place. Open it locally or host it on Cloudflare Pages. It shows each Pi's status, temperature, uptime, and lets you toggle settings remotely.

## What's In This Repo

| File | Description |
|------|-------------|
| `drawbox.py` | The main script — records speech, generates coloring pages, prints them |
| `drawbox_web.py` | Flask web dashboard — generate prints, view logs, change settings, run diagnostics |
| `drawbox-guide.html` | Complete interactive build guide — open in any browser |
| `drawbox-simulator.html` | Browser simulator — test the full flow without a Pi (needs API keys) |
| `drawbox-hub.html` | Multi-Pi management hub — monitor and configure multiple DrawBoxes |
| `deploy-web.sh` | One-command deploy to Pi via SSH |
| `deploy-tunnel.sh` | One-command Cloudflare Tunnel setup for remote access |
| `check.sh` | Health check — verifies the entire Pi setup |

## Web Dashboard

The dashboard (`drawbox_web.py`) runs on the Pi and provides:

- **Generate & Print** — type a description, generate and print from any browser
- **Live Logs** — streaming service logs via SSE
- **Settings** — coloring prompt, image model, TTS voice, record duration
- **"Please" Mode** — require kids to say "please" to get a drawing
- **Safety Filter** — toggle the word blocklist on/off (the AI prompt always instructs child-safe output)
- **WiFi Management** — scan and connect to WiFi networks from the dashboard
- **Diagnostics** — printer status, audio devices, disk usage, temperature, error logs
- **Service Control** — restart/stop/start DrawBox, reboot Pi

The dashboard uses gunicorn as a production WSGI server for reliable performance behind reverse proxies like Cloudflare Tunnel.

## Voice Lines

DrawBox talks to kids at every step:

| When | What It Says |
|------|-------------|
| Ready | "Ready! Press the button and tell me what to draw!" |
| Listening | "I'm listening!" |
| Thinking | "Ooh, great idea!" / "That sounds awesome!" / "Cool!" (random) |
| Printing | "Here it comes!" |
| Done | "All done! Press the button when you want another one!" |
| Blocked | "Hmm, I can't draw that. How about something fun like an animal or a rainbow?" |
| Busy | "Hold on, I'm still working on your picture!" |
| Reboot | "Rebooting now! See you in a moment." (hold button 5s) |

## Cost

Each coloring page costs about **$0.02** (with FLUX Schnell):
- ~$0.003 image generation (FLUX Schnell via Replicate)
- ~$0.01 voice (OpenAI TTS)
- ~$0.006 transcription (Whisper)

$5 gets you ~250 pages. Nano Banana 2 (Gemini) is free within Google's quota. Set spending limits at [platform.openai.com](https://platform.openai.com) and [replicate.com](https://replicate.com).

## Safety

This is used by young children, so safety is built in at multiple layers:

- **Word blocklist** — ~100 blocked words covering violence, sexual content, profanity, drugs, horror, and hate speech
- **Hardened prompt** — The image generation prompt explicitly instructs safe-only output
- **Friendly rejection** — "Hmm, I can't draw that. How about something fun like an animal or a rainbow?"
- **Safety toggle** — Admins can toggle the word filter from the dashboard. Even when off, the AI prompt still requires child-safe output (useful for false positives like "skeleton" for Halloween).

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Printer not detected | `lsusb`, try different USB port |
| Mic not recording | `arecord -l` to list devices, check `~/.asoundrc` |
| No speaker sound | `aplay -l`, check `~/.asoundrc` card numbers |
| Button no response | Test with: `python3 -c "from gpiozero import Button; b=Button(17); print('pressed' if b.is_pressed else 'open')"` |
| Images too dark | Lower threshold from 180 to 150 in `drawbox.py` |
| Service won't start | `journalctl -u drawbox -e` — usually wrong API key |
| Dashboard not loading | `sudo systemctl status drawbox-web` |
| Tunnel not connecting | `sudo systemctl status cloudflared` and check `/etc/cloudflared/config.yml` |
| Need to reboot | Hold the big red button for 5 seconds |

## Pi 5 Gotchas

These are already handled in the guide, but good to know:

- **No headphone jack** — must use USB speaker
- **RPi.GPIO doesn't work** — use `gpiozero` instead
- **USB mic only supports 44100Hz** — not 16000Hz
- **Don't use `continue` in try/finally** — causes `is_busy` flag to stick

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full technical reference — script flow, design decisions, systemd config, file layout, and known issues.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Bug fixes, build guide improvements, and photos of your build are all welcome.

## License

[MIT](LICENSE)

---

## Photos

<p align="center">
  <img src="docs/3.jpeg" alt="DrawBox — front view with button, mic, and label" width="400">
  <img src="docs/2.jpeg" alt="DrawBox — giant googly eyes" width="400">
  <img src="docs/1.jpeg" alt="Kid using DrawBox" width="400">
</p>

---

Built for my kids. Now yours can have one too.
