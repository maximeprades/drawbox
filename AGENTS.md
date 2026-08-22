# AGENTS.md

## Cursor Cloud specific instructions

DrawBox targets a Raspberry Pi 5. Most hardware code cannot run on the cloud VM.
Only two things run here: the Flask web dashboard and the `pytest` suite.

### What runs on the VM
- `drawbox_web.py` — the Flask dashboard. Run it with `python3 drawbox_web.py`.
  It listens on `http://0.0.0.0:5000`. It does not need GPIO hardware.
- `tests/` — the full `pytest` suite. Run it with `python3 -m pytest tests/`.
  All tests pass on the VM. The audio tests mock the hardware, so no Pi is needed.

### What does NOT run on the VM
- `drawbox.py` — the button and voice script. It imports `gpiozero` and needs Pi
  GPIO hardware, a USB mic, a USB speaker, and a printer. It cannot run here.
- `check.sh` — a Pi health check. It looks for files in `~/` and checks the
  printer, GPIO, and audio devices. It is not useful on the VM.

### API keys and image generation
- The dashboard starts and works without API keys. The safety filter, settings,
  scripts, and all local logic run without keys.
- Real image generation calls Vercel AI Gateway.
  Set `AI_GATEWAY_API_KEY` to test end-to-end generation. Without a key,
  `/api/generate` returns a graceful "Generation failed" for safe prompts.
  Blocked prompts return the safety message before any API call.
- The active image model is set in the dashboard Settings page (`image_model`).

### Notes
- This project uses the system Python on purpose. There is no virtualenv (see
  `CONTRIBUTING.md`). Install packages with `pip3 install --break-system-packages`.
- pip installs console scripts (like `pytest`) into `~/.local/bin`. That path may
  not be on `PATH`. Use `python3 -m pytest` to avoid the problem.
- There is no configured linter. The only static check is a syntax check:
  `python3 -m py_compile drawbox_core.py drawbox_web.py drawbox.py`.
- The dashboard shows generate results as short-lived toast messages at the bottom
  of the page. They fade quickly, so screenshots can miss them. Use the JSON
  responses from `/api/generate` for reliable proof.
