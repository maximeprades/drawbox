#!/usr/bin/env python3
"""DrawBox — AI Coloring Page Printer for Kids

Hardware: Pi 5, Brother HL-L2405W (USB), EG STARTS 100mm button,
          CHANGEEK USB mic, USB speaker
"""

import os, time, tempfile, subprocess, random, hashlib, threading
import sounddevice as sd
import soundfile as sf
import numpy as np
from gpiozero import Button as GpioButton
from openai import OpenAI
from PIL import Image
import base64
from io import BytesIO
from pathlib import Path

# ── CONFIG ──────────────────────────────────────
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
BUTTON_PIN = 17
SAMPLE_RATE = 44100
PRINTER_NAME = "drawbox-printer"
RECORD_SECONDS = 10
REBOOT_HOLD_SEC = 5           # hold button 5s to reboot
TTS_VOICE = "nova"            # alloy, echo, fable, onyx, nova, shimmer
CACHE_DIR = Path.home() / ".drawbox" / "voice_cache"

# ── SAFETY BLOCKLIST ────────────────────────────
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

COLORING_PROMPT = """Create a simple coloring page for children ages 3-8.
This is used by YOUNG CHILDREN — output MUST be 100% child-safe.
- Black and white LINE DRAWING only
- Thick, clean outlines (3-4px stroke)
- NO shading, NO gradients, NO filled/solid areas
- NO gray — pure black lines on white
- Simple shapes, minimal fine detail
- Large open areas for coloring with crayons
- Friendly, fun, cute, non-scary style
- Centered, filling most of the space
- Style: children's coloring book page
- ONLY draw safe, wholesome subjects (animals, nature, vehicles, food, toys)
- NEVER draw anything violent, scary, sexual, or inappropriate for a 5-year-old
- If the request is ambiguous, default to the most innocent interpretation"""

client = OpenAI(api_key=OPENAI_API_KEY)

# ── VOICE FEEDBACK ─────────────────────────────
# All voice lines. Keys with lists pick randomly.
VOICE_LINES = {
    "ready":     "Ready! Press the button and tell me what to draw!",
    "listening": "I'm listening!",
    "thinking":  [
        "Ooh, great idea! Let me draw that for you!",
        "That sounds awesome! Drawing it now!",
        "Cool! Give me a moment...",
        "Love it! One coloring page coming right up!",
        "Nice choice! Let me work on that!",
    ],
    "printing":  "Here it comes!",
    "done":      "All done! Press the button when you want another one!",
    "error":     "Oops, something went wrong. Try again!",
    "too_short": ("I didn't catch that. Press the button "
                  "and tell me what you want to draw!"),
    "busy":      ("Hold on, I'm still working on your picture! "
                  "Almost done..."),
    "blocked":   ("Hmm, I can't draw that. How about something "
                  "fun like an animal or a rainbow?"),
    "reboot":    "Rebooting now! See you in a moment.",
}

class VoiceFeedback:
    """Pre-generates TTS lines and caches them as .mp3 files."""

    def __init__(self):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._cache = {}  # key → path (or list of paths)
        self._warm_up()

    def _tts_path(self, text):
        """Deterministic filename based on voice + text content."""
        h = hashlib.md5(f"{TTS_VOICE}:{text}".encode()).hexdigest()[:12]
        return CACHE_DIR / f"{h}.mp3"

    def _generate_one(self, text):
        """Generate a single TTS file if not already cached."""
        path = self._tts_path(text)
        if path.exists():
            return path
        print(f"   🔊 Caching: {text[:50]}...")
        try:
            resp = client.audio.speech.create(
                model="tts-1", voice=TTS_VOICE, input=text)
            resp.stream_to_file(str(path))
        except Exception as e:
            print(f"   ⚠️  TTS cache error: {e}")
            return None
        return path

    def _warm_up(self):
        """Pre-generate all static voice lines at startup."""
        print("🔊 Warming up voice cache...")
        for key, val in VOICE_LINES.items():
            if isinstance(val, list):
                paths = []
                for line in val:
                    p = self._generate_one(line)
                    if p: paths.append(p)
                self._cache[key] = paths
            else:
                p = self._generate_one(val)
                if p: self._cache[key] = p
        print("   ✅ Voice cache ready")

    def play(self, key, block=True):
        """Play a cached voice line by key.
        If block=False, plays in a background thread."""
        entry = self._cache.get(key)
        if entry is None:
            # Fallback: generate on the fly
            text = VOICE_LINES.get(key, key)
            if isinstance(text, list): text = random.choice(text)
            self._play_live(text)
            return
        if isinstance(entry, list):
            path = random.choice(entry) if entry else None
        else:
            path = entry
        if not path: return
        if block:
            self._play_file(path)
        else:
            threading.Thread(target=self._play_file,
                             args=(path,), daemon=True).start()

    def play_dynamic(self, text, block=True):
        """Speak arbitrary text (not cached). Used for
        transcription read-back or dynamic messages."""
        if block:
            self._play_live(text)
        else:
            threading.Thread(target=self._play_live,
                             args=(text,), daemon=True).start()

    def _play_file(self, path):
        try:
            time.sleep(0.5)  # let USB speaker wake up
            subprocess.run(["mpg123", "-q", str(path)], check=True)
        except Exception:
            # Fallback to aplay if mpg123 fails
            try:
                subprocess.run(["aplay", "-q", str(path)], check=False)
            except Exception:
                pass

    def _play_live(self, text):
        """Generate and play TTS on the fly (uncached)."""
        print(f"🔊 Speaking: {text}")
        try:
            resp = client.audio.speech.create(
                model="tts-1", voice=TTS_VOICE, input=text)
            tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
            resp.stream_to_file(tmp.name)
            self._play_file(tmp.name)
            os.unlink(tmp.name)
        except Exception as e:
            print(f"   TTS error: {e}")
            subprocess.run(["espeak", text], check=False)

# ── BEEP FALLBACK ──────────────────────────────
def beep(freq=440, dur=0.2):
    t = np.linspace(0, dur, int(SAMPLE_RATE * dur), False)
    sd.play((np.sin(freq * t * 2 * np.pi) * 0.3).astype(np.float32),
            SAMPLE_RATE)
    sd.wait()

def beep_ready():
    beep(523, 0.1); time.sleep(0.05)
    beep(659, 0.1); time.sleep(0.05)
    beep(784, 0.2)

# ── RECORD ──────────────────────────────────────
def record_audio():
    print(f"🎙️  Recording for {RECORD_SECONDS}s...")
    frames = []
    def cb(indata, fc, ti, st): frames.append(indata.copy())
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, callback=cb):
        time.sleep(RECORD_SECONDS)
    if not frames: return None
    audio = np.concatenate(frames)
    if len(audio) / SAMPLE_RATE < 0.5: return None
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    sf.write(tmp.name, audio, SAMPLE_RATE)
    print(f"   Recorded {len(audio)/SAMPLE_RATE:.1f}s")
    return tmp.name

# ── TRANSCRIBE ──────────────────────────────────
def transcribe(path):
    print("📝 Transcribing...")
    with open(path, "rb") as f:
        r = client.audio.transcriptions.create(model="whisper-1", file=f)
    os.unlink(path)
    print(f'   Heard: "{r.text}"')
    return r.text

# ── GENERATE ────────────────────────────────────
def generate_image(desc):
    print(f"🎨 Generating: {desc}")
    r = client.images.generate(
        model="gpt-image-1",
        prompt=f"{COLORING_PROMPT}\n\nChild requested: {desc}",
        size="1024x1024", quality="medium")
    img_bytes = base64.b64decode(r.data[0].b64_json)
    img = Image.open(BytesIO(img_bytes)).convert("L")
    img = img.point(lambda x: 0 if x < 180 else 255, "1").convert("L")
    img = img.resize((1125, 1125), Image.LANCZOS)
    canvas = Image.new("L", (1275, 1650), 255)
    canvas.paste(img, (75, 262))
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    canvas.save(tmp.name); tmp.close()
    return tmp.name

# ── PRINT ───────────────────────────────────────
def print_image(path):
    print("🖨️  Printing...")
    subprocess.run(["lp", "-d", PRINTER_NAME,
        "-o", "media=Letter", "-o", "fit-to-page", path], check=True)
    os.unlink(path)
    print("   ✅ Sent to printer!")

# ── MAIN ────────────────────────────────────────
is_busy = False

def main():
    global is_busy
    btn = GpioButton(BUTTON_PIN, pull_up=True, bounce_time=0.1)

    voice = VoiceFeedback()

    print("=" * 48)
    print("  🎨 DrawBox Ready!")
    print("  Press the red button to start!")
    print("=" * 48)
    voice.play("ready")

    try:
        while True:
            btn.wait_for_press()

            # ── LONG-PRESS REBOOT ──
            held = 0.0
            while btn.is_pressed:
                time.sleep(0.1)
                held += 0.1
                if held >= REBOOT_HOLD_SEC:
                    print("🔄 Reboot requested (5s hold)")
                    voice.play("reboot")
                    subprocess.run(["sudo", "reboot"])
                    return  # script exits; systemd won't restart (clean exit)
            # Button released before 5s — normal press

            # ── BUSY GUARD ──
            if is_busy:
                voice.play("busy", block=False)
                continue

            is_busy = True
            try:
                # LISTENING
                voice.play("listening")
                path = record_audio()

                if not path:
                    voice.play("too_short")
                elif not (text := transcribe(path)) or len(text.strip()) < 2:
                    voice.play("too_short")
                elif not is_safe(text):
                    print(f"🚫 Blocked: {text}")
                    voice.play("blocked")
                else:
                    # THINKING (random variation)
                    voice.play("thinking")
                    img = generate_image(text)

                    # PRINTING
                    voice.play("printing")
                    print_image(img)

                    # DONE
                    voice.play("done")

            except Exception as e:
                print(f"❌ Error: {e}")
                voice.play("error")
            finally:
                is_busy = False
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n👋 Shutting down")

if __name__ == "__main__":
    main()
