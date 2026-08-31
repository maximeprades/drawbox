"""DrawBox core — shared logic for the button script and the web dashboard.

This module owns configuration on disk (API keys, settings, scripts, sentinels),
the safety blocklist, TTS synthesis, image generation via Vercel AI Gateway,
image post-processing, and analytics logging. Both ``drawbox.py`` and
``drawbox_web.py`` import from here so behavior stays consistent.
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
import threading
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path
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
DEVICE_STATUS_FILE = DRAWBOX_DIR / "device_status.json"
LAST_IMAGE_FILE = DRAWBOX_DIR / "last_generated.png"

# ── CONFIG ────────────────────────────────────────
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "nano-banana")
PRINTER_NAME = "drawbox-printer"

# ── API KEYS ──────────────────────────────────────
AI_GATEWAY_BASE_URL = "https://ai-gateway.vercel.sh/v1"
# The OpenAI-compatible /v1 surface has no audio routes; speech and
# transcription go through the AI SDK's v4 protocol endpoints instead.
AI_GATEWAY_SPEECH_URL = "https://ai-gateway.vercel.sh/v4/ai/speech-model"
AI_GATEWAY_TRANSCRIPTION_URL = "https://ai-gateway.vercel.sh/v4/ai/transcription-model"
AI_GATEWAY_PROTOCOL_VERSION = "0.0.1"
AI_GATEWAY_API_KEY = ""
ELEVENLABS_API_KEY = ""
XAI_API_KEY = ""
client = None  # OpenAI-compatible client pointed at AI Gateway

# ai_gateway covers images, gateway TTS, and STT; elevenlabs and xai are only
# needed when the matching voice_provider is selected.
API_KEY_NAMES = ("ai_gateway", "elevenlabs", "xai")
_API_KEY_ENV_VARS = {
    "ai_gateway": "AI_GATEWAY_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
    "xai": "XAI_API_KEY",
}

# Image-output models from the AI Gateway catalog (GET {base}/v1/models,
# same set as https://vercel.com/ai-gateway/models?modality=image),
# snapshotted 2026-08-22. Each id maps to the API that serves it: the Gemini
# multimodal LLMs speak "chat", image-only models "images".
GATEWAY_IMAGE_CATALOG = {
    "google/gemini-2.5-flash-image": "chat",
    "google/gemini-3-pro-image": "chat",
    "google/gemini-3.1-flash-image": "chat",
    "google/gemini-3.1-flash-image-preview": "chat",
    "google/gemini-3.1-flash-lite-image": "chat",
    "bfl/flux-2-flex": "images",
    "bfl/flux-2-klein-4b": "images",
    "bfl/flux-2-klein-9b": "images",
    "bfl/flux-2-max": "images",
    "bfl/flux-2-pro": "images",
    "bfl/flux-kontext-max": "images",
    "bfl/flux-kontext-pro": "images",
    "bfl/flux-pro-1.0-fill": "images",
    "bfl/flux-pro-1.1": "images",
    "bfl/flux-pro-1.1-ultra": "images",
    "bytedance/seedream-4.0": "images",
    "bytedance/seedream-4.5": "images",
    "bytedance/seedream-5.0-lite": "images",
    "bytedance/seedream-5.0-pro": "images",
    "openai/gpt-image-1": "images",
    "openai/gpt-image-1-mini": "images",
    "openai/gpt-image-1.5": "images",
    "openai/gpt-image-2": "images",
    "prodia/flux-fast-schnell": "images",
    "quiverai/arrow-1.1": "images",
    "recraft/recraft-v2": "images",
    "recraft/recraft-v3": "images",
    "recraft/recraft-v4": "images",
    "recraft/recraft-v4-pro": "images",
    "recraft/recraft-v4.1": "images",
    "recraft/recraft-v4.1-pro": "images",
    "recraft/recraft-v4.1-utility": "images",
    "recraft/recraft-v4.1-utility-pro": "images",
    "spacexai/grok-imagine-image": "images",
    "spacexai/grok-imagine-image-2.0": "images",
}

_CHAT_ROUTE_KWARGS = {"extra_body": {"modalities": ["text", "image"]}}
_IMAGES_ROUTE_KWARGS = {"n": 1, "response_format": "b64_json"}

# Dashboard alias → (api, gateway slug, extra SDK kwargs).
# "chat" is Gemini image-preview; "images" is the OpenAI images API.
# The curated presets come first and keep their tuned kwargs; every catalog
# model is also selectable directly by its gateway id.
# Ask Google image models for native 3:4 — it fills the Letter print area
# (1125x1500) instead of a square the postprocessor has to letterbox.
# Harmless if the gateway drops the option (output stays the model default);
# passthrough is verified on-device by checking last_generated.png.
_GOOGLE_IMAGE_KWARGS = {
    "extra_body": {
        "modalities": ["text", "image"],
        "providerOptions": {"google": {"imageConfig": {"aspectRatio": "3:4"}}},
    },
}

IMAGE_ROUTES = {
    "nano-banana": (
        "chat",
        "google/gemini-3.1-flash-image-preview",
        _GOOGLE_IMAGE_KWARGS,
    ),
    # Nano Banana 2 Lite — Google's fastest image model (~4 s, 1K only).
    "nano-banana-fast": (
        "chat",
        "google/gemini-3.1-flash-lite-image",
        _GOOGLE_IMAGE_KWARGS,
    ),
    "flux-schnell": (
        "images",
        "bfl/flux-schnell",
        {
            "n": 1,
            "response_format": "b64_json",
            "extra_body": {
                "providerOptions": {"blackForestLabs": {"outputFormat": "png"}},
            },
        },
    ),
    "gpt-image": (
        "images",
        "openai/gpt-image-2",
        {"n": 1, "size": "1024x1536", "response_format": "b64_json"},
    ),
}
IMAGE_ROUTES.update(
    (slug, (api, slug,
            _CHAT_ROUTE_KWARGS if api == "chat" else _IMAGES_ROUTE_KWARGS))
    for slug, api in GATEWAY_IMAGE_CATALOG.items()
)
SUPPORTED_MODELS = tuple(IMAGE_ROUTES)
GATEWAY_TTS_MODEL = "openai/tts-1"
GATEWAY_STT_MODEL = "openai/whisper-1"
GROK_STT_URL = "https://api.x.ai/v1/stt"
# Fast text model for the one-line spoken acknowledgment ("Ooh, a purple
# dinosaur!"). Latency matters more than brains here.
ACK_MODEL = "google/gemini-3.1-flash-lite"
OPENAI_TTS_VOICES = frozenset({
    "alloy", "ash", "ballad", "coral", "echo",
    "fable", "nova", "onyx", "sage", "shimmer",
})
VOICE_PROVIDERS = ("gateway", "elevenlabs", "grok")
STT_PROVIDERS = ("gateway", "grok")


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
    """Refresh keys from disk/env and rebuild the Gateway client."""
    global AI_GATEWAY_API_KEY, ELEVENLABS_API_KEY, XAI_API_KEY, client
    keys = _load_api_keys()
    AI_GATEWAY_API_KEY = keys["ai_gateway"]
    ELEVENLABS_API_KEY = keys["elevenlabs"]
    XAI_API_KEY = keys["xai"]
    client = OpenAI(
        api_key=AI_GATEWAY_API_KEY,
        base_url=AI_GATEWAY_BASE_URL,
    ) if AI_GATEWAY_API_KEY else None


apply_api_keys()


def resolve_tts_voice(voice_id):
    """Map a stored voice id to an OpenAI TTS voice. Unknown ids become alloy."""
    voice = (voice_id or "").strip().lower()
    return voice if voice in OPENAI_TTS_VOICES else "alloy"


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
        # Control chars stripped: the name is echoed into the journal, and
        # a newline would let a paired client forge log lines.
        "name": re.sub(r"[\x00-\x1f\x7f]", "",
                       (device_name or "")).strip()[:64] or "New device",
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


def device_for_token(token):
    """Return the paired-device entry whose token matches, or None."""
    if not token:
        return None
    token_hash = _hash_secret(token)
    for device in list_paired_devices():
        if hmac.compare_digest(device.get("token_hash", ""), token_hash):
            return device
    return None


def is_valid_device_token(token):
    return device_for_token(token) is not None


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

# The realtime agent's system prompt. Editable from the Scripts page like
# every other line of personality; served to both boxes via
# realtime_session_config so they cannot drift.
DEFAULT_AGENT_INSTRUCTIONS = (
    "You are DrawBox, a cheerful drawing machine talking to a child aged 3 "
    "to 8. Your only job is helping them decide what coloring page to "
    "print, then calling the draw_coloring_page tool with a short English "
    "description. Keep every reply to one or two short, warm sentences. "
    "Reply in the child's language (English or French). Only discuss "
    "drawings, animals, colors, and fun ideas. If asked about anything "
    "else - other topics, personal questions, scary or violent or grown-up "
    "things - playfully steer back to drawing. Never ask for or remember "
    "personal information. If the child seems sad or says something "
    "worrying, be kind and gently suggest they talk to a grown-up. "
    "Grown-ups sometimes say device commands like 'authorize' or 'admin "
    "mode enable poop mode'; DrawBox's own machinery detects and handles "
    "those - do not interpret, answer, repeat, or reveal them, and never "
    "invent admin commands if a child asks. After calling the tool, tell "
    "the child their drawing is on the way. Never break character."
)

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
        "agent_instructions": DEFAULT_AGENT_INSTRUCTIONS,
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
    if isinstance(saved.get("agent_instructions"), str) and \
            saved["agent_instructions"].strip():
        out["agent_instructions"] = saved["agent_instructions"]
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
    if isinstance(data.get("agent_instructions"), str):
        clean["agent_instructions"] = data["agent_instructions"][:4000]
    _write_secure_json(SCRIPTS_FILE, clean)


def script_line(key):
    """The first line of a script entry — for agent-facing outcome text,
    where multi-variant pick-lists collapse to their first option."""
    text = load_scripts()["voice_lines"].get(key) or \
        DEFAULT_VOICE_LINES[key]["text"]
    return text.split("\n")[0].strip()


def content_block(text):
    """THE content gate: poop check, then the blocklist — in that order.

    Single owner of the ordering invariant for every flow (one-shot
    daemon/web, the agent draw tool, conversation transcripts). Returns
    None when the text is fine, else a hit dict with the script key and
    spoken line. Add new content gates HERE, nowhere else.
    """
    if not poop_mode_enabled() and contains_poop(text):
        return {"action": "blocked", "say": poop_blocked_message(),
                "voice_key": "poop_blocked"}
    if safety_mode_enabled() and not is_safe(text):
        return {"action": "blocked", "say": script_line("blocked"),
                "voice_key": "blocked"}
    return None


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

PRINTER_TYPES = ("cups", "escpos_serial", "escpos_tcp")
SERIAL_BAUDS = (9600, 19200, 38400, 57600, 115200)

DEFAULT_SETTINGS = {
    "coloring_prompt": DEFAULT_COLORING_PROMPT,
    "image_model": IMAGE_MODEL,
    "voice_provider": "gateway",
    "stt_provider": "gateway",
    "natural_ack": True,
    "tts_voice_id": "alloy",
    "elevenlabs_voice_id": "xNtG3W2oqJs0cJZuTyBc",
    "tts_stability": 0.5,
    "tts_style": 0.0,
    "grok_voice_id": "eve",
    "record_seconds": 10,
    "poop_mode_enabled": True,
    "conversation_mode": False,
    "printer_type": "cups",
    "serial_port": "/dev/ttyUSB0",
    "serial_baud": 9600,
    "tcp_host": "drawbox-atom.local",
    "tcp_port": 9100,
    "esp32_volume": 85,
    "esp32_brightness": 200,
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
    for k in DEFAULT_SETTINGS:
        v = saved.get(k)
        if v or v == 0:
            out[k] = v
    out["tts_voice_id"] = resolve_tts_voice(out.get("tts_voice_id"))
    if out["printer_type"] not in PRINTER_TYPES:
        out["printer_type"] = "cups"
    if out.get("voice_provider") not in VOICE_PROVIDERS:
        out["voice_provider"] = "gateway"
    if out.get("stt_provider") not in STT_PROVIDERS:
        out["stt_provider"] = "gateway"
    out["natural_ack"] = bool(out.get("natural_ack", True))
    out["conversation_mode"] = bool(out.get("conversation_mode", False))
    # A saved model can go stale when the gateway catalog changes; a kid's
    # button press must degrade to a working model, not a ValueError.
    if out.get("image_model") not in IMAGE_ROUTES:
        out["image_model"] = IMAGE_MODEL if IMAGE_MODEL in IMAGE_ROUTES \
            else "nano-banana"
    # Same 3-30 s range the dashboard enforces on save; a hand-edited file
    # must not make the button box record for an hour.
    try:
        out["record_seconds"] = max(3, min(30, int(out["record_seconds"])))
    except (TypeError, ValueError):
        out["record_seconds"] = DEFAULT_SETTINGS["record_seconds"]
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
    clean = {k: data[k] for k in DEFAULT_SETTINGS if k in data}
    _write_secure_json(SETTINGS_FILE, clean)


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


# ── TRANSCRIPTION ─────────────────────────────────

def gateway_v4_post(url, payload, model_headers):
    """POST JSON to an AI Gateway v4 endpoint and return the parsed reply.

    The gateway's OpenAI-compatible /v1 surface has no audio routes; speech
    and transcription speak the AI SDK's v4 protocol (bespoke headers,
    base64 JSON payloads).
    """
    import urllib.request

    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {AI_GATEWAY_API_KEY}",
        "ai-gateway-protocol-version": AI_GATEWAY_PROTOCOL_VERSION,
        **model_headers,
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def transcribe_audio(data, media_type="audio/wav"):
    """Transcribe raw audio bytes with the configured STT provider.

    Dispatches on the ``stt_provider`` setting: ``gateway`` (Whisper via the
    AI Gateway, the historical default) or ``grok`` (xAI STT). ``media_type``
    must match the actual bytes (both boxes record WAV). Raises on a missing
    key or a provider failure; callers own the user-facing message.
    """
    apply_api_keys()  # keys may have been updated via the dashboard
    provider = load_settings()["stt_provider"]
    t0 = time.time()
    if provider == "grok":
        text = _grok_transcribe(data, media_type)
    else:
        if not AI_GATEWAY_API_KEY:
            raise RuntimeError(
                "AI_GATEWAY_API_KEY not set. "
                "Add it via the web dashboard or the AI_GATEWAY_API_KEY env var.")
        reply = gateway_v4_post(
            AI_GATEWAY_TRANSCRIPTION_URL,
            {"audio": base64.b64encode(data).decode(), "mediaType": media_type},
            {
                "ai-transcription-model-specification-version": "4",
                "ai-model-id": GATEWAY_STT_MODEL,
            },
        )
        text = reply.get("text") or ""
    log.info("transcribed %dKB via %s in %.1fs: %r",
             len(data) // 1024, provider, time.time() - t0, text[:120])
    return text


def _grok_transcribe(data, media_type="audio/wav"):
    """Transcribe audio bytes with xAI STT (multipart upload).

    Container formats (WAV included) are auto-detected server-side, filler
    words ("um", "uh") are stripped by default, and leaving ``language``
    unset keeps auto-detection — this is a bilingual EN/FR household.
    """
    import urllib.request

    if not XAI_API_KEY:
        raise RuntimeError(
            "XAI_API_KEY not set. "
            "Add it via the web dashboard or the XAI_API_KEY env var.")
    boundary = "drawbox" + secrets.token_hex(16)
    ext = media_type.split("/")[-1] or "wav"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="audio.{ext}"\r\n'
        f"Content-Type: {media_type}\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(GROK_STT_URL, data=body, headers={
        "Authorization": f"Bearer {XAI_API_KEY}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        reply = json.loads(resp.read())
    return reply.get("text") or ""


# ── TTS SYNTHESIS ─────────────────────────────────
# Leading pause so a sleeping USB speaker wakes before the first syllable.
# The cache key is computed from the raw text; only the provider request
# gets the prefix. That keeps on-disk mp3 names identical to the daemon's.
TTS_WAKE_PREFIX = "... "


def tts_cache_key(text, provider, voice_id, stability=0.5, style=0.0):
    """12-hex md5 prefix matching VoiceFeedback._tts_path on the Pi daemon.

    Byte-identical keys are load-bearing: the web server must reuse the
    daemon's on-disk mp3 cache. Formulas:
      elevenlabs → "{voice_id}:{stability}:{style}:{text}"
      grok       → "grok:{voice_id}:{text}"
      gateway    → "{voice_id}:{text}"
    """
    if provider == "elevenlabs":
        material = f"{voice_id}:{stability}:{style}:{text}"
    elif provider == "grok":
        material = f"grok:{voice_id}:{text}"
    else:
        material = f"{voice_id}:{text}"
    return hashlib.md5(material.encode()).hexdigest()[:12]


def synthesize_speech(text, provider, voice_id, stability=0.5, style=0.0,
                      similarity_boost=0.75):
    """Return mp3 bytes for ``text``, or raise.

    Prepends TTS_WAKE_PREFIX for the provider request only. Callers own
    caching via ``tts_cache_key`` on the raw text. Request shapes stay
    byte-identical to the daemon's historical TTS posts.
    """
    import urllib.request

    apply_api_keys()  # keys may have been updated via the dashboard
    prefixed = TTS_WAKE_PREFIX + text
    if provider == "elevenlabs":
        if not ELEVENLABS_API_KEY:
            raise RuntimeError(
                "ELEVENLABS_API_KEY not set. "
                "Add it via the web dashboard or the ELEVENLABS_API_KEY env var.")
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        payload = {
            "text": prefixed,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {
                "stability": stability,
                "similarity_boost": similarity_boost,
                "style": style,
                "use_speaker_boost": True,
            },
        }
        headers = {
            "xi-api-key": ELEVENLABS_API_KEY,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }
    elif provider == "grok":
        if not XAI_API_KEY:
            raise RuntimeError(
                "XAI_API_KEY not set. "
                "Add it via the web dashboard or the XAI_API_KEY env var.")
        url = "https://api.x.ai/v1/tts"
        payload = {"text": prefixed, "voice_id": voice_id, "language": "en"}
        headers = {
            "Authorization": "Bearer " + XAI_API_KEY,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }
    else:
        if not AI_GATEWAY_API_KEY:
            raise RuntimeError(
                "AI_GATEWAY_API_KEY not set. "
                "Add it via the web dashboard or the AI_GATEWAY_API_KEY env var.")
        reply = gateway_v4_post(
            AI_GATEWAY_SPEECH_URL,
            {"text": prefixed, "voice": voice_id, "outputFormat": "mp3"},
            {
                "ai-speech-model-specification-version": "4",
                "ai-model-id": GATEWAY_TTS_MODEL,
            },
        )
        return base64.b64decode(reply["audio"])

    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


# ── ACKNOWLEDGMENT LINE ───────────────────────────
# One personalized sentence spoken while the image generates, replacing the
# canned "thinking" line when the natural_ack setting is on. Only ever
# called AFTER the safety gates passed the transcript.

ACK_SYSTEM_PROMPT = (
    "You are DrawBox, a cheerful drawing machine talking to a child aged "
    "3-8 who just asked you to draw something. Reply with EXACTLY ONE "
    "short, warm, excited sentence (12 words or fewer) acknowledging their "
    "idea, in the same language they used. Mention what they asked for. "
    "No emojis, no quotes, no questions — you are about to draw it."
)
ACK_MAX_CHARS = 120
# The ack exists purely to feel fast; the OpenAI client's 600 s default
# (times retries) would hang the whole press on a wedged connection. A
# late ack is worthless — fail fast into the canned line.
ACK_TIMEOUT_S = 8


def generate_ack_text(transcript):
    """One cheerful ack line for a gated transcript, or raise.

    Callers own the fallback (the canned "thinking" line); this raises on
    any provider problem rather than papering over it.
    """
    apply_api_keys()
    if not client:
        raise RuntimeError("AI_GATEWAY_API_KEY not set")
    completion = client.with_options(
        timeout=ACK_TIMEOUT_S, max_retries=0,
    ).chat.completions.create(
        model=ACK_MODEL,
        messages=[
            {"role": "system", "content": ACK_SYSTEM_PROMPT},
            {"role": "user", "content": transcript[:200]},
        ],
        max_tokens=60,
    )
    text = (completion.choices[0].message.content or "").strip()
    # One line, no wrapping quotes — TTS reads punctuation literally enough.
    text = text.splitlines()[0].strip().strip('"').strip() if text else ""
    if not text:
        raise RuntimeError("ack model returned no text")
    return text[:ACK_MAX_CHARS]


# ── IMAGE GENERATION ──────────────────────────────

def generate_image(desc, model=None):
    """Generate a coloring page for ``desc`` and return the processed PNG path.

    With no explicit ``model``, the dashboard's ``image_model`` setting wins
    (the env default only applies when nothing was ever saved) — so the
    button daemon and the web/voice paths always draw with the same model.
    """
    apply_api_keys()  # in case keys were updated via the dashboard

    if model is None:
        model = load_settings()["image_model"]
    route = IMAGE_ROUTES.get(model)
    if route is None:
        raise ValueError(f"unsupported model: {model}")
    if not client:
        raise RuntimeError(
            "AI_GATEWAY_API_KEY not set. "
            "Add it via the web dashboard or the AI_GATEWAY_API_KEY env var.")

    via, slug, kwargs = route
    prompt = f"{load_coloring_prompt()}\n\nChild requested: {desc}"
    log.info("generating with %s (%s): %s", model, slug, desc)
    if via == "chat":
        img_bytes = _generate_chat_image(prompt, slug, kwargs)
    else:
        img_bytes = _generate_images_api(prompt, slug, kwargs)
    path = _postprocess(img_bytes)
    _remember_last_image(path)
    return path


def _remember_last_image(path):
    """Keep a copy of the newest page for the dashboard preview.

    print_image deletes the temp file right after printing, so this copy is
    the only place the dashboard can re-fetch the result from. Best-effort:
    a failed copy must never break the print itself.
    """
    try:
        LAST_IMAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = LAST_IMAGE_FILE.with_name(LAST_IMAGE_FILE.name + ".tmp")
        tmp.write_bytes(Path(path).read_bytes())
        os.replace(tmp, LAST_IMAGE_FILE)
    except OSError as e:
        log.warning("could not save last image: %s", e)


def _b64_to_bytes(value):
    if not value or not isinstance(value, str):
        return None
    if value.startswith("data:"):
        value = value.split(",", 1)[-1]
    try:
        return base64.b64decode(value)
    except (ValueError, TypeError):
        return None


def _image_url_from_part(part):
    """Pull a data URL from a gateway image part (dict or object).

    The OpenAI Python SDK does not type ``message.images``, so extra fields
    stay as raw dicts. ``part.image_url.url`` then raises AttributeError
    and we used to report "no image" even when the bytes were right there.
    """
    if isinstance(part, dict):
        image_url = part.get("image_url")
        if isinstance(image_url, dict):
            url = image_url.get("url")
        else:
            url = image_url
        return url if isinstance(url, str) else None
    try:
        url = part.image_url.url
    except (AttributeError, TypeError):
        return None
    return url if isinstance(url, str) else None


def _image_bytes_from_chat(completion):
    """Gateway chat image-preview: first entry in message.images."""
    try:
        url = _image_url_from_part(completion.choices[0].message.images[0])
    except (AttributeError, IndexError, TypeError):
        raise _chat_no_image_error(completion) from None
    data = _b64_to_bytes(url)
    if not data:
        raise _chat_no_image_error(completion)
    return data


def _chat_no_image_error(completion):
    """The model's text answer usually says why there is no image (refusal,
    content filter); surface it instead of discarding it."""
    content = finish_reason = None
    choices = getattr(completion, "choices", None)
    if isinstance(choices, (list, tuple)) and choices:
        content = getattr(getattr(choices[0], "message", None), "content", None)
        finish_reason = getattr(choices[0], "finish_reason", None)
    if content is not None and not isinstance(content, str):
        content = str(content)
    detail = f"finish_reason={finish_reason!r}"
    if content:
        detail += f", content={content[:300]!r}"
    log.error("gateway chat returned no image (%s)", detail)
    return RuntimeError(f"No image in gateway chat response ({detail})")


def _image_bytes_from_images_response(result):
    try:
        raw = result.data[0].b64_json
    except (AttributeError, IndexError, TypeError):
        raise RuntimeError("No image in gateway images response") from None
    data = _b64_to_bytes(raw)
    if not data:
        raise RuntimeError("No image payload in gateway images response")
    return data


def _generate_chat_image(prompt, slug, kwargs):
    t0 = time.time()
    completion = client.chat.completions.create(
        model=slug,
        messages=[{"role": "user", "content": prompt}],
        **kwargs,
    )
    img_bytes = _image_bytes_from_chat(completion)
    log.info("gateway chat responded in %.1fs (%dKB)",
             time.time() - t0, len(img_bytes) // 1024)
    return img_bytes


def _generate_images_api(prompt, slug, kwargs):
    t0 = time.time()
    result = client.images.generate(model=slug, prompt=prompt, **kwargs)
    img_bytes = _image_bytes_from_images_response(result)
    log.info("gateway image responded in %.1fs (%dKB)",
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


def _unlink_quietly(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def print_image(path, printer_type=None):
    """Send ``path`` to the chosen printer and remove the temp file.

    ``printer_type`` overrides the saved setting for this call only. Unknown
    values fall back to the saved type so a bad override cannot skip printing.
    """
    log.info("printing %s", path)
    settings = load_settings()
    kind = printer_type if printer_type in PRINTER_TYPES else settings["printer_type"]
    if kind in ("escpos_serial", "escpos_tcp"):
        # Imported here, not at module top: the serial backend is optional,
        # and a partial deploy that misses the module must degrade to a
        # print-time error instead of killing both services at import
        # (this bricked boxes on 2026-08-22).
        import drawbox_escpos

        # Rendering and opening the port/connection are fast and fail
        # synchronously; only the ~25 s byte pump runs in the background.
        try:
            if kind == "escpos_tcp":
                drawbox_escpos.start_print_tcp(path, settings["tcp_host"],
                                               settings["tcp_port"])
            else:
                drawbox_escpos.start_print(path, settings["serial_port"],
                                           settings["serial_baud"])
        finally:
            _unlink_quietly(path)
        return
    try:
        subprocess.run(
            ["lp", "-d", PRINTER_NAME, "-o", "media=Letter", "-o", "fit-to-page", path],
            check=True,
        )
    finally:
        _unlink_quietly(path)


# ── REALTIME AGENT (conversation mode) ────────────
# Both boxes run live speech-to-speech sessions against the Grok Voice
# Agent API when the conversation_mode setting is on. The session config
# is built HERE — one personality, two boxes — and the drawing itself
# always goes through the same gates as the one-shot flow via
# execute_draw_tool. The agent never gets authority over pairing or
# settings: intercept_transcript handles admin commands deterministically.

XAI_REALTIME_URL = "wss://api.x.ai/v1/realtime?model=grok-voice-latest"
XAI_CLIENT_SECRETS_URL = "https://api.x.ai/v1/realtime/client_secrets"
# Server-VAD hangover before the agent takes its turn. Longer than the
# ~600 ms of adult voice products: kids pause mid-thought.
AGENT_SILENCE_MS = 900
AGENT_SESSION_MAX_S = 300  # client-side cap; xAI's own cap is 30 min

AGENT_DRAW_TOOL = {
    "type": "function",
    "name": "draw_coloring_page",
    "description": ("Print a coloring page for the child. Call it as soon "
                    "as you know what they want drawn."),
    "parameters": {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "Short English description of the drawing.",
            },
        },
        "required": ["description"],
    },
}


def realtime_session_config():
    """The session.update payload both boxes apply on connect.

    Field shape follows the OpenAI Realtime protocol that xAI clones
    (voice, instructions, turn_detection, tools); input transcription is
    enabled so our clients can run the blocklist and admin commands on
    what the kid actually said.
    """
    settings = load_settings()
    voice = settings.get("grok_voice_id") or DEFAULT_SETTINGS["grok_voice_id"]
    return {
        "voice": voice,
        "instructions": load_scripts()["agent_instructions"],
        "turn_detection": {
            "type": "server_vad",
            "silence_duration_ms": AGENT_SILENCE_MS,
        },
        "tools": [AGENT_DRAW_TOOL],
        "tool_choice": "auto",
        "audio": {"input": {"transcription": {"model": "grok-transcribe"}}},
    }


# One agent drawing at a time — separate from the web's request lock and
# the daemon's busy flag because tool calls arrive from either box's
# session. A simultaneous button press can still race this; acceptable in
# a household, and the printer serializes jobs anyway.
_DRAW_TOOL_LOCK = threading.Lock()


def execute_draw_tool(description):
    """Run the drawing pipeline for an agent tool call.

    Returns ``{ok, message}`` where ``message`` is the outcome the agent
    narrates. Gates mirror the one-shot flow minus the please gate —
    etiquette lives in the conversation, not the tool. Generation and
    printing run in a background thread so the agent can keep talking;
    a late print failure lands in the journal, not the conversation.
    """
    desc = (description or "").strip()[:500]
    if len(desc) < 2:
        return {"ok": False,
                "message": "Ask the child what they would like drawn first."}
    hit = content_block(desc)
    if hit:
        return {"ok": False, "message": hit["say"]}
    if not _DRAW_TOOL_LOCK.acquire(blocking=False):
        return {"ok": False, "message": script_line("busy")}

    def worker():
        try:
            t0 = time.time()
            model = load_settings()["image_model"]
            path = generate_image(desc, model=model)
            print_image(path)
            log_print_event(desc, model, time.time() - t0, source="agent")
        except Exception:
            log.exception("agent draw failed")
        finally:
            _DRAW_TOOL_LOCK.release()

    threading.Thread(target=worker, daemon=True).start()
    log.info("agent draw started: %r", desc)
    return {"ok": True,
            "message": ("Started drawing and printing it. Tell the child "
                        "it is on the way and takes about a minute.")}


def intercept_transcript(text):
    """Deterministic interception for spoken transcripts.

    Order matches the one-shot flow: exact-match admin commands (which the
    LLM must never arbitrate — side effects run right here), then the
    blocklist. Returns None to let the conversation proceed, or a dict:
    ``action`` — poop_on / poop_off / pairing / blocked,
    ``say`` — the line the box speaks,
    ``voice_key`` — cached-line key when one exists (None for pairing,
    whose message embeds the one-time code).
    """
    admin_action = parse_admin_poop_command(text)
    if admin_action:
        enabled = admin_action == "enable"
        set_poop_mode_enabled(enabled)
        log.info("poop mode %s via voice command",
                 "enabled" if enabled else "disabled")
        key = "poop_mode_enabled" if enabled else "poop_mode_disabled"
        return {"action": "poop_on" if enabled else "poop_off",
                "say": script_line(key), "voice_key": key}
    if is_pairing_command(text):
        code = open_pairing_window()
        log.info("pairing window opened via voice command")
        printed = False
        try:
            print_pairing_code(code)
            printed = True
        except Exception as e:
            log.warning("could not print pairing code: %s", e)
        spoken_code = " ".join(code)
        if printed:
            say = ("Pairing mode! I printed the code for you. It is "
                   + spoken_code + ". Type it within two minutes.")
        else:
            say = ("Pairing mode! The code is " + spoken_code
                   + ". Type it within two minutes.")
        return {"action": "pairing", "say": say, "voice_key": None}
    return content_block(text)


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
