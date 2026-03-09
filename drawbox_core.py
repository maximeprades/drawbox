"""DrawBox Core — Shared logic used by both drawbox.py and drawbox_web.py

Centralizes API key management, safety filtering, image generation,
post-processing, printing, and analytics logging.
"""

import os, json, tempfile, subprocess, base64, time
from pathlib import Path
from io import BytesIO
from datetime import datetime
from urllib.request import Request, urlopen

import replicate
from openai import OpenAI
from PIL import Image

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

# ── API KEYS ──────────────────────────────────────
OPENAI_API_KEY = ""
REPLICATE_API_TOKEN = ""
GEMINI_API_KEY = ""
ELEVENLABS_API_KEY = ""
client = None


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
        "elevenlabs": keys.get("elevenlabs") or os.environ.get("ELEVENLABS_API_KEY") or "",
    }


def apply_api_keys():
    """Apply loaded keys to module globals and environment."""
    global OPENAI_API_KEY, REPLICATE_API_TOKEN, GEMINI_API_KEY, ELEVENLABS_API_KEY, client
    keys = _load_api_keys()
    OPENAI_API_KEY = keys["openai"]
    REPLICATE_API_TOKEN = keys["replicate"]
    GEMINI_API_KEY = keys["gemini"]
    ELEVENLABS_API_KEY = keys["elevenlabs"]
    if keys["replicate"]:
        os.environ["REPLICATE_API_TOKEN"] = keys["replicate"]
    if OPENAI_API_KEY:
        client = OpenAI(api_key=OPENAI_API_KEY)


# Initialize on import
apply_api_keys()

# ── SAFETY BLOCKLIST ──────────────────────────────
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
    """Check text against the safety blocklist using word-boundary matching."""
    words = set(text.lower().split())
    return not words & BLOCKED_WORDS


def safety_mode_enabled():
    return SAFETY_MODE_FILE.exists()


def please_mode_enabled():
    return PLEASE_MODE_FILE.exists()


def has_please(text):
    t = text.lower()
    return any(w in t for w in (
        "please", "s'il vous plait", "s'il te plait",
        "s'il vous plaît", "s'il te plaît", "svp"))


# ── COLORING PROMPT ───────────────────────────────
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


def load_coloring_prompt():
    """Load coloring prompt from shared settings file, fall back to default."""
    if SETTINGS_FILE.exists():
        try:
            settings = json.loads(SETTINGS_FILE.read_text())
            prompt = settings.get("coloring_prompt", "").strip()
            if prompt:
                return prompt
        except Exception:
            pass
    return DEFAULT_COLORING_PROMPT


# ── IMAGE GENERATION ──────────────────────────────

def generate_image(desc, model=None):
    """Generate a coloring page image and return the path to the processed PNG."""
    # Re-read keys in case they were updated via the web dashboard
    apply_api_keys()

    if model is None:
        model = IMAGE_MODEL

    coloring_prompt = load_coloring_prompt()
    prompt = f"{coloring_prompt}\n\nChild requested: {desc}"
    print(f"🎨 Generating ({model}): {desc}")

    if model == "nano-banana":
        if not GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY not set (needed for nano-banana). "
                               "Set it via the web dashboard or service file.")
        img_bytes = _generate_nano_banana(prompt)
    elif model == "gpt-image":
        if not client:
            raise RuntimeError("OPENAI_API_KEY not set (needed for gpt-image). "
                               "Set it via the web dashboard or service file.")
        img_bytes = _generate_gpt_image(prompt)
    else:  # flux-schnell
        if not REPLICATE_API_TOKEN:
            raise RuntimeError("REPLICATE_API_TOKEN not set (needed for flux-schnell). "
                               "Set it via the web dashboard or service file.")
        img_bytes = _generate_flux_schnell(prompt)

    return _postprocess(img_bytes)


def _generate_flux_schnell(prompt):
    print("   📡 Calling Replicate (flux-schnell)...")
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
        })
    img_bytes = output[0].read()
    print(f"   ✅ Replicate responded in {time.time()-t0:.1f}s ({len(img_bytes)//1024}KB)")
    return img_bytes


def _generate_nano_banana(prompt):
    """Generate via Google Gemini API (Nano Banana 2)."""
    print("   📡 Calling Gemini (nano-banana)...")
    t0 = time.time()
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
    req = Request(url, data=body,
                  headers={"Content-Type": "application/json"})
    try:
        resp = urlopen(req, timeout=60)
    except Exception as e:
        print(f"   ❌ Gemini API request failed: {e}")
        raise
    raw = resp.read()
    data = json.loads(raw)
    # Find the image part in the response
    for part in data.get("candidates", [{}])[0].get("content", {}).get("parts", []):
        if "inlineData" in part:
            img_bytes = base64.b64decode(part["inlineData"]["data"])
            print(f"   ✅ Gemini responded in {time.time()-t0:.1f}s ({len(img_bytes)//1024}KB)")
            return img_bytes
    # No image found — log the response for debugging
    print(f"   ❌ No image in Gemini response. Keys: {list(data.keys())}")
    if "candidates" in data:
        for i, c in enumerate(data["candidates"]):
            print(f"      candidate[{i}]: finishReason={c.get('finishReason','?')}, "
                  f"parts={[list(p.keys()) for p in c.get('content',{}).get('parts',[])]}")
    if "error" in data:
        print(f"      error: {data['error']}")
    raise RuntimeError(f"No image in Gemini response: {list(data.keys())}")


def _generate_gpt_image(prompt):
    print("   📡 Calling OpenAI (gpt-image-1)...")
    t0 = time.time()
    r = client.images.generate(
        model="gpt-image-1", prompt=prompt,
        size="1024x1536", quality="low")
    img_bytes = base64.b64decode(r.data[0].b64_json)
    print(f"   ✅ OpenAI responded in {time.time()-t0:.1f}s ({len(img_bytes)//1024}KB)")
    return img_bytes


def _postprocess(img_bytes):
    """Convert to B&W line art and fit onto letter-size canvas."""
    img = Image.open(BytesIO(img_bytes)).convert("L")
    img = img.point(lambda x: 0 if x < 180 else 255, "1").convert("L")
    iw, ih = img.size
    canvas_w, canvas_h = 1275, 1650
    margin = 75  # 0.5" at 150dpi
    max_w = canvas_w - 2 * margin
    max_h = canvas_h - 2 * margin
    scale = min(max_w / iw, max_h / ih)
    new_w = int(iw * scale)
    new_h = int(ih * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new("L", (canvas_w, canvas_h), 255)
    canvas.paste(img, ((canvas_w - new_w) // 2, (canvas_h - new_h) // 2))
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    canvas.save(tmp.name)
    tmp.close()
    return tmp.name


# ── PRINTING ──────────────────────────────────────

def print_image(path):
    """Send an image to the printer and delete the temp file."""
    print("🖨️  Printing...")
    try:
        subprocess.run(["lp", "-d", PRINTER_NAME,
            "-o", "media=Letter", "-o", "fit-to-page", path], check=True)
        print("   ✅ Sent to printer!")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ── ANALYTICS LOGGING ─────────────────────────────

def log_print_event(prompt, model, duration_s, source="button"):
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
