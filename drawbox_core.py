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
IMAGE_ROUTES = {
    "nano-banana": (
        "chat",
        "google/gemini-3.1-flash-image-preview",
        {"extra_body": {"modalities": ["text", "image"]}},
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
OPENAI_TTS_VOICES = frozenset({
    "alloy", "ash", "ballad", "coral", "echo",
    "fable", "nova", "onyx", "sage", "shimmer",
})
VOICE_PROVIDERS = ("gateway", "elevenlabs", "grok")


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

PRINTER_TYPES = ("cups", "escpos_serial", "escpos_tcp")
SERIAL_BAUDS = (9600, 19200, 38400, 57600, 115200)

DEFAULT_SETTINGS = {
    "coloring_prompt": DEFAULT_COLORING_PROMPT,
    "image_model": IMAGE_MODEL,
    "voice_provider": "gateway",
    "tts_voice_id": "alloy",
    "elevenlabs_voice_id": "xNtG3W2oqJs0cJZuTyBc",
    "tts_stability": 0.5,
    "tts_style": 0.0,
    "grok_voice_id": "eve",
    "record_seconds": 10,
    "poop_mode_enabled": True,
    "printer_type": "cups",
    "serial_port": "/dev/ttyUSB0",
    "serial_baud": 9600,
    "tcp_host": "drawbox-atom.local",
    "tcp_port": 9100,
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
    """Transcribe raw audio bytes with the gateway Whisper model.

    ``media_type`` must match the actual bytes (the ESP32 voice box sends
    WAV). Raises on a missing key or a gateway failure; callers own the
    user-facing message.
    """
    apply_api_keys()  # keys may have been updated via the dashboard
    if not AI_GATEWAY_API_KEY:
        raise RuntimeError(
            "AI_GATEWAY_API_KEY not set. "
            "Add it via the web dashboard or the AI_GATEWAY_API_KEY env var.")
    t0 = time.time()
    reply = gateway_v4_post(
        AI_GATEWAY_TRANSCRIPTION_URL,
        {"audio": base64.b64encode(data).decode(), "mediaType": media_type},
        {
            "ai-transcription-model-specification-version": "4",
            "ai-model-id": GATEWAY_STT_MODEL,
        },
    )
    text = reply.get("text") or ""
    log.info("transcribed %dKB in %.1fs: %r",
             len(data) // 1024, time.time() - t0, text[:120])
    return text


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


# ── IMAGE GENERATION ──────────────────────────────

def generate_image(desc, model=None):
    """Generate a coloring page for ``desc`` and return the processed PNG path."""
    apply_api_keys()  # in case keys were updated via the dashboard

    if model is None:
        model = IMAGE_MODEL
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
