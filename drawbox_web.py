#!/usr/bin/env python3
"""DrawBox Web Dashboard — Control panel accessible from any browser on your network."""

import os, json, tempfile, subprocess, base64, time as _time, threading, shutil
from pathlib import Path
from io import BytesIO
from datetime import datetime
from collections import Counter
from urllib.request import Request, urlopen
from flask import Flask, request, jsonify, Response, render_template_string, send_file
import replicate
from openai import OpenAI
from PIL import Image

# ── CONFIG ───────────────────────────────────────
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "nano-banana")
PRINTER_NAME = "drawbox-printer"
SETTINGS_FILE = Path.home() / ".drawbox" / "web_settings.json"
PLEASE_MODE_FILE = Path.home() / ".drawbox" / "please_mode"
SAFETY_MODE_FILE = Path.home() / ".drawbox" / "safety_mode"
PRINT_LOG_FILE = Path.home() / ".drawbox" / "print_log.jsonl"
API_KEYS_FILE = Path.home() / ".drawbox" / "api_keys.json"
REPO_DIR = Path.home() / "drawbox-repo"
GUIDE_PATH = Path.home() / "drawbox-guide.html"
SIMULATOR_PATH = Path.home() / "drawbox-simulator.html"

# ── API KEYS (file > env var) ────────────────────
def _load_api_keys():
    """Load API keys from file, fall back to environment variables."""
    keys = {}
    if API_KEYS_FILE.exists():
        try:
            keys = json.loads(API_KEYS_FILE.read_text())
        except Exception:
            pass
    return {
        "openai": keys.get("openai") or os.environ.get("OPENAI_API_KEY") or "",
        "replicate": keys.get("replicate") or os.environ.get("REPLICATE_API_TOKEN") or "",
        "gemini": keys.get("gemini") or os.environ.get("GEMINI_API_KEY") or "",
    }

def _apply_api_keys():
    """Apply loaded keys to module globals and environment."""
    global OPENAI_API_KEY, GEMINI_API_KEY, client
    keys = _load_api_keys()
    OPENAI_API_KEY = keys["openai"]
    GEMINI_API_KEY = keys["gemini"]
    if keys["replicate"]:
        os.environ["REPLICATE_API_TOKEN"] = keys["replicate"]
    if OPENAI_API_KEY:
        client = OpenAI(api_key=OPENAI_API_KEY)

_apply_api_keys()

DEFAULT_COLORING_PROMPT = """Create a simple coloring page for children ages 3-8.
This is used by YOUNG CHILDREN — output MUST be 100% child-safe.
- Black and white LINE DRAWING only
- Thick, clean outlines (3-4px stroke)
- NO shading, NO gradients, NO filled/solid areas
- NO gray — pure black lines on white
- Simple shapes, minimal fine detail
- Large open areas for coloring with crayons
- Friendly, fun, cute, non-scary style
- Centered with padding — the subject must NOT touch or extend to the edges
- Leave at least 10% empty white space on all sides as margin
- Style: children's coloring book page
- ONLY draw safe, wholesome subjects (animals, nature, vehicles, food, toys)
- NEVER draw anything violent, scary, sexual, or inappropriate for a 5-year-old
- If the request is ambiguous, default to the most innocent interpretation"""

# ── SAFETY BLOCKLIST ─────────────────────────────
BLOCKED_WORDS = {
    "kill", "murder", "dead", "death", "die", "dying", "corpse",
    "blood", "bloody", "gore", "gory", "wound", "stab", "shoot",
    "gun", "guns", "rifle", "pistol", "shotgun", "weapon", "knife",
    "bomb", "explode", "explosion", "grenade", "missile", "nuke",
    "sex", "sexy", "sexual", "nude", "naked", "porn", "hentai",
    "penis", "dick", "cock", "vagina", "pussy", "boob", "breast",
    "nipple", "butt", "ass", "anus", "genital", "erotic", "orgasm",
    "fuck", "shit", "damn", "bitch", "bastard", "crap", "piss",
    "whore", "slut", "hooker", "prostitute", "stripper",
    "drug", "drugs", "cocaine", "heroin", "meth", "weed", "marijuana",
    "alcohol", "beer", "wine", "vodka", "drunk", "cigarette", "smoke",
    "devil", "satan", "demon", "hell", "torture", "horror", "zombie",
    "vampire", "skeleton", "skull", "coffin", "grave", "creepy",
    "scary", "nightmare", "terror", "evil", "wicked", "curse",
    "hate", "racist", "racism", "nazi", "hitler", "slavery", "slave",
    "suicide", "hanging", "noose", "drown", "poison", "overdose",
    "rape", "molest", "abuse", "kidnap", "assault", "victim",
    "war", "soldier", "army", "military", "combat", "battle",
    "thong", "lingerie", "bikini", "underwear",
    "pee", "fart", "vomit", "snot",
}

def is_safe(text):
    words = text.lower()
    return not any(w in words for w in BLOCKED_WORDS)

# ── SETTINGS ─────────────────────────────────────
def load_settings():
    defaults = {"coloring_prompt": DEFAULT_COLORING_PROMPT,
                "tts_voice": "nova", "record_seconds": 10}
    if SETTINGS_FILE.exists():
        try:
            saved = json.loads(SETTINGS_FILE.read_text())
            # Only override defaults with non-empty saved values
            for k, v in saved.items():
                if v or v == 0:  # keep falsy 0 but skip "" and None
                    defaults[k] = v
        except Exception:
            pass
    return defaults

def save_settings(data):
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, indent=2))

# ── ANALYTICS ────────────────────────────────────
def log_print_event(prompt, model, duration_s, source="web"):
    """Append a print event to the analytics log."""
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "prompt": prompt[:200],
        "model": model,
        "duration_s": round(duration_s, 2),
        "source": source,
    }
    try:
        PRINT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(PRINT_LOG_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass

# ── IMAGE GENERATION & PRINTING ──────────────────
client = None  # initialized by _apply_api_keys()
is_generating = False
last_image_b64 = None

def generate_image(desc):
    settings = load_settings()
    prompt = settings.get("coloring_prompt", DEFAULT_COLORING_PROMPT)
    full_prompt = f"{prompt}\n\nChild requested: {desc}"
    model = settings.get("image_model", IMAGE_MODEL)
    if model == "nano-banana":
        img_bytes = _generate_nano_banana(full_prompt)
    elif model == "gpt-image":
        img_bytes = _generate_gpt_image(full_prompt)
    else:
        img_bytes = _generate_flux_schnell(full_prompt)
    return _postprocess(img_bytes)


def _generate_flux_schnell(prompt):
    output = replicate.run(
        "black-forest-labs/flux-schnell",
        input={
            "prompt": prompt, "num_outputs": 1, "aspect_ratio": "3:4",
            "output_format": "png", "num_inference_steps": 4, "go_fast": True,
        })
    return output[0].read()


def _generate_nano_banana(prompt):
    url = (f"https://generativelanguage.googleapis.com/v1beta/"
           f"models/gemini-2.5-flash-image:generateContent"
           f"?key={GEMINI_API_KEY}")
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {"aspectRatio": "3:4"},
        },
    }).encode()
    req = Request(url, data=body, headers={"Content-Type": "application/json"})
    resp = urlopen(req, timeout=60)
    data = json.loads(resp.read())
    for part in data["candidates"][0]["content"]["parts"]:
        if "inlineData" in part:
            return base64.b64decode(part["inlineData"]["data"])
    raise RuntimeError("No image in Gemini response")


def _generate_gpt_image(prompt):
    r = client.images.generate(
        model="gpt-image-1", prompt=prompt,
        size="1024x1536", quality="low")
    return base64.b64decode(r.data[0].b64_json)


def _postprocess(img_bytes):
    img = Image.open(BytesIO(img_bytes)).convert("L")
    img = img.point(lambda x: 0 if x < 180 else 255, "1").convert("L")
    iw, ih = img.size
    canvas_w, canvas_h = 1275, 1650
    margin = 75
    max_w = canvas_w - 2 * margin
    max_h = canvas_h - 2 * margin
    scale = min(max_w / iw, max_h / ih)
    new_w = int(iw * scale)
    new_h = int(ih * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new("L", (canvas_w, canvas_h), 255)
    canvas.paste(img, ((canvas_w - new_w) // 2, (canvas_h - new_h) // 2))
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    canvas.save(tmp.name); tmp.close()
    return tmp.name

def print_image(path):
    subprocess.run(["lp", "-d", PRINTER_NAME,
        "-o", "media=Letter", "-o", "fit-to-page", path], check=True)
    os.unlink(path)

# ── DIAGNOSTICS ALLOWLIST ────────────────────────
DIAGNOSTIC_COMMANDS = {
    "printer_status": ["lpstat", "-p", "-d"],
    "printer_queue":  ["lpstat", "-o"],
    "audio_inputs":   ["arecord", "-l"],
    "audio_outputs":  ["aplay", "-l"],
    "service_status": ["systemctl", "status", "drawbox"],
    "web_service":    ["systemctl", "status", "drawbox-web"],
    "disk_usage":     ["df", "-h", "/"],
    "uptime":         ["uptime"],
    "temperature":    ["vcgencmd", "measure_temp"],
    "wifi_status":    ["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi"],
    "wifi_list":      ["nmcli", "dev", "wifi", "list"],
    "rpi_connect":    ["rpi-connect", "status"],
    "journal_errors": ["journalctl", "-u", "drawbox", "-p", "err", "-n", "20", "--no-pager"],
    "cups_log":       ["tail", "-n", "30", "/var/log/cups/error_log"],
}

# ── FLASK APP ────────────────────────────────────
from werkzeug.middleware.proxy_fix import ProxyFix
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# CORS: allow requests from the cloud hub dashboard
@app.after_request
def add_cors(response):
    origin = request.headers.get("Origin", "")
    if origin and (".drawbox." in origin or origin.endswith(".pages.dev")):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, OPTIONS"
    return response

@app.route("/api/<path:path>", methods=["OPTIONS"])
def cors_preflight(path):
    return "", 204

# ── HTML TEMPLATE ────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DrawBox Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {
  --background: #ffffff;
  --foreground: #0a0a0a;
  --card: #ffffff;
  --card-foreground: #0a0a0a;
  --muted: #f5f5f5;
  --muted-foreground: #737373;
  --secondary: #f5f5f5;
  --secondary-foreground: #171717;
  --primary: #171717;
  --primary-foreground: #fafafa;
  --accent: #f5f5f5;
  --accent-foreground: #171717;
  --border: #e5e5e5;
  --input: #e5e5e5;
  --ring: #0a0a0a;
  --destructive: #ef4444;
  --success: #22c55e;
  --warning: #f59e0b;
  --info: #3b82f6;
  --sidebar-bg: #fafafa;
  --sidebar-foreground: #0a0a0a;
  --sidebar-accent: #f0f0f0;
  --sidebar-border: #e5e5e5;
  --sidebar-muted: #737373;
  --sidebar-width: 250px;
  --chart-1: #e76f51;
  --chart-2: #2a9d8f;
  --chart-3: #264653;
  --chart-4: #e9c46a;
  --radius: 0.5rem;
  --code-bg: #18181b;
  --code-text: #d4d4d8;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  background: var(--background);
  color: var(--foreground);
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}

/* ── SIDEBAR ────────────────────────── */
.sidebar {
  position: fixed; top: 0; left: 0; bottom: 0;
  width: var(--sidebar-width); background: var(--sidebar-bg);
  border-right: 1px solid var(--sidebar-border);
  display: flex; flex-direction: column;
  z-index: 50; overflow-y: auto;
}
.sidebar-header {
  padding: 20px 16px 16px;
  border-bottom: 1px solid var(--sidebar-border);
}
.sidebar-logo {
  display: flex; align-items: center; gap: 10px;
  font-size: 15px; font-weight: 700; letter-spacing: -0.01em;
}
.sidebar-logo svg { width: 22px; height: 22px; flex-shrink: 0; }
.sidebar-label {
  padding: 20px 16px 6px;
  font-size: 11px; font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.5px;
  color: var(--sidebar-muted);
}
.sidebar-item {
  display: flex; align-items: center; gap: 10px;
  padding: 8px 12px; margin: 1px 8px;
  border-radius: var(--radius); font-size: 14px; font-weight: 500;
  color: var(--sidebar-foreground); text-decoration: none;
  cursor: pointer; transition: background 0.15s;
}
.sidebar-item:hover { background: var(--sidebar-accent); }
.sidebar-item.active {
  background: var(--sidebar-accent);
  font-weight: 600;
}
.sidebar-item svg { width: 18px; height: 18px; flex-shrink: 0; opacity: 0.7; }
.sidebar-item.active svg { opacity: 1; }
.sidebar-footer {
  margin-top: auto; padding: 16px;
  border-top: 1px solid var(--sidebar-border);
  display: flex; gap: 12px; flex-wrap: wrap;
}
.sidebar-footer a {
  font-size: 12px; color: var(--sidebar-muted);
  text-decoration: none;
}
.sidebar-footer a:hover { color: var(--foreground); }

/* ── MAIN CONTENT ───────────────────── */
.main-content {
  margin-left: var(--sidebar-width);
  padding: 32px;
  min-height: 100vh;
}
.page { max-width: 1100px; display: none; }
.page.active { display: block; }
.page-header { margin-bottom: 24px; }
.page-title { font-size: 22px; font-weight: 700; letter-spacing: -0.02em; }
.page-desc { font-size: 14px; color: var(--muted-foreground); margin-top: 2px; }

/* ── CARDS ──────────────────────────── */
.card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: 0 1px 2px 0 rgba(0,0,0,0.03);
}
.card-header { padding: 20px 24px 0; }
.card-title { font-size: 15px; font-weight: 600; }
.card-desc { font-size: 13px; color: var(--muted-foreground); margin-top: 2px; }
.card-content { padding: 20px 24px; }
.card-content:first-child { padding-top: 24px; }
.card + .card { margin-top: 16px; }
/* Override card+card margin inside grids — gap handles spacing */
.stat-grid > .card + .card,
.analytics-grid > .card + .card { margin-top: 0; }

/* ── STAT CARDS ─────────────────────── */
.stat-grid {
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px;
  margin-bottom: 24px;
}
.stat-card { padding: 20px 24px; display: flex; flex-direction: column; height: 120px; box-sizing: border-box; }
.stat-label { font-size: 12px; font-weight: 500; color: var(--muted-foreground); text-transform: uppercase; letter-spacing: 0.04em; }
.stat-value { font-size: 24px; font-weight: 700; letter-spacing: -0.02em; margin-top: 8px; line-height: 1.2; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.stat-sub { font-size: 12px; margin-top: auto; padding-top: 8px; color: var(--muted-foreground); }
.stat-sub.up { color: var(--success); }

/* ── BUTTONS ────────────────────────── */
.btn {
  display: inline-flex; align-items: center; justify-content: center;
  gap: 6px; border-radius: var(--radius); font-size: 14px;
  font-weight: 500; padding: 8px 16px; border: none; cursor: pointer;
  transition: all 0.15s ease; font-family: inherit;
}
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-primary { background: var(--primary); color: var(--primary-foreground); }
.btn-primary:hover:not(:disabled) { opacity: 0.9; }
.btn-secondary { background: var(--secondary); color: var(--secondary-foreground); }
.btn-secondary:hover:not(:disabled) { background: #ebebeb; }
.btn-destructive { background: var(--destructive); color: #fff; }
.btn-destructive:hover:not(:disabled) { opacity: 0.9; }
.btn-outline { background: transparent; border: 1px solid var(--border); color: var(--foreground); }
.btn-outline:hover:not(:disabled) { background: var(--accent); }
.btn-ghost { background: transparent; color: var(--foreground); padding: 6px 10px; }
.btn-ghost:hover:not(:disabled) { background: var(--accent); }
.btn-success { background: var(--success); color: #fff; }
.btn-success:hover:not(:disabled) { opacity: 0.9; }
.btn-sm { padding: 6px 12px; font-size: 13px; }
.btn-row { display: flex; gap: 8px; flex-wrap: wrap; }

/* ── INPUTS ─────────────────────────── */
.input, .textarea, .select {
  display: block; width: 100%; padding: 8px 12px;
  font-size: 14px; font-family: inherit;
  border: 1px solid var(--input);
  border-radius: var(--radius); background: transparent;
  outline: none; transition: border-color 0.15s;
  color: var(--foreground);
}
.input:focus, .textarea:focus, .select:focus {
  border-color: var(--ring);
  box-shadow: 0 0 0 2px rgba(10,10,10,0.05);
}
.textarea { resize: vertical; min-height: 120px; line-height: 1.5; }
.select {
  cursor: pointer; -webkit-appearance: none; appearance: none;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23999' stroke-width='2'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E");
  background-repeat: no-repeat; background-position: right 12px center;
}
.code-input {
  font-family: 'SF Mono', 'Fira Code', ui-monospace, monospace;
  font-size: 13px; background: var(--code-bg); color: var(--code-text);
  border-color: #333;
}
.code-input:focus { border-color: #555; }
.form-group { margin-bottom: 16px; }
.form-group:last-child { margin-bottom: 0; }
.form-label {
  display: block; font-size: 13px; font-weight: 500;
  margin-bottom: 6px; color: var(--foreground);
}
.form-hint { font-size: 12px; color: var(--muted-foreground); margin-top: 4px; }

/* ── BADGES ─────────────────────────── */
.badge {
  display: inline-flex; align-items: center; padding: 2px 10px;
  border-radius: 9999px; font-size: 12px; font-weight: 500;
}
.badge-success { background: #dcfce7; color: #166534; }
.badge-warning { background: #fef3c7; color: #92400e; }
.badge-destructive { background: #fee2e2; color: #991b1b; }
.badge-default { background: var(--secondary); color: var(--secondary-foreground); }
.badge-info { background: #dbeafe; color: #1e40af; }

/* ── DOT INDICATOR ──────────────────── */
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
.dot-green { background: var(--success); }
.dot-red { background: var(--destructive); }
.dot-amber { background: var(--warning); }

/* ── DATA TABLE ─────────────────────── */
.data-table { width: 100%; border-collapse: collapse; font-size: 14px; }
.data-table th {
  text-align: left; padding: 10px 24px;
  font-size: 12px; font-weight: 500;
  color: var(--muted-foreground);
  border-bottom: 1px solid var(--border);
}
.data-table td {
  padding: 10px 24px;
  border-bottom: 1px solid var(--border);
}
.data-table tbody tr:last-child td { border-bottom: none; }
.data-table td.muted { color: var(--muted-foreground); font-size: 13px; }

/* ── ANALYTICS CHARTS (CSS-only) ──── */
.analytics-grid {
  display: grid; grid-template-columns: 1fr 1fr; gap: 16px;
  margin-bottom: 16px;
  align-items: stretch;
}
.analytics-grid > .card { display: flex; flex-direction: column; margin-top: 0; }
.analytics-grid > .card > .card-content { flex: 1; min-height: 60px; }
.bar-row { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
.bar-row:last-child { margin-bottom: 0; }
.bar-label { width: 110px; font-size: 13px; font-weight: 500; flex-shrink: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.bar-track { flex: 1; height: 22px; background: var(--muted); border-radius: 4px; overflow: hidden; }
.bar-fill { height: 100%; border-radius: 4px; transition: width 0.6s ease; min-width: 2px; }
.bar-value { width: 40px; text-align: right; font-size: 13px; font-weight: 600; flex-shrink: 0; }

/* ── LOG AREA ──────────────────────── */
.log-area {
  background: var(--code-bg); color: var(--code-text);
  border-radius: 0 0 var(--radius) var(--radius);
  padding: 16px 20px;
  font-family: 'SF Mono', 'Fira Code', ui-monospace, monospace;
  font-size: 12px; line-height: 1.7;
  height: 400px; overflow-y: auto;
  white-space: pre-wrap; word-break: break-word;
}

/* ── DIAGNOSTICS ───────────────────── */
.diag-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 8px;
  margin-bottom: 16px;
}
.diag-out {
  background: var(--code-bg); color: var(--code-text);
  border-radius: var(--radius); padding: 16px 20px;
  font-family: 'SF Mono', 'Fira Code', ui-monospace, monospace;
  font-size: 12px; line-height: 1.6;
  min-height: 60px; max-height: 350px; overflow-y: auto;
  white-space: pre-wrap; word-break: break-word;
}

/* ── GENERATE ──────────────────────── */
.gen-form { display: flex; gap: 10px; }
.gen-form .input { flex: 1; }
.preview-img {
  max-width: 100%; border: 1px solid var(--border);
  border-radius: var(--radius); margin-top: 16px; display: none;
}

/* ── WIFI ──────────────────────────── */
.wifi-network {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 0; border-bottom: 1px solid var(--border);
  cursor: pointer; transition: background 0.1s;
}
.wifi-network:last-child { border-bottom: none; }
.wifi-network:hover { background: var(--accent); margin: 0 -24px; padding: 10px 24px; }
.wifi-ssid { font-size: 14px; font-weight: 500; flex: 1; }
.wifi-meta { font-size: 12px; color: var(--muted-foreground); }

/* ── TOGGLE ────────────────────────── */
.toggle {
  position: relative; width: 44px; height: 24px;
  background: var(--border); border-radius: 12px;
  cursor: pointer; transition: background 0.2s;
  flex-shrink: 0; border: none;
}
.toggle.on { background: var(--success); }
.toggle::after {
  content: ''; position: absolute; top: 2px; left: 2px;
  width: 20px; height: 20px; background: #fff;
  border-radius: 50%; transition: transform 0.2s;
  box-shadow: 0 1px 3px rgba(0,0,0,0.1);
}
.toggle.on::after { transform: translateX(20px); }

/* ── MOBILE HEADER ─────────────────── */
.mobile-header {
  display: none; position: fixed; top: 0; left: 0; right: 0;
  height: 56px; background: var(--background);
  border-bottom: 1px solid var(--border);
  padding: 0 16px; z-index: 40;
  align-items: center; gap: 12px;
}
.mobile-header .mobile-title { font-size: 15px; font-weight: 700; }
.mobile-header button { background: none; border: none; cursor: pointer; padding: 4px; }
.sidebar-overlay {
  display: none; position: fixed; inset: 0;
  background: rgba(0,0,0,0.3); z-index: 45;
}

/* ── TOAST ──────────────────────────── */
.toast {
  position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
  padding: 10px 24px; border-radius: var(--radius);
  font-size: 13px; font-weight: 500; color: #fff;
  z-index: 999; opacity: 0; transition: opacity 0.3s;
  pointer-events: none;
}
.toast.show { opacity: 1; }
.toast-ok { background: var(--success); }
.toast-err { background: var(--destructive); }

/* ── SETTING ROW ───────────────────── */
.setting-row {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 0; border-bottom: 1px solid var(--border);
}
.setting-row:last-child { border-bottom: none; }
.setting-info { flex: 1; }
.setting-name { font-size: 14px; font-weight: 500; }
.setting-desc { font-size: 12px; color: var(--muted-foreground); margin-top: 2px; }

/* ── RESPONSIVE ────────────────────── */
@media (max-width: 768px) {
  .sidebar {
    transform: translateX(-100%);
    transition: transform 0.2s ease;
    box-shadow: none;
  }
  .sidebar.open {
    transform: translateX(0);
    box-shadow: 4px 0 24px rgba(0,0,0,0.1);
  }
  .sidebar-overlay.show { display: block; }
  .mobile-header { display: flex; }
  .main-content { margin-left: 0; padding: 72px 16px 32px; }
  .stat-grid { grid-template-columns: repeat(2, 1fr); }
  .analytics-grid { grid-template-columns: 1fr; }
  .gen-form { flex-direction: column; }
  .log-area { height: 280px; }
  .data-table th, .data-table td { padding: 8px 12px; font-size: 13px; }
}
@media (max-width: 480px) {
  .stat-grid { grid-template-columns: 1fr; }
}
</style>
</head>
<body>

<!-- MOBILE HEADER -->
<header class="mobile-header">
  <button onclick="toggleSidebar()">
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12h18M3 6h18M3 18h18"/></svg>
  </button>
  <span class="mobile-title">DrawBox</span>
</header>
<div class="sidebar-overlay" id="sidebarOverlay" onclick="toggleSidebar()"></div>

<!-- SIDEBAR -->
<aside class="sidebar" id="sidebar">
  <div class="sidebar-header">
    <div class="sidebar-logo">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="3" width="18" height="18" rx="3"/><path d="M8 16l2-5 3 3 3-7"/></svg>
      DrawBox
    </div>
  </div>
  <nav>
    <div class="sidebar-label">Menu</div>
    <a class="sidebar-item active" data-page="overview" onclick="showPage('overview')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>
      Overview
    </a>
    <a class="sidebar-item" data-page="generate" onclick="showPage('generate')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>
      Generate
    </a>
    <a class="sidebar-item" data-page="logs" onclick="showPage('logs')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 19h16M4 15h16M4 11h10M4 7h6"/></svg>
      Logs
    </a>
    <a class="sidebar-item" data-page="settings" onclick="showPage('settings')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/></svg>
      Settings
    </a>
    <div class="sidebar-label">System</div>
    <a class="sidebar-item" data-page="wifi" onclick="showPage('wifi')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M5 12.55a11 11 0 0 1 14.08 0M1.42 9a16 16 0 0 1 21.16 0M8.53 16.11a6 6 0 0 1 6.95 0M12 20h.01"/></svg>
      WiFi
    </a>
    <a class="sidebar-item" data-page="diagnostics" onclick="showPage('diagnostics')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>
      Diagnostics
    </a>
    <a class="sidebar-item" data-page="update" onclick="showPage('update')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg>
      Update
    </a>
    <a class="sidebar-item" data-page="system" onclick="showPage('system')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><circle cx="6" cy="6" r="1"/><circle cx="6" cy="18" r="1"/></svg>
      System
    </a>
  </nav>
  <div class="sidebar-footer">
    <a href="/guide" target="_blank">Build Guide</a>
    <a href="/simulator" target="_blank">Simulator</a>
  </div>
</aside>

<!-- MAIN CONTENT -->
<main class="main-content">

  <!-- ═══ OVERVIEW ═══ -->
  <div class="page active" id="page-overview">
    <div class="page-header">
      <h1 class="page-title">Overview</h1>
      <p class="page-desc">System status and usage analytics</p>
    </div>

    <div class="stat-grid">
      <div class="card stat-card">
        <div class="stat-label">Status</div>
        <div class="stat-value" id="ovStatus">--</div>
        <div class="stat-sub" id="ovStatusSub">DrawBox service</div>
      </div>
      <div class="card stat-card">
        <div class="stat-label">Pages Printed</div>
        <div class="stat-value" id="ovTotal">--</div>
        <div class="stat-sub" id="ovToday"></div>
      </div>
      <div class="card stat-card">
        <div class="stat-label">Temperature</div>
        <div class="stat-value" id="ovTemp">--</div>
        <div class="stat-sub" id="ovTempSub">Raspberry Pi</div>
      </div>
      <div class="card stat-card">
        <div class="stat-label">Uptime</div>
        <div class="stat-value" id="ovUptime">--</div>
        <div class="stat-sub" id="ovUptimeSub">Since last reboot</div>
      </div>
    </div>

    <div class="analytics-grid">
      <div class="card">
        <div class="card-header">
          <div class="card-title">Model Usage</div>
          <div class="card-desc">Breakdown by image model</div>
        </div>
        <div class="card-content" id="modelChart">
          <div style="color:var(--muted-foreground);font-size:13px">Loading...</div>
        </div>
      </div>
      <div class="card">
        <div class="card-header">
          <div class="card-title">Popular Prompts</div>
          <div class="card-desc">Most requested drawings</div>
        </div>
        <div class="card-content" id="topPrompts">
          <div style="color:var(--muted-foreground);font-size:13px">Loading...</div>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-header" style="display:flex;align-items:center;justify-content:space-between">
        <div>
          <div class="card-title">Recent Activity</div>
          <div class="card-desc">Last 10 generated pages</div>
        </div>
        <div class="badge badge-default" id="ovAvgDuration">--</div>
      </div>
      <div class="card-content" style="padding:0">
        <table class="data-table">
          <thead>
            <tr><th>Time</th><th>Prompt</th><th>Model</th><th>Duration</th></tr>
          </thead>
          <tbody id="recentBody">
            <tr><td colspan="4" class="muted" style="text-align:center;padding:20px">Loading...</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- ═══ GENERATE ═══ -->
  <div class="page" id="page-generate">
    <div class="page-header">
      <h1 class="page-title">Generate & Print</h1>
      <p class="page-desc">Create a coloring page and send it to the printer</p>
    </div>
    <div class="card">
      <div class="card-content">
        <div class="form-label">What should we draw?</div>
        <div class="gen-form">
          <input class="input" type="text" id="genInput" placeholder="A happy dinosaur riding a skateboard..." />
          <button class="btn btn-primary" id="genBtn" onclick="doGenerate()">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 9l6-6 6 6"/><path d="M12 3v14"/><path d="M5 21h14"/></svg>
            Generate & Print
          </button>
        </div>
        <img class="preview-img" id="previewImg" alt="Generated coloring page" />
      </div>
    </div>
  </div>

  <!-- ═══ LOGS ═══ -->
  <div class="page" id="page-logs">
    <div class="page-header">
      <h1 class="page-title">Live Logs</h1>
      <p class="page-desc">Real-time log stream from the DrawBox service</p>
    </div>
    <div class="card">
      <div class="card-header" style="display:flex;align-items:center;justify-content:space-between">
        <div class="card-title" style="font-family:ui-monospace,monospace;font-size:13px;color:var(--muted-foreground)">journalctl -u drawbox -f</div>
        <button class="btn btn-ghost btn-sm" onclick="clearLog()">Clear</button>
      </div>
      <div class="log-area" id="logArea">Connecting to log stream...</div>
    </div>
  </div>

  <!-- ═══ SETTINGS ═══ -->
  <div class="page" id="page-settings">
    <div class="page-header">
      <h1 class="page-title">Settings</h1>
      <p class="page-desc">Configure image generation, voice, and safety</p>
    </div>

    <div class="card">
      <div class="card-header"><div class="card-title">Image Generation</div></div>
      <div class="card-content">
        <div class="form-group">
          <label class="form-label">Coloring Prompt</label>
          <textarea class="textarea" id="cfgPrompt" rows="8"></textarea>
          <div class="form-hint">System prompt sent with every image generation request</div>
        </div>
        <div class="form-group">
          <label class="form-label">Image Model</label>
          <select class="select" id="cfgModel">
            <option value="flux-schnell">FLUX Schnell (Replicate) — fastest</option>
            <option value="nano-banana">Nano Banana 2 (Gemini)</option>
            <option value="gpt-image">GPT Image (OpenAI) — slowest</option>
          </select>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-header"><div class="card-title">Voice & Audio</div></div>
      <div class="card-content">
        <div class="form-group">
          <label class="form-label">TTS Voice</label>
          <select class="select" id="cfgVoice">
            <option value="nova">nova</option>
            <option value="alloy">alloy</option>
            <option value="echo">echo</option>
            <option value="fable">fable</option>
            <option value="onyx">onyx</option>
            <option value="shimmer">shimmer</option>
          </select>
        </div>
        <div class="form-group">
          <label class="form-label">Record Duration (seconds)</label>
          <input class="input" type="number" id="cfgRecSec" min="3" max="30" style="max-width:120px" />
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-header"><div class="card-title">Safety & Behavior</div></div>
      <div class="card-content">
        <div class="setting-row">
          <div class="setting-info">
            <div class="setting-name">"Please" Mode</div>
            <div class="setting-desc">Kids must say "please" to get a drawing</div>
          </div>
          <button class="toggle" id="pleaseToggle" onclick="togglePlease()"></button>
        </div>
        <div class="setting-row">
          <div class="setting-info">
            <div class="setting-name">Safety Filter</div>
            <div class="setting-desc">Word blocklist. When off, the AI prompt still instructs child-safe output.</div>
          </div>
          <button class="toggle" id="safetyToggle" onclick="toggleSafety()"></button>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-header"><div class="card-title">API Keys</div></div>
      <div class="card-content">
        <div class="form-hint" style="margin-bottom:12px">Keys are stored on the Pi in ~/.drawbox/api_keys.json. Leave a field empty to keep the current key.</div>
        <div class="form-group">
          <label class="form-label">OpenAI API Key</label>
          <input class="input" type="password" id="keyOpenai" placeholder="sk-..." autocomplete="off" />
          <div class="form-hint" id="keyOpenaiHint"></div>
        </div>
        <div class="form-group">
          <label class="form-label">Replicate API Token</label>
          <input class="input" type="password" id="keyReplicate" placeholder="r8_..." autocomplete="off" />
          <div class="form-hint" id="keyReplicateHint"></div>
        </div>
        <div class="form-group">
          <label class="form-label">Gemini API Key</label>
          <input class="input" type="password" id="keyGemini" placeholder="AI..." autocomplete="off" />
          <div class="form-hint" id="keyGeminiHint"></div>
        </div>
        <button class="btn btn-primary" onclick="saveApiKeys()">Save API Keys</button>
      </div>
    </div>

    <div class="btn-row" style="margin-top:16px">
      <button class="btn btn-primary" onclick="saveSettings()">Save Settings</button>
      <button class="btn btn-outline" onclick="loadSettings()">Reset</button>
    </div>
  </div>

  <!-- ═══ WIFI ═══ -->
  <div class="page" id="page-wifi">
    <div class="page-header">
      <h1 class="page-title">WiFi</h1>
      <p class="page-desc">Manage wireless network connections</p>
    </div>
    <div class="card">
      <div class="card-header" style="display:flex;align-items:center;justify-content:space-between">
        <div>
          <div class="card-title">Networks</div>
          <div class="card-desc" id="wifiCurrent">Click scan to find networks</div>
        </div>
        <button class="btn btn-outline btn-sm" onclick="loadWifi()">Scan</button>
      </div>
      <div class="card-content" id="wifiList" style="display:none"></div>
    </div>
    <div class="card" id="wifiConnectCard" style="display:none">
      <div class="card-header">
        <div class="card-title">Connect to <span id="wifiSSID"></span></div>
      </div>
      <div class="card-content">
        <div class="gen-form">
          <input class="input" type="password" id="wifiPass" placeholder="Password (leave empty for open)" />
          <button class="btn btn-primary btn-sm" onclick="connectWifi()">Connect</button>
        </div>
      </div>
    </div>
  </div>

  <!-- ═══ DIAGNOSTICS ═══ -->
  <div class="page" id="page-diagnostics">
    <div class="page-header">
      <h1 class="page-title">Diagnostics</h1>
      <p class="page-desc">Run system diagnostic commands</p>
    </div>
    <div class="card">
      <div class="card-content">
        <div class="diag-grid">
          <button class="btn btn-outline btn-sm" onclick="runDiag('printer_status')">Printer Status</button>
          <button class="btn btn-outline btn-sm" onclick="runDiag('printer_queue')">Print Queue</button>
          <button class="btn btn-outline btn-sm" onclick="runDiag('audio_inputs')">Audio Inputs</button>
          <button class="btn btn-outline btn-sm" onclick="runDiag('audio_outputs')">Audio Outputs</button>
          <button class="btn btn-outline btn-sm" onclick="runDiag('service_status')">Service Status</button>
          <button class="btn btn-outline btn-sm" onclick="runDiag('web_service')">Web Service</button>
          <button class="btn btn-outline btn-sm" onclick="runDiag('disk_usage')">Disk Usage</button>
          <button class="btn btn-outline btn-sm" onclick="runDiag('temperature')">Temperature</button>
          <button class="btn btn-outline btn-sm" onclick="runDiag('wifi_status')">WiFi Status</button>
          <button class="btn btn-outline btn-sm" onclick="runDiag('wifi_list')">WiFi Networks</button>
          <button class="btn btn-outline btn-sm" onclick="runDiag('rpi_connect')">RPi Connect</button>
          <button class="btn btn-outline btn-sm" onclick="runDiag('journal_errors')">Error Logs</button>
          <button class="btn btn-outline btn-sm" onclick="runDiag('cups_log')">CUPS Log</button>
        </div>
        <div class="diag-out" id="diagOut">Click a button above to run a diagnostic command.</div>
      </div>
    </div>
  </div>

  <!-- ═══ UPDATE ═══ -->
  <div class="page" id="page-update">
    <div class="page-header">
      <h1 class="page-title">Software Update</h1>
      <p class="page-desc">Pull the latest code from GitHub and apply it to this DrawBox</p>
    </div>
    <div class="card">
      <div class="card-header" style="display:flex;align-items:center;justify-content:space-between">
        <div>
          <div class="card-title">Current Version</div>
          <div class="card-desc" id="updateSha">Not checked yet</div>
        </div>
        <button class="btn btn-outline btn-sm" id="updateCheckBtn" onclick="checkUpdate()">Check for Updates</button>
      </div>
      <div class="card-content">
        <div id="updateStatus" style="margin-bottom:12px">
          <span class="badge badge-default">Press "Check for Updates" to see if a newer version is available</span>
        </div>
        <div id="updateDetails" style="display:none">
          <div id="updateCommits" style="font-size:14px;margin-bottom:12px"></div>
          <button class="btn btn-primary" id="deployBtn" onclick="deployUpdate()">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg>
            Install Update &amp; Restart
          </button>
          <div class="form-hint" style="margin-top:8px">Downloads the latest code, copies files, and restarts both services. The dashboard will reload automatically.</div>
        </div>
        <div class="diag-out" id="updateLog" style="display:none;margin-top:12px;height:200px"></div>
      </div>
    </div>
  </div>

  <!-- ═══ SYSTEM ═══ -->
  <div class="page" id="page-system">
    <div class="page-header">
      <h1 class="page-title">System</h1>
      <p class="page-desc">Service control and power management</p>
    </div>
    <div class="card">
      <div class="card-header"><div class="card-title">DrawBox Service</div></div>
      <div class="card-content">
        <div class="btn-row">
          <button class="btn btn-success btn-sm" onclick="svcAction('restart')">Restart</button>
          <button class="btn btn-outline btn-sm" onclick="svcAction('start')">Start</button>
          <button class="btn btn-outline btn-sm" onclick="svcAction('stop')">Stop</button>
        </div>
      </div>
    </div>
    <div class="card">
      <div class="card-header"><div class="card-title">Power</div></div>
      <div class="card-content">
        <button class="btn btn-destructive btn-sm" onclick="if(confirm('Reboot the Raspberry Pi?'))rebootPi()">Reboot Pi</button>
        <div class="form-hint" style="margin-top:8px">This will restart the entire Raspberry Pi. The dashboard will be unavailable for about a minute.</div>
      </div>
    </div>
  </div>

</main>

<div class="toast" id="toast"></div>

<script>
// ── GLOBALS ───────────────────────────────────
const $ = id => document.getElementById(id);
let evtSrc = null, wifiTarget = '';

// ── TOAST ─────────────────────────────────────
function toast(msg, ok=true) {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast show ' + (ok ? 'toast-ok' : 'toast-err');
  setTimeout(() => t.classList.remove('show'), 3000);
}

// ── PAGE SWITCHING ────────────────────────────
function showPage(page) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  $('page-' + page).classList.add('active');
  document.querySelectorAll('.sidebar-item').forEach(item => {
    item.classList.toggle('active', item.dataset.page === page);
  });
  $('sidebar').classList.remove('open');
  $('sidebarOverlay').classList.remove('show');
  if (page === 'overview') loadAnalytics();
  if (page === 'settings') { loadSettings(); loadPleaseMode(); loadSafetyMode(); loadApiKeys(); }
}

function toggleSidebar() {
  $('sidebar').classList.toggle('open');
  $('sidebarOverlay').classList.toggle('show');
}

// ── STATUS ────────────────────────────────────
async function refreshStatus() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    const status = d.service_running;
    $('ovStatus').innerHTML = status
      ? '<span class="dot dot-green"></span>Running'
      : '<span class="dot dot-red"></span>Stopped';
    $('ovTemp').textContent = d.temperature || '--';
    // Shorten uptime: "1 day, 18 hours, 20 minutes" → "1d 18h 20m"
    const ut = (d.uptime || '--')
      .replace(/(\d+)\s*days?/i, '$1d')
      .replace(/(\d+)\s*hours?/i, '$1h')
      .replace(/(\d+)\s*minutes?/i, '$1m')
      .replace(/,\s*/g, ' ');
    $('ovUptime').textContent = ut;
  } catch(e) {}
}
refreshStatus();
setInterval(refreshStatus, 15000);

// ── ANALYTICS ─────────────────────────────────
async function loadAnalytics() {
  try {
    const r = await fetch('/api/analytics');
    const d = await r.json();

    // Stats
    $('ovTotal').textContent = d.total_prints;
    $('ovToday').textContent = d.prints_today > 0
      ? '+' + d.prints_today + ' today'
      : 'None today';
    $('ovToday').className = 'stat-sub' + (d.prints_today > 0 ? ' up' : '');
    $('ovAvgDuration').textContent = d.avg_duration > 0
      ? 'Avg: ' + d.avg_duration + 's'
      : '--';

    // Model chart
    const mc = $('modelChart');
    const entries = Object.entries(d.model_counts || {});
    const total = entries.reduce((a, [,c]) => a + c, 0) || 1;
    const colors = {'flux-schnell':'var(--chart-1)','nano-banana':'var(--chart-2)','gpt-image':'var(--chart-3)'};
    if (entries.length === 0) {
      mc.innerHTML = '<div style="color:var(--muted-foreground);font-size:13px">No data yet</div>';
    } else {
      mc.innerHTML = entries.map(([m, c]) =>
        '<div class="bar-row">' +
          '<span class="bar-label">' + m + '</span>' +
          '<div class="bar-track"><div class="bar-fill" style="width:' + (c/total*100).toFixed(1) + '%;background:' + (colors[m]||'var(--chart-4)') + '"></div></div>' +
          '<span class="bar-value">' + c + '</span>' +
        '</div>'
      ).join('');
    }

    // Top prompts
    const tp = $('topPrompts');
    if (d.top_prompts && d.top_prompts.length) {
      tp.innerHTML = d.top_prompts.map((p, i) =>
        '<div style="display:flex;align-items:center;gap:12px;padding:6px 0;' + (i < d.top_prompts.length-1 ? 'border-bottom:1px solid var(--border)' : '') + '">' +
          '<span style="font-size:12px;color:var(--muted-foreground);width:20px">' + (i+1) + '</span>' +
          '<span style="font-size:13px;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + esc(p.prompt) + '</span>' +
          '<span style="font-size:13px;font-weight:600">' + p.count + '</span>' +
        '</div>'
      ).join('');
    } else {
      tp.innerHTML = '<div style="color:var(--muted-foreground);font-size:13px">No data yet</div>';
    }

    // Recent
    const tb = $('recentBody');
    if (d.recent && d.recent.length) {
      tb.innerHTML = d.recent.map(r =>
        '<tr><td class="muted">' + fmtTime(r.ts) + '</td>' +
        '<td>' + esc(r.prompt) + '</td>' +
        '<td><span class="badge badge-default">' + r.model + '</span></td>' +
        '<td class="muted">' + (r.duration_s ? r.duration_s.toFixed(1) + 's' : '--') + '</td></tr>'
      ).join('');
    } else {
      tb.innerHTML = '<tr><td colspan="4" class="muted" style="text-align:center;padding:20px">No prints yet. Press the button or use Generate!</td></tr>';
    }
  } catch(e) { console.error('Analytics error:', e); }
}

function fmtTime(iso) {
  const d = new Date(iso), n = new Date();
  if (d.toDateString() === n.toDateString())
    return d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
  return d.toLocaleDateString([], {month:'short',day:'numeric'}) + ' ' +
         d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
}

function esc(s) {
  const d = document.createElement('div'); d.textContent = s; return d.innerHTML;
}

// ── GENERATE ──────────────────────────────────
async function doGenerate() {
  const desc = $('genInput').value.trim();
  if (!desc) { toast('Type a description first', false); return; }
  const btn = $('genBtn');
  btn.disabled = true; btn.innerHTML = 'Generating...';
  try {
    const r = await fetch('/api/generate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({description: desc})
    });
    const d = await r.json();
    if (d.ok) {
      toast('Coloring page sent to printer!');
      if (d.image) {
        $('previewImg').src = 'data:image/png;base64,' + d.image;
        $('previewImg').style.display = 'block';
      }
      $('genInput').value = '';
    } else { toast(d.error || 'Generation failed', false); }
  } catch(e) { toast('Network error: ' + e.message, false); }
  btn.disabled = false;
  btn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 9l6-6 6 6"/><path d="M12 3v14"/><path d="M5 21h14"/></svg> Generate & Print';
}
$('genInput').addEventListener('keydown', e => { if (e.key === 'Enter') doGenerate(); });

// ── LOGS (SSE) ────────────────────────────────
function connectLogs() {
  if (evtSrc) evtSrc.close();
  evtSrc = new EventSource('/api/logs');
  evtSrc.onmessage = e => {
    $('logArea').textContent += e.data + '\\n';
    $('logArea').scrollTop = $('logArea').scrollHeight;
  };
  evtSrc.onerror = () => {
    $('logArea').textContent += '[log stream disconnected — reconnecting...]\\n';
  };
}
connectLogs();
function clearLog() { $('logArea').textContent = ''; }

// ── SETTINGS ──────────────────────────────────
async function loadSettings() {
  try {
    const r = await fetch('/api/settings');
    const d = await r.json();
    $('cfgPrompt').value = d.coloring_prompt || '';
    $('cfgModel').value = d.image_model || 'nano-banana';
    $('cfgVoice').value = d.tts_voice || 'nova';
    $('cfgRecSec').value = d.record_seconds || 10;
  } catch(e) {}
}

async function saveSettings() {
  const data = {
    coloring_prompt: $('cfgPrompt').value,
    image_model: $('cfgModel').value,
    tts_voice: $('cfgVoice').value,
    record_seconds: parseInt($('cfgRecSec').value) || 10
  };
  try {
    const r = await fetch('/api/settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data)
    });
    const d = await r.json();
    if (d.ok) toast('Settings saved!');
    else toast('Save failed', false);
  } catch(e) { toast('Error: ' + e.message, false); }
}

// ── API KEYS ──────────────────────────────────
async function loadApiKeys() {
  try {
    const r = await fetch('/api/keys');
    const d = await r.json();
    $('keyOpenaiHint').textContent = d.openai ? 'Current: ' + d.openai : 'Not set';
    $('keyReplicateHint').textContent = d.replicate ? 'Current: ' + d.replicate : 'Not set';
    $('keyGeminiHint').textContent = d.gemini ? 'Current: ' + d.gemini : 'Not set';
    $('keyOpenai').value = '';
    $('keyReplicate').value = '';
    $('keyGemini').value = '';
  } catch(e) {}
}

async function saveApiKeys() {
  const data = {};
  if ($('keyOpenai').value.trim()) data.openai = $('keyOpenai').value.trim();
  if ($('keyReplicate').value.trim()) data.replicate = $('keyReplicate').value.trim();
  if ($('keyGemini').value.trim()) data.gemini = $('keyGemini').value.trim();
  if (!Object.keys(data).length) { toast('No keys entered', false); return; }
  try {
    const r = await fetch('/api/keys', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data)
    });
    const d = await r.json();
    if (d.ok) { toast('API keys saved!'); loadApiKeys(); }
    else toast('Save failed', false);
  } catch(e) { toast('Error: ' + e.message, false); }
}

// ── PLEASE MODE ───────────────────────────────
async function loadPleaseMode() {
  try {
    const r = await fetch('/api/please-mode');
    const d = await r.json();
    $('pleaseToggle').className = 'toggle' + (d.enabled ? ' on' : '');
  } catch(e) {}
}

async function togglePlease() {
  try {
    const r = await fetch('/api/please-mode');
    const d = await r.json();
    await fetch('/api/please-mode', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled: !d.enabled})
    });
    loadPleaseMode();
    toast(d.enabled ? '"Please" mode disabled' : '"Please" mode enabled!');
  } catch(e) { toast('Error: ' + e.message, false); }
}

// ── SAFETY MODE ───────────────────────────────
async function loadSafetyMode() {
  try {
    const r = await fetch('/api/safety-mode');
    const d = await r.json();
    $('safetyToggle').className = 'toggle' + (d.enabled ? ' on' : '');
  } catch(e) {}
}

async function toggleSafety() {
  try {
    const r = await fetch('/api/safety-mode');
    const d = await r.json();
    if (d.enabled && !confirm('Turn OFF the safety word filter?\\nThe AI prompt still instructs child-safe output, but blocked words will be allowed.')) return;
    await fetch('/api/safety-mode', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled: !d.enabled})
    });
    loadSafetyMode();
    toast(d.enabled ? 'Safety filter disabled' : 'Safety filter enabled!');
  } catch(e) { toast('Error: ' + e.message, false); }
}

// ── WIFI ──────────────────────────────────────
async function loadWifi() {
  $('wifiCurrent').textContent = 'Scanning...';
  try {
    const sr = await fetch('/api/diagnostics', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({command: 'wifi_status'})
    });
    const sd = await sr.json();
    const cur = (sd.output || '').split('\\n').find(l => l.startsWith('yes'));
    $('wifiCurrent').textContent = cur ? 'Connected: ' + cur.split(':')[1] : 'Not connected';

    const r = await fetch('/api/wifi/networks');
    const d = await r.json();
    const wl = $('wifiList');
    if (!d.networks || d.networks.length === 0) {
      wl.innerHTML = '<div style="color:var(--muted-foreground);font-size:13px;padding:8px 0">No networks found</div>';
    } else {
      wl.innerHTML = d.networks.map(n =>
        '<div class="wifi-network" onclick="selectWifi(\\'' + n.ssid.replace(/'/g, "\\\\'") + '\\')">' +
          '<span class="wifi-ssid">' + esc(n.ssid) + '</span>' +
          '<span class="wifi-meta">' + n.signal + '% &middot; ' + n.security + '</span>' +
        '</div>'
      ).join('');
    }
    wl.style.display = 'block';
  } catch(e) { $('wifiCurrent').textContent = 'Error: ' + e.message; }
}

function selectWifi(ssid) {
  wifiTarget = ssid;
  $('wifiSSID').textContent = ssid;
  $('wifiConnectCard').style.display = 'block';
  $('wifiPass').value = '';
  $('wifiPass').focus();
}

async function connectWifi() {
  if (!wifiTarget) return;
  toast('Connecting to ' + wifiTarget + '...');
  try {
    const r = await fetch('/api/wifi/connect', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ssid: wifiTarget, password: $('wifiPass').value})
    });
    const d = await r.json();
    if (d.ok) {
      toast('Connected to ' + wifiTarget);
      $('wifiConnectCard').style.display = 'none';
      loadWifi();
    } else { toast(d.error || 'Failed to connect', false); }
  } catch(e) { toast('Error: ' + e.message, false); }
}

// ── DIAGNOSTICS ───────────────────────────────
async function runDiag(cmd) {
  const out = $('diagOut');
  out.textContent = 'Running ' + cmd + '...';
  try {
    const r = await fetch('/api/diagnostics', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({command: cmd})
    });
    const d = await r.json();
    out.textContent = d.output || d.error || 'No output';
  } catch(e) { out.textContent = 'Error: ' + e.message; }
}

// ── SOFTWARE UPDATE ───────────────────────────
async function checkUpdate() {
  const btn = $('updateCheckBtn');
  btn.disabled = true; btn.textContent = 'Checking...';
  try {
    const r = await fetch('/api/update/check');
    const d = await r.json();
    if (d.error) {
      $('updateStatus').innerHTML = '<span class="badge badge-destructive">' + esc(d.error) + '</span>';
      $('updateDetails').style.display = 'none';
      btn.disabled = false; btn.textContent = 'Check for Updates';
      return;
    }
    $('updateSha').textContent = 'Current: ' + d.local_sha.substring(0, 7);
    if (d.has_update) {
      $('updateStatus').innerHTML = '<span class="badge badge-warning">' + d.commits_behind + ' update' + (d.commits_behind > 1 ? 's' : '') + ' available</span>';
      $('updateCommits').innerHTML =
        '<p style="margin-bottom:8px">Latest: <strong>' + esc(d.latest_message) + '</strong></p>' +
        '<p style="font-size:13px;color:var(--muted-foreground)">Remote: ' + d.remote_sha.substring(0, 7) + '</p>';
      $('updateDetails').style.display = 'block';
    } else {
      $('updateStatus').innerHTML = '<span class="badge badge-success">Up to date</span>';
      $('updateDetails').style.display = 'none';
    }
  } catch(e) { toast('Update check failed: ' + e.message, false); }
  btn.disabled = false; btn.textContent = 'Check for Updates';
}

async function deployUpdate() {
  if (!confirm('Install the update and restart DrawBox services? The dashboard will reload automatically when ready.')) return;
  const btn = $('deployBtn');
  const log = $('updateLog');
  btn.disabled = true; btn.textContent = 'Installing...';
  log.style.display = 'block';
  log.textContent = 'Starting deployment...\\n';
  try {
    const r = await fetch('/api/update/deploy', {method: 'POST'});
    const d = await r.json();
    if (d.ok) {
      log.textContent += d.output + '\\n\\nService restarting... waiting for it to come back.\\n';
      pollUntilAlive();
    } else {
      log.textContent += 'ERROR: ' + (d.error || 'Unknown error') + '\\n';
      btn.disabled = false; btn.textContent = 'Install Update & Restart';
    }
  } catch(e) {
    log.textContent += 'Connection lost (expected during restart).\\nWaiting for service to come back...\\n';
    pollUntilAlive();
  }
}

function pollUntilAlive(attempts = 0) {
  if (attempts > 30) {
    $('updateLog').textContent += 'Service did not come back after 60s. Check manually.\\n';
    return;
  }
  setTimeout(async () => {
    try {
      const r = await fetch('/api/status', {signal: AbortSignal.timeout(3000)});
      if (r.ok) {
        $('updateLog').textContent += 'Service is back online! Reloading...\\n';
        setTimeout(() => window.location.reload(), 1000);
      } else { pollUntilAlive(attempts + 1); }
    } catch(e) { pollUntilAlive(attempts + 1); }
  }, 2000);
}

// ── SERVICE CONTROL ───────────────────────────
async function svcAction(action) {
  try {
    const r = await fetch('/api/service/' + action, {method: 'POST'});
    const d = await r.json();
    toast(d.message || (action + ' done'));
    setTimeout(refreshStatus, 2000);
  } catch(e) { toast('Error: ' + e.message, false); }
}

async function rebootPi() {
  try {
    await fetch('/api/reboot', {method: 'POST'});
    toast('Rebooting Pi...');
  } catch(e) { toast('Error: ' + e.message, false); }
}

// ── INIT ──────────────────────────────────────
loadAnalytics();
loadSettings();
loadPleaseMode();
loadSafetyMode();
loadApiKeys();
</script>
</body>
</html>"""

# ── ROUTES ───────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/guide")
def guide():
    if GUIDE_PATH.exists():
        return send_file(GUIDE_PATH)
    return "Guide not found. Copy drawbox-guide.html to ~/", 404

@app.route("/simulator")
def simulator():
    if SIMULATOR_PATH.exists():
        return send_file(SIMULATOR_PATH)
    return "Simulator not found. Copy drawbox-simulator.html to ~/", 404

@app.route("/api/status")
def api_status():
    # Service status
    svc = subprocess.run(["systemctl", "is-active", "drawbox"],
                         capture_output=True, text=True)
    running = svc.stdout.strip() == "active"

    # Temperature
    temp = "--"
    try:
        t = subprocess.run(["vcgencmd", "measure_temp"],
                           capture_output=True, text=True, timeout=5)
        temp = t.stdout.strip().replace("temp=", "")
    except Exception:
        pass

    # Uptime
    up = "--"
    try:
        u = subprocess.run(["uptime", "-p"],
                           capture_output=True, text=True, timeout=5)
        up = u.stdout.strip().replace("up ", "")
    except Exception:
        pass

    # RPi Connect
    rpi = "unknown"
    try:
        rc = subprocess.run(["rpi-connect", "status"],
                            capture_output=True, text=True, timeout=5)
        out = rc.stdout.strip().lower()
        if "connected" in out:
            rpi = "connected"
        elif "not" in out or rc.returncode != 0:
            rpi = "off"
        else:
            rpi = out[:20]
    except FileNotFoundError:
        rpi = "not installed"
    except Exception:
        pass

    return jsonify(service_running=running, temperature=temp,
                   uptime=up, rpi_connect=rpi)

@app.route("/api/generate", methods=["POST"])
def api_generate():
    global is_generating, last_image_b64
    if is_generating:
        return jsonify(ok=False, error="Already generating — please wait.")

    data = request.get_json() or {}
    desc = (data.get("description") or "").strip()
    if not desc:
        return jsonify(ok=False, error="Please describe what to draw.")
    if len(desc) > 500:
        return jsonify(ok=False, error="Description too long (max 500 chars).")
    if SAFETY_MODE_FILE.exists() and not is_safe(desc):
        return jsonify(ok=False,
            error="That description contains blocked words. "
                  "Try something fun like an animal or a rainbow!")

    is_generating = True
    try:
        t0 = _time.time()
        path = generate_image(desc)
        duration = _time.time() - t0
        # Read image for preview before printing
        with open(path, "rb") as f:
            last_image_b64 = base64.b64encode(f.read()).decode()
        print_image(path)
        # Log analytics
        settings = load_settings()
        log_print_event(desc, settings.get("image_model", IMAGE_MODEL), duration)
        return jsonify(ok=True, image=last_image_b64)
    except Exception as e:
        return jsonify(ok=False, error=str(e))
    finally:
        is_generating = False

@app.route("/api/logs")
def api_logs():
    def generate():
        proc = subprocess.Popen(
            ["journalctl", "-u", "drawbox", "-f", "-n", "50", "--no-pager"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            for line in proc.stdout:
                yield f"data: {line.rstrip()}\n\n"
        except GeneratorExit:
            proc.kill()
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})

@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "GET":
        return jsonify(load_settings())
    data = request.get_json() or {}
    settings = load_settings()
    if "coloring_prompt" in data:
        settings["coloring_prompt"] = data["coloring_prompt"][:5000]
    if "image_model" in data and data["image_model"] in ("flux-schnell", "nano-banana", "gpt-image"):
        settings["image_model"] = data["image_model"]
    if "tts_voice" in data:
        settings["tts_voice"] = data["tts_voice"]
    if "record_seconds" in data:
        settings["record_seconds"] = max(3, min(30, int(data["record_seconds"])))
    save_settings(settings)
    return jsonify(ok=True)

@app.route("/api/please-mode", methods=["GET", "POST"])
def api_please_mode():
    if request.method == "GET":
        return jsonify(enabled=PLEASE_MODE_FILE.exists())
    data = request.get_json() or {}
    if data.get("enabled"):
        PLEASE_MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PLEASE_MODE_FILE.touch()
    else:
        PLEASE_MODE_FILE.unlink(missing_ok=True)
    return jsonify(ok=True, enabled=PLEASE_MODE_FILE.exists())

@app.route("/api/safety-mode", methods=["GET", "POST"])
def api_safety_mode():
    if request.method == "GET":
        return jsonify(enabled=SAFETY_MODE_FILE.exists())
    data = request.get_json() or {}
    if data.get("enabled"):
        SAFETY_MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
        SAFETY_MODE_FILE.touch()
    else:
        SAFETY_MODE_FILE.unlink(missing_ok=True)
    return jsonify(ok=True, enabled=SAFETY_MODE_FILE.exists())

@app.route("/api/keys", methods=["GET", "POST"])
def api_keys():
    if request.method == "GET":
        keys = _load_api_keys()
        # Return masked versions so we don't expose full keys in the browser
        return jsonify({
            k: ("" if not v else v[:4] + "..." + v[-4:] if len(v) > 12 else "****")
            for k, v in keys.items()
        })
    data = request.get_json() or {}
    # Load existing keys, only update the ones that were sent
    try:
        existing = json.loads(API_KEYS_FILE.read_text()) if API_KEYS_FILE.exists() else {}
    except Exception:
        existing = {}
    for k in ("openai", "replicate", "gemini"):
        val = data.get(k, "").strip()
        if val:  # only overwrite if a new value was provided
            existing[k] = val
    API_KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    API_KEYS_FILE.write_text(json.dumps(existing, indent=2))
    _apply_api_keys()
    return jsonify(ok=True)

@app.route("/api/wifi/networks")
def api_wifi_networks():
    try:
        r = subprocess.run(
            ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"],
            capture_output=True, text=True, timeout=15)
        networks = []
        seen = set()
        for line in r.stdout.strip().splitlines():
            parts = line.split(":")
            if len(parts) >= 3 and parts[0] and parts[0] not in seen:
                seen.add(parts[0])
                networks.append({
                    "ssid": parts[0],
                    "signal": int(parts[1]) if parts[1].isdigit() else 0,
                    "security": parts[2] or "Open"
                })
        networks.sort(key=lambda n: n["signal"], reverse=True)
        return jsonify(networks=networks)
    except Exception as e:
        return jsonify(networks=[], error=str(e))

@app.route("/api/wifi/connect", methods=["POST"])
def api_wifi_connect():
    data = request.get_json() or {}
    ssid = (data.get("ssid") or "").strip()
    password = (data.get("password") or "").strip()
    if not ssid:
        return jsonify(ok=False, error="SSID required")
    try:
        cmd = ["sudo", "nmcli", "dev", "wifi", "connect", ssid]
        if password:
            cmd += ["password", password]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return jsonify(ok=True, message=f"Connected to {ssid}")
        else:
            return jsonify(ok=False, error=r.stderr.strip() or r.stdout.strip())
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@app.route("/api/diagnostics", methods=["POST"])
def api_diagnostics():
    data = request.get_json() or {}
    cmd_key = data.get("command", "")
    if cmd_key not in DIAGNOSTIC_COMMANDS:
        return jsonify(error="Unknown command: " + cmd_key)
    cmd = DIAGNOSTIC_COMMANDS[cmd_key]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        output = r.stdout
        if r.stderr:
            output += "\n" + r.stderr
        return jsonify(output=output.strip() or "(no output)")
    except FileNotFoundError:
        return jsonify(output=f"Command not found: {cmd[0]}")
    except subprocess.TimeoutExpired:
        return jsonify(output="Command timed out after 15 seconds.")
    except Exception as e:
        return jsonify(error=str(e))

@app.route("/api/service/<action>", methods=["POST"])
def api_service(action):
    if action not in ("restart", "stop", "start"):
        return jsonify(ok=False, message="Invalid action"), 400
    try:
        subprocess.run(["sudo", "systemctl", action, "drawbox"],
                       check=True, timeout=30)
        return jsonify(ok=True,
                       message=f"DrawBox service {action}ed successfully.")
    except Exception as e:
        return jsonify(ok=False, message=str(e))

@app.route("/api/reboot", methods=["POST"])
def api_reboot():
    try:
        subprocess.Popen(["sudo", "reboot"])
        return jsonify(ok=True, message="Rebooting...")
    except Exception as e:
        return jsonify(ok=False, message=str(e))

# ── ANALYTICS ────────────────────────────────────

@app.route("/api/analytics")
def api_analytics():
    events = []
    if PRINT_LOG_FILE.exists():
        try:
            for line in PRINT_LOG_FILE.read_text().strip().splitlines():
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue
        except Exception:
            pass

    total = len(events)
    today = datetime.now().date().isoformat()
    prints_today = sum(1 for e in events if e.get("ts", "").startswith(today))

    # Model counts
    model_counts = {}
    for e in events:
        m = e.get("model", "unknown")
        model_counts[m] = model_counts.get(m, 0) + 1

    # Average generation time
    durations = [e["duration_s"] for e in events if "duration_s" in e]
    avg_duration = round(sum(durations) / len(durations), 1) if durations else 0

    # Top prompts
    prompt_counts = Counter()
    for e in events:
        p = e.get("prompt", "").strip().lower()
        if p:
            prompt_counts[p] += 1
    top_prompts = [{"prompt": p, "count": c} for p, c in prompt_counts.most_common(8)]

    # Recent (last 10, newest first)
    recent = list(reversed(events[-10:]))

    return jsonify(
        total_prints=total,
        prints_today=prints_today,
        model_counts=model_counts,
        avg_duration=avg_duration,
        top_prompts=top_prompts,
        recent=recent,
    )

# ── SOFTWARE UPDATE ──────────────────────────────

@app.route("/api/update/check")
def api_update_check():
    if not REPO_DIR.exists():
        return jsonify(error="Software updates not set up yet. Re-run deploy-web.sh from your Mac to set it up.",
                       has_update=False)
    try:
        subprocess.run(["git", "fetch", "origin"], cwd=str(REPO_DIR),
                       capture_output=True, timeout=30)
        local = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO_DIR),
                               capture_output=True, text=True).stdout.strip()
        remote = subprocess.run(["git", "rev-parse", "origin/main"],
                                cwd=str(REPO_DIR),
                                capture_output=True, text=True).stdout.strip()
        behind = subprocess.run(
            ["git", "rev-list", "--count", f"{local}..{remote}"],
            cwd=str(REPO_DIR), capture_output=True, text=True
        ).stdout.strip()
        behind = int(behind) if behind.isdigit() else 0
        msg = ""
        if behind > 0:
            msg = subprocess.run(
                ["git", "log", "-1", "--format=%s", "origin/main"],
                cwd=str(REPO_DIR), capture_output=True, text=True
            ).stdout.strip()
        return jsonify(
            has_update=behind > 0,
            local_sha=local,
            remote_sha=remote,
            commits_behind=behind,
            latest_message=msg,
        )
    except Exception as e:
        return jsonify(error=str(e), has_update=False)

@app.route("/api/update/deploy", methods=["POST"])
def api_update_deploy():
    if not REPO_DIR.exists():
        return jsonify(ok=False, error="No repo found")
    try:
        pull = subprocess.run(["git", "pull", "origin", "main"],
                              cwd=str(REPO_DIR),
                              capture_output=True, text=True, timeout=60)
        output = pull.stdout + "\n" + pull.stderr

        home = Path.home()
        files_to_copy = ["drawbox.py", "drawbox_web.py",
                         "drawbox-guide.html", "drawbox-simulator.html",
                         "check.sh"]
        for fname in files_to_copy:
            src = REPO_DIR / fname
            if src.exists():
                shutil.copy2(str(src), str(home / fname))
                output += f"\nCopied {fname} to ~/"

        # Schedule service restart (runs after response is sent)
        def restart_later():
            _time.sleep(1)
            subprocess.run(["sudo", "systemctl", "restart", "drawbox-web"],
                           capture_output=True)
            subprocess.run(["sudo", "systemctl", "restart", "drawbox"],
                           capture_output=True)

        threading.Thread(target=restart_later, daemon=True).start()

        return jsonify(ok=True, output=output.strip())
    except Exception as e:
        return jsonify(ok=False, error=str(e))

# ── INIT ─────────────────────────────────────────
SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
# Safety mode ON by default (create sentinel file if missing)
if not SAFETY_MODE_FILE.exists():
    SAFETY_MODE_FILE.touch()

if __name__ == "__main__":
    if not OPENAI_API_KEY:
        print("Warning: OPENAI_API_KEY not set. Voice features won't work.")
    print(f"   Image model: {IMAGE_MODEL}")
    print(f"   Safety filter: {'ON' if SAFETY_MODE_FILE.exists() else 'OFF'}")
    print("DrawBox Web Dashboard starting on http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
