#!/usr/bin/env python3
"""DrawBox — AI Coloring Page Printer for Kids

Hardware: Pi 5, Brother HL-L2405W (USB), EG STARTS 100mm button,
          CHANGEEK USB mic, USB speaker
"""

import os, time, tempfile, subprocess, random, hashlib, threading
from concurrent.futures import ThreadPoolExecutor
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
PLEASE_MODE_FILE = Path.home() / ".drawbox" / "please_mode"

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

def please_mode_enabled():
    return PLEASE_MODE_FILE.exists()

def has_please(text):
    t = text.lower()
    return any(w in t for w in (
        "please", "s'il vous plait", "s'il te plait",
        "s'il vous plaît", "s'il te plaît", "svp"))

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
    "say_please": ("Oops! Don't forget to say please! "
                   "Try again and say the magic word!"),
    "reboot":    "Rebooting now! See you in a moment.",
}

# ── KIDS JOKES (told while generating) ─────────
KIDS_JOKES = [
    "Why did the teddy bear say no to dessert? Because she was already stuffed!",
    "What do you call a sleeping dinosaur? A dino-snore!",
    "What do you call a fish without eyes? A fsh!",
    "Why do cows wear bells? Because their horns don't work!",
    "What do you call a bear with no teeth? A gummy bear!",
    "Why did the banana go to the doctor? Because it wasn't peeling well!",
    "What do you call a dog that does magic tricks? A Labracadabrador!",
    "Why can't you give Elsa a balloon? Because she will let it go!",
    "What do you call a dinosaur that crashes their car? Tyrannosaurus Wrecks!",
    "Why did the cookie go to the hospital? Because it felt crummy!",
    "What do cats eat for breakfast? Mice Krispies!",
    "What animal is always at a baseball game? A bat!",
    "Why are ghosts bad at lying? Because you can see right through them!",
    "What did the ocean say to the beach? Nothing, it just waved!",
    "Why did the math book look so sad? Because it had too many problems!",
    "What do you call a funny mountain? Hill-arious!",
    "What do you call cheese that isn't yours? Nacho cheese!",
    "Why did the student eat his homework? Because the teacher told him it was a piece of cake!",
    "What has ears but cannot hear? A cornfield!",
    "What do you call a pig that does karate? A pork chop!",
    "Why did the bicycle fall over? Because it was two tired!",
    "What did the big flower say to the little flower? Hi, bud!",
    "What do elves learn in school? The elf-abet!",
    "Why do bees have sticky hair? Because they use honeycombs!",
    "What do you call a boomerang that won't come back? A stick!",
    "Why did the golfer bring two pairs of pants? In case he got a hole in one!",
    "What do you call a snowman with a six-pack? An abdominal snowman!",
    "What did the left eye say to the right eye? Between you and me, something smells!",
    "What do you call a train that sneezes? Achoo-choo train!",
    "Why are elephants so wrinkly? Because you can't iron them!",
    "What did one wall say to the other wall? I'll meet you at the corner!",
    "What do you get when you cross a snowman and a vampire? Frostbite!",
    "Why don't scientists trust atoms? Because they make up everything!",
    "What kind of tree fits in your hand? A palm tree!",
    "What do you call a lazy kangaroo? A pouch potato!",
    "Why did the scarecrow win an award? Because he was outstanding in his field!",
    "What do you call a duck that gets all A's? A wise quacker!",
    "Why can't a leopard hide? Because he's always spotted!",
    "What did the traffic light say to the car? Don't look, I'm about to change!",
    "What do you call a cat sitting on the beach on Christmas Eve? Sandy Claws!",
    "Why did the tomato turn red? Because it saw the salad dressing!",
    "What do you get when you cross a centipede and a parrot? A walkie talkie!",
    "What did the stamp say to the envelope? Stick with me and we'll go places!",
    "Why are fish so smart? Because they live in schools!",
    "What do you call a sleeping bull? A bulldozer!",
    "What did the zero say to the eight? Nice belt!",
    "Why did the kid bring a ladder to school? Because she wanted to go to high school!",
    "What do you call a fairy that hasn't taken a bath? Stinker Bell!",
    "What do you get when you cross a rabbit with shellfish? An oyster bunny!",
    "Why was the broom late? It over-swept!",
]

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
        # Cache jokes (parallel for speed on first run)
        print("   🃏 Caching jokes...")
        uncached = [j for j in KIDS_JOKES if not self._tts_path(j).exists()]
        if uncached:
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(self._generate_one, uncached))
        self._joke_paths = [self._tts_path(j) for j in KIDS_JOKES
                            if self._tts_path(j).exists()]
        print(f"   🃏 Jokes cached: {len(self._joke_paths)}/{len(KIDS_JOKES)}")
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

    def play_jokes_until_done(self, thread, pause_between=1.5):
        """Play random jokes until the given thread completes."""
        jokes = list(self._joke_paths)
        if not jokes:
            thread.join()
            return
        random.shuffle(jokes)
        idx = 0
        while thread.is_alive():
            if idx >= len(jokes):
                random.shuffle(jokes)
                idx = 0
            if not thread.is_alive():
                break
            print(f"   🃏 Telling joke {idx + 1}...")
            self._play_file(jokes[idx])
            idx += 1
            # Brief pause between jokes, checking if generation is done
            waited = 0.0
            while waited < pause_between and thread.is_alive():
                time.sleep(0.1)
                waited += 0.1

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
        size="1024x1024", quality="low")
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
                elif please_mode_enabled() and not has_please(text):
                    print(f"🙏 No please: {text}")
                    voice.play("say_please")
                else:
                    # THINKING (random variation)
                    voice.play("thinking")

                    # Generate in background, tell jokes while waiting
                    gen_result = [None]
                    gen_error = [None]
                    def gen_worker():
                        try: gen_result[0] = generate_image(text)
                        except Exception as e: gen_error[0] = e
                    gen_thread = threading.Thread(
                        target=gen_worker, daemon=True)
                    gen_thread.start()
                    voice.play_jokes_until_done(gen_thread)
                    if gen_error[0]:
                        raise gen_error[0]
                    img = gen_result[0]

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
