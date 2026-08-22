"""DrawBox core — shared logic for the button script and the web dashboard.

This module owns configuration on disk (API keys, settings, scripts, sentinels),
the safety blocklist, image generation across three providers, image post-
processing, and analytics logging. Both ``drawbox.py`` and ``drawbox_web.py``
import from here so behavior stays consistent.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import subprocess
import tempfile
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.request import Request, urlopen

import replicate
from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("drawbox")

# ── FILE PATHS ────────────────────────────────────
DRAWBOX_DIR = Path.home() / ".drawbox"
API_KEYS_FILE = DRAWBOX_DIR / "api_keys.json"
SETTINGS_FILE = DRAWBOX_DIR / "web_settings.json"
PLEASE_MODE_FILE = DRAWBOX_DIR / "please_mode"
SAFETY_MODE_FILE = DRAWBOX_DIR / "safety_mode"
PRINT_LOG_FILE = DRAWBOX_DIR / "print_log.jsonl"
SCRIPTS_FILE = DRAWBOX_DIR / "voice_scripts.json"
CACHE_DIR = DRAWBOX_DIR / "voice_cache"
PAIRING_FILE = DRAWBOX_DIR / "pairing.json"
PAIRED_DEVICES_FILE = DRAWBOX_DIR / "paired_devices.json"

# ── CONFIG ────────────────────────────────────────
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "nano-banana")
PRINTER_NAME = "drawbox-printer"

AI_GATEWAY_BASE_URL = "https://ai-gateway.vercel.sh/v1"

# Multimodal LLMs: the gateway serves these via /chat/completions, not
# /images/generations.
GATEWAY_CHAT_IMAGE_MODELS = frozenset({
    "google/gemini-2.5-flash-image",
    "google/gemini-3-pro-image",
    "google/gemini-3.1-flash-image",
    "google/gemini-3.1-flash-image-preview",
    "google/gemini-3.1-flash-lite-image",
})

# Snapshot of the image-output models from the AI Gateway catalog
# (GET https://ai-gateway.vercel.sh/v1/models, the models with image output
# modality; same set as https://vercel.com/ai-gateway/models?modality=image),
# snapshotted 2026-08-22.
GATEWAY_IMAGE_MODELS = (
    "google/gemini-2.5-flash-image",
    "google/gemini-3-pro-image",
    "google/gemini-3.1-flash-image",
    "google/gemini-3.1-flash-image-preview",
    "google/gemini-3.1-flash-lite-image",
    "bfl/flux-2-flex",
    "bfl/flux-2-klein-4b",
    "bfl/flux-2-klein-9b",
    "bfl/flux-2-max",
    "bfl/flux-2-pro",
    "bfl/flux-kontext-max",
    "bfl/flux-kontext-pro",
    "bfl/flux-pro-1.0-fill",
    "bfl/flux-pro-1.1",
    "bfl/flux-pro-1.1-ultra",
    "bytedance/seedream-4.0",
    "bytedance/seedream-4.5",
    "bytedance/seedream-5.0-lite",
    "bytedance/seedream-5.0-pro",
    "openai/gpt-image-1",
    "openai/gpt-image-1-mini",
    "openai/gpt-image-1.5",
    "openai/gpt-image-2",
    "prodia/flux-fast-schnell",
    "quiverai/arrow-1.1",
    "recraft/recraft-v2",
    "recraft/recraft-v3",
    "recraft/recraft-v4",
    "recraft/recraft-v4-pro",
    "recraft/recraft-v4.1",
    "recraft/recraft-v4.1-pro",
    "recraft/recraft-v4.1-utility",
    "recraft/recraft-v4.1-utility-pro",
    "spacexai/grok-imagine-image",
    "spacexai/grok-imagine-image-2.0",
)

SUPPORTED_MODELS = ("nano-banana", "flux-schnell", "gpt-image") + GATEWAY_IMAGE_MODELS

# ── API KEYS ──────────────────────────────────────
OPENAI_API_KEY = ""
REPLICATE_API_TOKEN = ""
GEMINI_API_KEY = ""
ELEVENLABS_API_KEY = ""
AI_GATEWAY_API_KEY = ""
XAI_API_KEY = ""
client = None  # OpenAI client, rebuilt on apply_api_keys()


API_KEY_NAMES = ("openai", "replicate", "gemini", "elevenlabs", "ai_gateway", "xai")
_API_KEY_ENV_VARS = {
    "openai": "OPENAI_API_KEY",
    "replicate": "REPLICATE_API_TOKEN",
    "gemini": "GEMINI_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
    "ai_gateway": "AI_GATEWAY_API_KEY",
    "xai": "XAI_API_KEY",
}


def _load_api_keys():
    """Read API keys from the on-disk file, falling back to environment variables."""
    keys = {}
    if API_KEYS_FILE.exists():
        try:
            keys = json.loads(API_KEYS_FILE.read_text())
        except (OSError, ValueError) as e:
            log.warning("could not read %s: %s", API_KEYS_FILE, e)
    return {
        name: keys.get(name) or os.environ.get(env_var) or ""
        for name, env_var in _API_KEY_ENV_VARS.items()
    }


def apply_api_keys():
    """Refresh module-level keys from disk/env and rebuild the OpenAI client."""
    global OPENAI_API_KEY, REPLICATE_API_TOKEN, GEMINI_API_KEY, ELEVENLABS_API_KEY, \
        AI_GATEWAY_API_KEY, XAI_API_KEY, client
    keys = _load_api_keys()
    OPENAI_API_KEY = keys["openai"]
    REPLICATE_API_TOKEN = keys["replicate"]
    GEMINI_API_KEY = keys["gemini"]
    ELEVENLABS_API_KEY = keys["elevenlabs"]
    AI_GATEWAY_API_KEY = keys["ai_gateway"]
    XAI_API_KEY = keys["xai"]
    if REPLICATE_API_TOKEN:
        os.environ["REPLICATE_API_TOKEN"] = REPLICATE_API_TOKEN
    client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


apply_api_keys()


def mask_key(key, head=4, tail=0):
    """Return a short masked preview of a secret for logging/UI hints."""
    if not key:
        return ""
    if len(key) <= head + tail + 2:
        return "****"
    return key[:head] + "…" + (key[-tail:] if tail else "")


# ── SAFETY BLOCKLIST ──────────────────────────────
BLOCKED_WORDS = frozenset({
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
})

_WORD_RE = re.compile(r"[a-z]+")
POOP_WORDS = frozenset({"poop", "poops", "pooped", "pooping", "poopy"})


def is_safe(text):
    """Return True iff ``text`` contains no blocked word.

    Uses Unicode-aware word splitting (regex over ASCII letters) so punctuation
    like commas, apostrophes, or dashes can't sneak a blocked word through
    (``"kill,it"`` is correctly detected).
    """
    if not text:
        return True
    tokens = set(_WORD_RE.findall(text.lower()))
    return not tokens & BLOCKED_WORDS


def contains_poop(text):
    """Return True when text contains a whole-word poop-family token."""
    if not text:
        return False
    tokens = set(_WORD_RE.findall(text.lower()))
    return bool(tokens & POOP_WORDS)


def normalize_voice_command(text):
    """Normalize a transcript for strict voice-command matching."""
    tokens = _WORD_RE.findall((text or "").lower())
    return " ".join(tokens)


def parse_admin_poop_command(text):
    """Return 'enable'/'disable' for exact admin poop commands, else None."""
    normalized = normalize_voice_command(text)
    if normalized == "admin mode enable poop mode":
        return "enable"
    if normalized == "admin mode disable poop mode":
        return "disable"
    return None


def safety_mode_enabled():
    return SAFETY_MODE_FILE.exists()


def ensure_safety_mode_default():
    """Default-on opt-out: create the sentinel on first run if it does not exist."""
    SAFETY_MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not SAFETY_MODE_FILE.exists():
        SAFETY_MODE_FILE.touch()


def please_mode_enabled():
    return PLEASE_MODE_FILE.exists()


_PLEASE_PHRASES = (
    "please", "svp",
    "s'il vous plait", "s'il te plait",
    "s'il vous plaît", "s'il te plaît",
    "s’il vous plait", "s’il te plait",
    "s’il vous plaît", "s’il te plaît",
    "s il vous plait", "s il te plait",
    "s il vous plaît", "s il te plaît",
    "sil vous plait", "sil te plait",
    "sil vous plaît", "sil te plaît",
)


def has_please(text):
    t = (text or "").lower()
    return any(p in t for p in _PLEASE_PHRASES)


# ── DEVICE PAIRING ────────────────────────────────
# Physical-presence auth for the dashboard: press the button, say
# "authorize", and DrawBox speaks a one-time code. Redeeming the code
# issues a long-lived device token. Only hashes touch the disk.

PAIRING_WINDOW_SEC = 120
PAIRING_MAX_ATTEMPTS = 5


def is_pairing_command(text):
    """True when a transcript asks to pair a new device.

    Whisper often renders the spoken word as "authorized", so the
    past-tense variants count too.
    """
    tokens = set(normalize_voice_command(text).split())
    return bool(tokens & {"authorize", "authorise", "authorized", "authorised"})


def _hash_secret(value):
    return hashlib.sha256(value.encode()).hexdigest()


def open_pairing_window():
    """Start a pairing window and return the one-time code to speak aloud."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    _write_secure_json(PAIRING_FILE, {
        "code_hash": _hash_secret(code),
        "expires_at": time.time() + PAIRING_WINDOW_SEC,
        "attempts": 0,
    })
    return code


def redeem_pairing_code(code, device_name):
    """Exchange a spoken pairing code for a device token, or None if invalid.

    The window file is atomically claimed (renamed away) before validation,
    so one spoken code can never mint two tokens even with concurrent web
    workers. A wrong guess restores the window with the attempt counted;
    the fifth wrong guess, expiry, or success all leave the window closed.
    """
    claim = PAIRING_FILE.with_name(PAIRING_FILE.name + ".claim")
    try:
        os.replace(PAIRING_FILE, claim)  # atomic: exactly one claimer wins
    except OSError:
        return None
    try:
        window = json.loads(claim.read_text())
    except (OSError, ValueError):
        window = None
    finally:
        claim.unlink(missing_ok=True)
    if not isinstance(window, dict) or time.time() > window.get("expires_at", 0):
        return None
    if not hmac.compare_digest(window.get("code_hash", ""),
                               _hash_secret(code or "")):
        window["attempts"] = window.get("attempts", 0) + 1
        if window["attempts"] >= PAIRING_MAX_ATTEMPTS:
            log.warning("pairing window closed after %d wrong codes",
                        window["attempts"])
        else:
            _write_secure_json(PAIRING_FILE, window)
        return None
    token = secrets.token_urlsafe(32)
    devices = list_paired_devices()
    devices.append({
        "id": secrets.token_hex(6),
        "name": (device_name or "").strip()[:64] or "New device",
        "token_hash": _hash_secret(token),
        "created": datetime.now().isoformat(timespec="seconds"),
    })
    _write_secure_json(PAIRED_DEVICES_FILE, devices)
    log.info("paired new device: %s", devices[-1]["name"])
    return token


def list_paired_devices():
    if not PAIRED_DEVICES_FILE.exists():
        return []
    try:
        devices = json.loads(PAIRED_DEVICES_FILE.read_text())
    except (OSError, ValueError) as e:
        log.warning("could not read %s: %s", PAIRED_DEVICES_FILE, e)
        return []
    return devices if isinstance(devices, list) else []


def is_valid_device_token(token):
    if not token:
        return False
    token_hash = _hash_secret(token)
    return any(hmac.compare_digest(d.get("token_hash", ""), token_hash)
               for d in list_paired_devices())


def revoke_paired_device(device_id):
    """Remove a paired device by id. Returns True iff something was removed."""
    devices = list_paired_devices()
    kept = [d for d in devices if d.get("id") != device_id]
    if len(kept) == len(devices):
        return False
    _write_secure_json(PAIRED_DEVICES_FILE, kept)
    return True


# ── DEFAULT SCRIPTS ───────────────────────────────
# Default voice lines, descriptions, and jokes. Lived in two places before
# and drifted; this is now the single source of truth.

DEFAULT_VOICE_LINES = {
    "ready":      {"text": "Ready! Press the button and tell me what to draw!",
                   "desc": "Played on startup"},
    "listening":  {"text": "I'm listening!",
                   "desc": "Played when recording starts"},
    "thinking":   {"text": ("Ooh, great idea! Let me draw that for you!\n"
                            "That sounds awesome! Drawing it now!\n"
                            "Cool! Give me a moment...\n"
                            "Love it! One coloring page coming right up!\n"
                            "Nice choice! Let me work on that!"),
                   "desc": "One picked randomly while generating (one per line)"},
    "printing":   {"text": "Here it comes!",
                   "desc": "Played when sending to printer"},
    "done":       {"text": "All done! Press the button when you want another one!",
                   "desc": "Played after printing"},
    "error":      {"text": "Oops, something went wrong. Try again!",
                   "desc": "Played on any error"},
    "too_short":  {"text": "I didn't catch that. Press the button and tell me what you want to draw!",
                   "desc": "Played when recording is too short or empty"},
    "busy":       {"text": "Hold on, I'm still working on your picture! Almost done...",
                   "desc": "Played if button pressed while already generating"},
    "blocked":    {"text": "Hmm, I can't draw that. How about something fun like an animal or a rainbow?",
                   "desc": "Played when safety filter blocks a request"},
    "say_please": {"text": "Oops! Don't forget to say please! Try again and say the magic word!",
                   "desc": "Played when Please Mode is on and kid forgets to say please"},
    "poop_blocked": {"text": "I'm sorry, I can't draw this. You have to ask again without the word poop in it.",
                     "desc": "Played when Poop Mode is off and a request uses poop words"},
    "poop_mode_enabled": {"text": "Poop mode enabled.",
                          "desc": "Played when the admin voice command enables Poop Mode"},
    "poop_mode_disabled": {"text": "Poop mode disabled.",
                           "desc": "Played when the admin voice command disables Poop Mode"},
    "reboot":     {"text": "Rebooting now! See you in a moment.",
                   "desc": "Played on long button press reboot"},
}

DEFAULT_JOKES = [
    "Why did the teddy bear say no to dessert? Because she was already stuffed!",
    "What do you call a sleeping dinosaur? A dino-snore!",
    "Why do cows wear bells? Because their horns don't work!",
    "What do you call a bear with no teeth? A gummy bear!",
    "Why did the banana go to the doctor? Because it wasn't peeling well!",
    "Why can't you give Elsa a balloon? Because she will let it go!",
    "What do you call a dinosaur that crashes their car? Tyrannosaurus Wrecks!",
    "Why did the cookie go to the hospital? Because it felt crummy!",
    "Why are ghosts bad at lying? Because you can see right through them!",
    "What did the ocean say to the beach? Nothing, it just waved!",
]


def default_scripts():
    return {
        "voice_lines": {k: v["text"] for k, v in DEFAULT_VOICE_LINES.items()},
        "jokes": list(DEFAULT_JOKES),
    }


def load_scripts():
    """Return current voice scripts: defaults overlaid with on-disk overrides."""
    out = default_scripts()
    if not SCRIPTS_FILE.exists():
        return out
    try:
        saved = json.loads(SCRIPTS_FILE.read_text())
    except (OSError, ValueError) as e:
        log.warning("could not read %s: %s", SCRIPTS_FILE, e)
        return out
    if isinstance(saved.get("voice_lines"), dict):
        for k, v in saved["voice_lines"].items():
            if isinstance(v, str) and v:
                out["voice_lines"][k] = v
    if isinstance(saved.get("jokes"), list):
        jokes = [j for j in saved["jokes"] if isinstance(j, str) and j.strip()]
        if jokes:
            out["jokes"] = jokes
    return out


def save_scripts(data):
    """Persist voice scripts, sanitizing structure and length first."""
    clean = {}
    if isinstance(data.get("voice_lines"), dict):
        clean["voice_lines"] = {
            k: v[:500] for k, v in data["voice_lines"].items()
            if isinstance(k, str) and isinstance(v, str)
        }
    if isinstance(data.get("jokes"), list):
        clean["jokes"] = [j[:300] for j in data["jokes"][:100] if isinstance(j, str)]
    _write_secure_json(SCRIPTS_FILE, clean)


# ── SETTINGS ──────────────────────────────────────
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
- If the request is ambiguous, default to the most innocent interpretation
- Common requests from kids include: cats/kitties, Range Rovers, fast cars, dinosaurs, unicorns, dogs, rainbows
- If the transcription is garbled but sounds like it could be a vehicle (car, truck, SUV), default to drawing a cool Range Rover or sports car
- If it sounds like it could be an animal, default to a cute kitty or puppy"""

DEFAULT_SETTINGS = {
    "coloring_prompt": DEFAULT_COLORING_PROMPT,
    "image_model": IMAGE_MODEL,
    "tts_voice_id": "xNtG3W2oqJs0cJZuTyBc",
    "tts_stability": 0.5,
    "tts_style": 0.0,
    "record_seconds": 10,
    "poop_mode_enabled": True,
}


def load_settings():
    """Return current settings: defaults overlaid with on-disk overrides."""
    out = dict(DEFAULT_SETTINGS)
    if not SETTINGS_FILE.exists():
        return out
    try:
        saved = json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError) as e:
        log.warning("could not read %s: %s", SETTINGS_FILE, e)
        return out
    if not isinstance(saved, dict):
        return out
    for k, v in saved.items():
        if v or v == 0:
            out[k] = v
    return out


def _write_secure_json(path, data):
    """Write JSON atomically with mode 0600 (mkstemp's default).

    The atomic replace matters: the web workers and the button daemon read
    these files while the other process may be mid-write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def save_settings(data):
    _write_secure_json(SETTINGS_FILE, data)


def poop_mode_enabled():
    return bool(load_settings().get("poop_mode_enabled", True))


def set_poop_mode_enabled(enabled):
    settings = load_settings()
    settings["poop_mode_enabled"] = bool(enabled)
    save_settings(settings)
    return bool(settings["poop_mode_enabled"])


def poop_blocked_message():
    return load_scripts()["voice_lines"].get("poop_blocked") or \
        DEFAULT_VOICE_LINES["poop_blocked"]["text"]


def load_coloring_prompt():
    return load_settings().get("coloring_prompt") or DEFAULT_COLORING_PROMPT


# ── IMAGE GENERATION ──────────────────────────────

def generate_image(desc, model=None):
    """Generate a coloring page for ``desc`` and return the processed PNG path."""
    apply_api_keys()  # in case keys were updated via the dashboard

    if model is None:
        model = IMAGE_MODEL
    if model not in SUPPORTED_MODELS:
        raise ValueError(f"unsupported model: {model}")

    prompt = f"{load_coloring_prompt()}\n\nChild requested: {desc}"
    log.info("generating with %s: %s", model, desc)

    if model == "nano-banana":
        if not GEMINI_API_KEY:
            raise RuntimeError(
                "GEMINI_API_KEY not set (needed for nano-banana). "
                "Set it via the web dashboard or service file.")
        img_bytes = _generate_nano_banana(prompt)
    elif model == "gpt-image":
        if not client:
            raise RuntimeError(
                "OPENAI_API_KEY not set (needed for gpt-image). "
                "Set it via the web dashboard or service file.")
        img_bytes = _generate_gpt_image(prompt)
    elif model in GATEWAY_IMAGE_MODELS:
        if not AI_GATEWAY_API_KEY:
            raise RuntimeError(
                "AI_GATEWAY_API_KEY not set (needed for AI Gateway models). "
                "Set it via the web dashboard or service file.")
        img_bytes = _generate_ai_gateway(prompt, model)
    else:  # flux-schnell
        if not REPLICATE_API_TOKEN:
            raise RuntimeError(
                "REPLICATE_API_TOKEN not set (needed for flux-schnell). "
                "Set it via the web dashboard or service file.")
        img_bytes = _generate_flux_schnell(prompt)

    return _postprocess(img_bytes)


def _read_replicate_output(output):
    """Replicate's flux-schnell has returned both file objects and URL strings
    over time; accept either shape."""
    if isinstance(output, list):
        if not output:
            raise RuntimeError("Replicate returned an empty list")
        output = output[0]
    if hasattr(output, "read"):
        return output.read()
    if isinstance(output, (bytes, bytearray)):
        return bytes(output)
    if isinstance(output, str):
        with urlopen(output, timeout=60) as r:
            return r.read()
    raise RuntimeError(f"Unexpected Replicate output type: {type(output).__name__}")


def _generate_flux_schnell(prompt):
    t0 = time.time()
    output = replicate.run(
        "black-forest-labs/flux-schnell",
        input={
            "prompt": prompt,
            "num_outputs": 1,
            "aspect_ratio": "3:4",
            "output_format": "png",
            "num_inference_steps": 4,
            "go_fast": True,
        },
    )
    img_bytes = _read_replicate_output(output)
    log.info("replicate responded in %.1fs (%dKB)",
             time.time() - t0, len(img_bytes) // 1024)
    return img_bytes


def _generate_nano_banana(prompt):
    """Generate via Google Gemini API (Nano Banana 2)."""
    t0 = time.time()
    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        "models/gemini-2.5-flash-image:generateContent"
        f"?key={GEMINI_API_KEY}"
    )
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {"aspectRatio": "3:4"},
        },
    }).encode()
    req = Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except Exception:
        log.exception("gemini request failed")
        raise

    for part in data.get("candidates", [{}])[0].get("content", {}).get("parts", []):
        if "inlineData" in part:
            img_bytes = base64.b64decode(part["inlineData"]["data"])
            log.info("gemini responded in %.1fs (%dKB)",
                     time.time() - t0, len(img_bytes) // 1024)
            return img_bytes

    # No image — surface diagnostic info via exception, not stdout side effects.
    finish_reasons = [c.get("finishReason") for c in data.get("candidates", [])]
    err = data.get("error", {}).get("message", "")
    raise RuntimeError(
        f"No image in Gemini response (finishReason={finish_reasons}, error={err!r})")


def _generate_gpt_image(prompt):
    t0 = time.time()
    r = client.images.generate(
        model="gpt-image-1", prompt=prompt,
        size="1024x1536", quality="low",
    )
    img_bytes = base64.b64decode(r.data[0].b64_json)
    log.info("openai responded in %.1fs (%dKB)",
             time.time() - t0, len(img_bytes) // 1024)
    return img_bytes


def _generate_ai_gateway(prompt, model):
    """Generate via the Vercel AI Gateway (chat or image endpoint per model)."""
    t0 = time.time()
    chat = model in GATEWAY_CHAT_IMAGE_MODELS
    if chat:
        url = f"{AI_GATEWAY_BASE_URL}/chat/completions"
        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()
    else:
        url = f"{AI_GATEWAY_BASE_URL}/images/generations"
        body = json.dumps({
            "model": model,
            "prompt": prompt,
            "n": 1,
            "response_format": "b64_json",
        }).encode()
    req = Request(url, data=body, headers={
        "Authorization": f"Bearer {AI_GATEWAY_API_KEY}",
        "Content-Type": "application/json",
    })
    try:
        with urlopen(req, timeout=120) as resp:  # big image models are slow
            data = json.loads(resp.read())
    except Exception:
        log.exception("ai gateway request failed")
        raise

    if chat:
        choice = data.get("choices", [{}])[0]
        images = choice.get("message", {}).get("images") or []
        if not images:
            err = data.get("error", {}).get("message", "")
            raise RuntimeError(
                f"No image in AI Gateway response "
                f"(finish_reason={choice.get('finish_reason')!r}, error={err!r})")
        data_url = images[0]["image_url"]["url"]
        img_bytes = base64.b64decode(data_url.split("base64,", 1)[1])
    else:
        items = data.get("data") or []
        if not items or not items[0].get("b64_json"):
            err = data.get("error", {}).get("message", "")
            raise RuntimeError(
                f"No image in AI Gateway response (data={items!r}, error={err!r})")
        img_bytes = base64.b64decode(items[0]["b64_json"])

    log.info("ai gateway responded in %.1fs (%dKB)",
             time.time() - t0, len(img_bytes) // 1024)
    return img_bytes


# ── POST-PROCESSING ───────────────────────────────
# Output: US Letter at 150 DPI = 1275×1650 px, with 0.5" margin all around.

CANVAS_W, CANVAS_H = 1275, 1650
CANVAS_MARGIN = 75
BLACK_WHITE_THRESHOLD = 180


def _postprocess(img_bytes):
    """Threshold to pure B&W line art, then fit-and-center on a Letter canvas."""
    img = Image.open(BytesIO(img_bytes)).convert("L")
    img = img.point(lambda x: 0 if x < BLACK_WHITE_THRESHOLD else 255, "1").convert("L")
    iw, ih = img.size
    max_w = CANVAS_W - 2 * CANVAS_MARGIN
    max_h = CANVAS_H - 2 * CANVAS_MARGIN
    scale = min(max_w / iw, max_h / ih)
    new_w, new_h = int(iw * scale), int(ih * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new("L", (CANVAS_W, CANVAS_H), 255)
    canvas.paste(img, ((CANVAS_W - new_w) // 2, (CANVAS_H - new_h) // 2))
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        canvas.save(tmp.name)
        return tmp.name


# ── PRINTING ──────────────────────────────────────

_DEJAVU_DIR = "/usr/share/fonts/truetype/dejavu"


def _pairing_fonts():
    """DejaVu ships with Raspberry Pi OS; fall back to PIL's built-in."""
    try:
        return (ImageFont.truetype(f"{_DEJAVU_DIR}/DejaVuSans-Bold.ttf", 200),
                ImageFont.truetype(f"{_DEJAVU_DIR}/DejaVuSans.ttf", 56))
    except OSError:
        # Sized variants (Pillow >= 10.1) — the unsized default is ~10px,
        # which renders an unreadable card on a Letter-sized canvas.
        return ImageFont.load_default(size=200), ImageFont.load_default(size=56)


def print_pairing_code(code):
    """Print a card with the pairing code — paper works even when the
    speaker doesn't."""
    big, small = _pairing_fonts()
    img = Image.new("L", (CANVAS_W, CANVAS_H), 255)
    draw = ImageDraw.Draw(img)

    def centered(text, font, y):
        box = draw.textbbox((0, 0), text, font=font)
        draw.text(((CANVAS_W - (box[2] - box[0])) // 2, y), text,
                  font=font, fill=0)

    centered("DrawBox pairing code", small, CANVAS_H // 4)
    centered(" ".join(code), big, CANVAS_H // 4 + 120)
    centered("Type it in your DrawBox app.", small, CANVAS_H // 4 + 420)
    centered("It works for two minutes.", small, CANVAS_H // 4 + 500)

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        img.save(tmp.name)
    print_image(tmp.name)


def print_image(path):
    """Send ``path`` to the configured printer and remove the temp file."""
    log.info("printing %s", path)
    try:
        subprocess.run(
            ["lp", "-d", PRINTER_NAME, "-o", "media=Letter", "-o", "fit-to-page", path],
            check=True,
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ── ANALYTICS LOGGING ─────────────────────────────

def log_print_event(prompt, model, duration_s, source="button"):
    """Append a print event to the analytics log (best-effort; never raises)."""
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
    except OSError as e:
        log.warning("could not append print log: %s", e)
