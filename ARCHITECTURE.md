# Architecture

Technical reference for DrawBox internals. Read this if you want to understand how the system works, contribute, or build your own variant.

## How It Works

1. Kid presses the big red arcade button
2. Speaker says "I'm listening!"
3. Kid says what they want ("a dinosaur riding a skateboard")
4. Whisper transcribes the audio
5. Safety filter checks for inappropriate content (~100-word blocklist)
6. GPT Image generates a coloring page (black & white line drawing)
7. Speaker announces "Here it comes!"
8. Brother laser printer prints it
9. Speaker says "All done! Press the button when you want another one!"

Special interactions:
- **Hold button 5 seconds** — safe reboot ("Rebooting now! See you in a moment.")
- **Press while busy** — "Hold on, I'm still working on your picture!"
- **Blocked content** — "Hmm, I can't draw that. How about something fun like an animal or a rainbow?"

## Hardware

| Component | Exact Model | Connection | Notes |
|---|---|---|---|
| **Computer** | Raspberry Pi 5 (4GB) | — | Running Raspberry Pi OS Bookworm (64-bit Lite) |
| **Printer** | Brother HL-L2405W | USB | Mono laser, set up driverless via IPP |
| **Button** | EG STARTS 100mm arcade button | GPIO 17 (Pin 11) + GND (Pin 9) | Microswitch has 4.8mm spade terminals |
| **Microphone** | CHANGEEK USB mic | USB (card 2, hw:2,0) | Only supports 44100Hz sample rate |
| **Speaker** | USB speaker | USB (card 3) | Configured as default via ~/.asoundrc |
| **Power** | Official Pi 5 27W USB-C PSU | USB-C | — |
| **Storage** | 32GB microSD | SD slot | — |

### Critical Hardware Notes

1. **RPi.GPIO does NOT work on Pi 5** — Must use `gpiozero`. The Pi 5 uses a different I/O chip (RP1) that RPi.GPIO doesn't support.
2. **USB mic sample rate is 44100Hz** — NOT 16000Hz. Using wrong rate causes `PortAudioError: Invalid sample rate`.
3. **GPIO chip on Pi 5 is `gpiochip4`** — Not gpiochip0 like older Pis. gpiozero handles this automatically.
4. **Button wiring**: COM → Pin 11 (GPIO 17), NO → Pin 9 (GND). The tab with the 90-degree bend is COM.
5. **No headphone jack on Pi 5** — Must use USB speaker or Bluetooth.
6. **USB devices shift card numbers** — If plugged into different ports, card numbers change. Verify with `aplay -l` and `arecord -l`.

### GPIO Pinout

```
Pin 9  (GND)     ← Button NO terminal
Pin 11 (GPIO 17) ← Button COM terminal
```

## Software Stack

| Layer | Technology | Notes |
|---|---|---|
| OS | Raspberry Pi OS Bookworm 64-bit Lite | Headless, no desktop |
| Language | Python 3 | System Python, no virtualenv |
| GPIO | gpiozero | NOT RPi.GPIO (incompatible with Pi 5) |
| AI - Image | OpenAI gpt-image-1 | Generates coloring pages |
| AI - Speech-to-Text | OpenAI Whisper (whisper-1) | Transcribes kid's voice |
| AI - Text-to-Speech | OpenAI TTS (tts-1, voice: nova) | Voice feedback |
| Audio Recording | sounddevice + soundfile | Via PortAudio/ALSA |
| Image Processing | Pillow (PIL) | Threshold + resize to Letter |
| Audio Playback | mpg123 | Plays cached .mp3 TTS files |
| Printing | CUPS (lp command) | Driverless IPP setup |
| Web Dashboard | Flask | `http://drawbox.local:5000` |
| Process Management | systemd | Two services: `drawbox.service` + `drawbox-web.service` |

### Python Dependencies

```
openai, Pillow, numpy, sounddevice, soundfile, gpiozero, flask
```

## Script Architecture (drawbox.py)

### Flow

```
main()
  └→ button.wait_for_press()
       ├→ [if held >= 5s] → voice.play("reboot") → sudo reboot
       ├→ [if is_busy] → voice.play("busy") → continue
       └→ [normal press]:
            └→ voice.play("listening")
            └→ record_audio()          # 10s recording at 44100Hz
            └→ transcribe()            # Whisper API
            └→ is_safe()               # Blocklist check
            └→ voice.play("thinking")  # Random variation (5 options)
            └→ generate_image()        # gpt-image-1, 1024x1024
            └→ voice.play("printing")
            └→ print_image()           # lp command to CUPS
            └→ voice.play("done")
```

### Key Design Decisions

1. **gpiozero Button** with `pull_up=True, bounce_time=0.1` — `wait_for_press()` blocks until button pressed, bounce_time prevents double-triggers
2. **Long-press reboot** — Polls `btn.is_pressed` in 0.1s increments; if held >= 5 seconds, plays "Rebooting now!" and runs `sudo reboot`
3. **VoiceFeedback class** — Pre-generates and caches all TTS lines as .mp3 at startup (`~/.drawbox/voice_cache/`). Uses MD5 hash of voice+text for deterministic filenames. 0.5s sleep before playback lets USB speaker wake up.
4. **if/elif/else chain** (not try/continue) — Avoids `continue` inside `try/finally` blocks which would skip `is_busy = False` reset.
5. **Walrus operator** for transcription: `elif not (text := transcribe(path)) or len(text.strip()) < 2`
6. **Safety blocklist** — Set of ~100 blocked words checked against transcription. Plus prompt engineering in COLORING_PROMPT.
7. **Image post-processing** — Convert to grayscale → threshold at 180 → resize to 1125x1125 → paste centered on 1275x1650 Letter canvas
8. **Busy guard** — `is_busy` flag prevents overlapping requests
9. **Minimum audio check** — Recordings shorter than 0.5s are discarded (silence/accidental press)

## Web Dashboard (drawbox_web.py)

Flask web app at `http://drawbox.local:5000`. Runs as a separate systemd service.

### Routes

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | Dashboard (single-page, inline HTML) |
| `/guide` | GET | Serves drawbox-guide.html |
| `/simulator` | GET | Serves drawbox-simulator.html |
| `/api/generate` | POST | Safety check → generate image → print |
| `/api/logs` | GET | SSE stream of journalctl |
| `/api/settings` | GET/POST | Read/write settings |
| `/api/diagnostics` | POST | Run allowlisted diagnostic commands |
| `/api/service/<action>` | POST | systemctl restart/stop/start |
| `/api/status` | GET | Service status, temperature, uptime |
| `/api/reboot` | POST | sudo reboot |

### Implementation Notes

- **Shared core** — Both scripts import from `drawbox_core.py` (API keys, safety, image generation, printing, logging)
- **No auth** — Home toy on local network
- **Diagnostics allowlist** — 14 commands, no arbitrary shell execution
- **Log streaming** — SSE via `journalctl -u drawbox -f`

## File Layout

### Development (Mac)

```
drawbox_core.py         # Shared logic (API keys, safety, generation, printing)
drawbox.py              # Main script (the brain)
drawbox_web.py          # Flask web dashboard
drawbox-guide.html      # 20-step interactive build guide
drawbox-simulator.html  # Browser-based simulator (no Pi needed)
deploy-web.sh           # One-command deploy to Pi
check.sh                # Health check script
requirements.txt        # Python dependencies
```

### Pi (/home/pi/)

```
~/drawbox_core.py         # Shared logic module
~/drawbox.py              # Main script (drawbox.service)
~/drawbox_web.py          # Web dashboard (drawbox-web.service)
~/drawbox-guide.html      # Served at /guide
~/drawbox-simulator.html  # Served at /simulator
~/check.sh                # Health check
~/.drawbox/voice_cache/   # Cached TTS audio (.mp3)
~/.drawbox/web_settings.json  # Dashboard settings
~/.asoundrc               # ALSA audio device config
```

### Systemd Services

```
/etc/systemd/system/drawbox.service      # Main script, auto-starts on boot
/etc/systemd/system/drawbox-web.service  # Dashboard, auto-starts on boot
/etc/sudoers.d/drawbox-web               # Passwordless systemctl for dashboard
```

## Configuration

### Environment Variables

```bash
# Set in drawbox.service AND ~/.bashrc (for manual runs)
# Systemd does NOT read ~/.bashrc — key must be in the service file
Environment=OPENAI_API_KEY=sk-...
```

### ALSA Audio (~/.asoundrc)

```
defaults.pcm.card 3
defaults.ctl.card 3
```

Card numbers depend on USB port order. Verify with `aplay -l` and `arecord -l`.

### Printer Setup

```bash
# Driverless IPP (recommended)
sudo lpadmin -p drawbox-printer -E \
    -v "ipp://Brother%20HL-L2405W%20(USB)._ipp._tcp.local/" \
    -m everywhere
sudo lpoptions -d drawbox-printer
```

## Known Issues

| Issue | Cause | Fix |
|---|---|---|
| SSH dies after reboot | Socket not enabled | `sudo systemctl enable ssh.service && sudo systemctl enable ssh.socket` |
| "GPIO busy" error | Previous process holds pin | `sudo killall python3` then restart service |
| `continue` in try/finally | `is_busy` flag stays stuck | Use if/elif/else chains instead (already fixed) |
| drawbox.local not resolving | mDNS issue | Use IP directly: `hostname -I` |
