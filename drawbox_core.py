"""DrawBox core — shared logic for the button script and the web dashboard.

This module owns configuration on disk (API keys, settings, scripts, sentinels),
the safety blocklist, image generation across three providers, image post-
processing, and analytics logging. Both ``drawbox.py`` and ``drawbox_web.py``
import from here so behavior stays consistent.
"""

import base64
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.request import Request, urlopen

import replicate
from openai import OpenAI
from PIL import Image

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

# ── CONFIG ────────────────────────────────────────
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "nano-banana")
PRINTER_NAME = "drawbox-printer"
SUPPORTED_MODELS = ("nano-banana", "flux-schnell", "gpt-image")

# ── API KEYS ──────────────────────────────────────
OPENAI_API_KEY = ""
REPLICATE_API_TOKEN = ""
GEMINI_API_KEY = ""
ELEVENLABS_API_KEY = ""
client = None  # OpenAI client, rebuilt on apply_api_keys()


def _load_api_keys():
    """Read API keys from the on-disk file, falling back to environment variables."""
    keys = {}
    if API_KEYS_FILE.exists():
        try:
            keys = json.loads(API_KEYS_FILE.read_text())
        except (OSError, ValueError) as e:
            log.warning("could not read %s: %s", API_KEYS_FILE, e)
    return {
        "openai": keys.get("openai") or os.environ.get("OPENAI_API_KEY") or "",
        "replicate": keys.get("replicate") or os.environ.get("REPLICATE_API_TOKEN") or "",
        "gemini": keys.get("gemini") or os.environ.get("GEMINI_API_KEY") or "",
        "elevenlabs": keys.get("elevenlabs") or os.environ.get("ELEVENLABS_API_KEY") or "",
    }


def apply_api_keys():
    """Refresh module-level keys from disk/env and rebuild the OpenAI client."""
    global OPENAI_API_KEY, REPLICATE_API_TOKEN, GEMINI_API_KEY, ELEVENLABS_API_KEY, client
    keys = _load_api_keys()
    OPENAI_API_KEY = keys["openai"]
    REPLICATE_API_TOKEN = keys["replicate"]
    GEMINI_API_KEY = keys["gemini"]
    ELEVENLABS_API_KEY = keys["elevenlabs"]
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


def safety_mode_enabled():
    return SAFETY_MODE_FILE.exists()


def please_mode_enabled():
    return PLEASE_MODE_FILE.exists()


_PLEASE_PHRASES = (
    "please", "svp",
    "s'il vous plait", "s'il te plait",
    "s'il vous plaît", "s'il te plaît",
)


def has_please(text):
    t = (text or "").lower()
    return any(p in t for p in _PLEASE_PHRASES)


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


def default_voice_text(key):
    return DEFAULT_VOICE_LINES[key]["text"]


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
    SCRIPTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SCRIPTS_FILE.write_text(json.dumps(clean, indent=2))


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


def save_settings(data):
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, indent=2))


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
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    canvas.save(tmp.name)
    tmp.close()
    return tmp.name


# ── PRINTING ──────────────────────────────────────

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
