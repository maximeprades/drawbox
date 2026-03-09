#!/usr/bin/env python3
"""DrawBox — AI Coloring Page Printer for Kids

Hardware: Pi 5, Brother HL-L2405W (USB), EG STARTS 100mm button,
          CHANGEEK USB mic, USB speaker
"""

import os, time, tempfile, subprocess, random, hashlib, threading, json, traceback
from concurrent.futures import ThreadPoolExecutor
import sounddevice as sd
import soundfile as sf
import numpy as np
from gpiozero import Button as GpioButton

from drawbox_core import (
    apply_api_keys, API_KEYS_FILE, SETTINGS_FILE, SAFETY_MODE_FILE,
    SCRIPTS_FILE, CACHE_DIR, IMAGE_MODEL,
    is_safe, safety_mode_enabled, please_mode_enabled, has_please,
    generate_image, print_image, log_print_event,
)
import drawbox_core

# ── CONFIG ──────────────────────────────────────
BUTTON_PIN = 17
SAMPLE_RATE = 44100
RECORD_SECONDS = 10
REBOOT_HOLD_SEC = 5           # hold button 5s to reboot
TTS_VOICE_ID = "xNtG3W2oqJs0cJZuTyBc"  # ElevenLabs voice ID
TTS_STABILITY = 0.5           # 0.0–1.0 (lower = more expressive)
TTS_STYLE = 0.0               # 0.0–1.0 (style exaggeration)

# ── VOICE FEEDBACK ─────────────────────────────
# All voice lines. Keys with lists pick randomly.
VOICE_LINES = {
    "ready":     "[cheerfully] Ready! Press the button and tell me what to draw!",
    "listening": "[cheerfully] I'm listening!",
    "thinking":  [
        "[excitedly] Ooh, great idea! Let me draw that for you!",
        "[elated] That sounds awesome! Drawing it now!",
        "[cheerfully] Cool! Give me a moment...",
        "[excitedly] Love it! One coloring page coming right up!",
        "[cheerfully] Nice choice! Let me work on that!",
    ],
    "printing":  "[excitedly] Here it comes!",
    "done":      "[giggling] All done! Press the button when you want another one!",
    "error":     "[gently] Oops, something went wrong. Try again!",
    "too_short": ("[gently] I didn't catch that. Press the button "
                  "and tell me what you want to draw!"),
    "busy":      ("[cheerfully] Hold on, I'm still working on your picture! "
                  "Almost done..."),
    "blocked":   ("[gently] Hmm, I can't draw that. How about something "
                  "fun like an animal or a rainbow?"),
    "say_please": ("[playfully] Oops! Don't forget to say please! "
                   "Try again and say the magic word!"),
    "reboot":    "[cheerfully] Rebooting now! See you in a moment.",
}

# ── KIDS JOKES (told while generating) ─────────
KIDS_JOKES = [
    "Why did the teddy bear say no to dessert? ... [giggling] Because she was already stuffed!",
    "What do you call a sleeping dinosaur? ... [giggling] A dino-snore!",
    "What do you call a fish without eyes? ... [giggling] A fsh!",
    "Why do cows wear bells? ... [giggling] Because their horns don't work!",
    "What do you call a bear with no teeth? ... [giggling] A gummy bear!",
    "Why did the banana go to the doctor? ... [giggling] Because it wasn't peeling well!",
    "What do you call a dog that does magic tricks? ... [giggling] A Labracadabrador!",
    "Why can't you give Elsa a balloon? ... [giggling] Because she will let it go!",
    "What do you call a dinosaur that crashes their car? ... [giggling] Tyrannosaurus Wrecks!",
    "Why did the cookie go to the hospital? ... [giggling] Because it felt crummy!",
    "What do cats eat for breakfast? ... [giggling] Mice Krispies!",
    "What animal is always at a baseball game? ... [giggling] A bat!",
    "Why are ghosts bad at lying? ... [giggling] Because you can see right through them!",
    "What did the ocean say to the beach? ... [giggling] Nothing, it just waved!",
    "Why did the math book look so sad? ... [giggling] Because it had too many problems!",
    "What do you call a funny mountain? ... [giggling] Hill-arious!",
    "What do you call cheese that isn't yours? ... [giggling] Nacho cheese!",
    "Why did the student eat his homework? ... [giggling] Because the teacher told him it was a piece of cake!",
    "What has ears but cannot hear? ... [giggling] A cornfield!",
    "What do you call a pig that does karate? ... [giggling] A pork chop!",
    "Why did the bicycle fall over? ... [giggling] Because it was two tired!",
    "What did the big flower say to the little flower? ... [giggling] Hi, bud!",
    "What do elves learn in school? ... [giggling] The elf-abet!",
    "Why do bees have sticky hair? ... [giggling] Because they use honeycombs!",
    "What do you call a boomerang that won't come back? ... [giggling] A stick!",
    "Why did the golfer bring two pairs of pants? ... [giggling] In case he got a hole in one!",
    "What do you call a snowman with a six-pack? ... [giggling] An abdominal snowman!",
    "What did the left eye say to the right eye? ... [giggling] Between you and me, something smells!",
    "What do you call a train that sneezes? ... [giggling] Achoo-choo train!",
    "Why are elephants so wrinkly? ... [giggling] Because you can't iron them!",
    "What did one wall say to the other wall? ... [giggling] I'll meet you at the corner!",
    "What do you get when you cross a snowman and a vampire? ... [giggling] Frostbite!",
    "Why don't scientists trust atoms? ... [giggling] Because they make up everything!",
    "What kind of tree fits in your hand? ... [giggling] A palm tree!",
    "What do you call a lazy kangaroo? ... [giggling] A pouch potato!",
    "Why did the scarecrow win an award? ... [giggling] Because he was outstanding in his field!",
    "What do you call a duck that gets all A's? ... [giggling] A wise quacker!",
    "Why can't a leopard hide? ... [giggling] Because he's always spotted!",
    "What did the traffic light say to the car? ... [giggling] Don't look, I'm about to change!",
    "What do you call a cat sitting on the beach on Christmas Eve? ... [giggling] Sandy Claws!",
    "Why did the tomato turn red? ... [giggling] Because it saw the salad dressing!",
    "What do you get when you cross a centipede and a parrot? ... [giggling] A walkie talkie!",
    "What did the stamp say to the envelope? ... [giggling] Stick with me and we'll go places!",
    "Why are fish so smart? ... [giggling] Because they live in schools!",
    "What do you call a sleeping bull? ... [giggling] A bulldozer!",
    "What did the zero say to the eight? ... [giggling] Nice belt!",
    "Why did the kid bring a ladder to school? ... [giggling] Because she wanted to go to high school!",
    "What do you call a fairy that hasn't taken a bath? ... [giggling] Stinker Bell!",
    "What do you get when you cross a rabbit with shellfish? ... [giggling] An oyster bunny!",
    "Why was the broom late? ... [giggling] It over-swept!",
]

def _load_voice_scripts():
    """Load voice line/joke overrides from the dashboard scripts file."""
    global VOICE_LINES, KIDS_JOKES
    if not SCRIPTS_FILE.exists():
        return
    try:
        data = json.loads(SCRIPTS_FILE.read_text())
    except Exception:
        return
    # Override voice lines
    if data.get("voice_lines"):
        for key, text in data["voice_lines"].items():
            if not text or key not in VOICE_LINES:
                continue
            lines = [l.strip() for l in text.strip().split("\n") if l.strip()]
            if len(lines) > 1:
                VOICE_LINES[key] = lines
            elif lines:
                VOICE_LINES[key] = lines[0]
        print(f"   📝 Voice line overrides loaded from {SCRIPTS_FILE}")
    # Override jokes
    if data.get("jokes"):
        KIDS_JOKES = [j for j in data["jokes"] if j.strip()]
        print(f"   🃏 Jokes overrides loaded: {len(KIDS_JOKES)} jokes")

_load_voice_scripts()

def _load_tts_settings():
    """Load TTS voice ID and ElevenLabs settings from the dashboard settings file."""
    global TTS_VOICE_ID, TTS_STABILITY, TTS_STYLE
    if not SETTINGS_FILE.exists():
        return
    try:
        data = json.loads(SETTINGS_FILE.read_text())
    except Exception:
        return
    if data.get("tts_voice_id"):
        TTS_VOICE_ID = data["tts_voice_id"]
    if "tts_stability" in data:
        TTS_STABILITY = max(0.0, min(1.0, float(data["tts_stability"])))
    if "tts_style" in data:
        TTS_STYLE = max(0.0, min(1.0, float(data["tts_style"])))

_load_tts_settings()

class VoiceFeedback:
    """Pre-generates TTS lines via ElevenLabs and caches them as .mp3 files."""

    def __init__(self):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._cache = {}  # key → path (or list of paths)
        self._warm_up()

    def _tts_path(self, text):
        """Deterministic filename based on voice + settings + text."""
        h = hashlib.md5(
            f"{TTS_VOICE_ID}:{TTS_STABILITY}:{TTS_STYLE}:{text}".encode()
        ).hexdigest()[:12]
        return CACHE_DIR / f"{h}.mp3"

    def _generate_one(self, text):
        """Generate a single TTS file via ElevenLabs if not already cached."""
        path = self._tts_path(text)
        if path.exists():
            return path
        print(f"   🔊 Caching: {text[:50]}...")
        try:
            self._elevenlabs_tts(text, str(path))
        except Exception as e:
            print(f"   ⚠️  TTS cache error: {e}")
            return None
        return path

    def _elevenlabs_tts(self, text, out_path):
        """Call ElevenLabs TTS API and save the audio to out_path."""
        import urllib.request
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{TTS_VOICE_ID}"
        body = json.dumps({
            "text": text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {
                "stability": TTS_STABILITY,
                "similarity_boost": 0.75,
                "style": TTS_STYLE,
                "use_speaker_boost": True,
            },
        }).encode()
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json",
            "xi-api-key": drawbox_core.ELEVENLABS_API_KEY,
            "Accept": "audio/mpeg",
        })
        resp = urllib.request.urlopen(req, timeout=30)
        with open(out_path, "wb") as f:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                f.write(chunk)

    def _warm_up(self):
        """Pre-generate all static voice lines at startup."""
        print("🔊 Warming up voice cache...")
        # Generate a short silent MP3 to wake the USB speaker before speech.
        # Uses ffmpeg to create pure silence — no API call, no vocal artifacts.
        self._silence_path = CACHE_DIR / "silence.mp3"
        if not self._silence_path.exists():
            print("   🔇 Generating speaker wake-up file...")
            try:
                subprocess.run([
                    "ffmpeg", "-y", "-f", "lavfi",
                    "-i", "anullsrc=r=44100:cl=mono",
                    "-t", "0.5", "-q:a", "9",
                    str(self._silence_path),
                ], check=True, capture_output=True)
            except Exception as e:
                print(f"   ⚠️  Could not generate silence file: {e}")
                self._silence_path = None
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
            # Play a short silence first to wake the USB speaker,
            # then the actual speech. mpg123 supports multiple files.
            if self._silence_path and self._silence_path.exists():
                subprocess.run(
                    ["mpg123", "-q", str(self._silence_path), str(path)],
                    check=True)
            else:
                time.sleep(0.5)  # fallback: sleep if no silence file
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
        tmp_path = None
        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
            tmp_path = tmp.name
            tmp.close()
            self._elevenlabs_tts(text, tmp_path)
            self._play_file(tmp_path)
        except Exception as e:
            print(f"   TTS error: {e}")
            subprocess.run(["espeak", text], check=False)
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def play_jokes_until_done(self, thread):
        """Play one random joke, then wait for the thread to finish."""
        jokes = list(self._joke_paths)
        if not jokes:
            thread.join()
            return
        joke = random.choice(jokes)
        if thread.is_alive():
            print("   🃏 Telling a joke...")
            self._play_file(joke)
        # Wait silently for generation to finish
        thread.join()

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
    print("📝 Transcribing (whisper-1)...")
    t0 = time.time()
    with open(path, "rb") as f:
        r = drawbox_core.client.audio.transcriptions.create(model="whisper-1", file=f)
    os.unlink(path)
    print(f'   ✅ Transcribed in {time.time()-t0:.1f}s: "{r.text}"')
    return r.text

# ── MAIN ────────────────────────────────────────
is_busy = False

def main():
    global is_busy

    # Re-read keys in case they were updated via the web dashboard
    apply_api_keys()

    print("─" * 48)
    print("  🔧 DrawBox Configuration")
    print("─" * 48)
    print(f"   Image model : {IMAGE_MODEL}")
    print(f"   OpenAI key  : {'✅ ' + drawbox_core.OPENAI_API_KEY[:8] + '...' if drawbox_core.OPENAI_API_KEY else '❌ missing'}")
    print(f"   ElevenLabs  : {'✅ ' + drawbox_core.ELEVENLABS_API_KEY[:8] + '...' if drawbox_core.ELEVENLABS_API_KEY else '❌ missing'}")
    print(f"   Replicate   : {'✅ ' + drawbox_core.REPLICATE_API_TOKEN[:8] + '...' if drawbox_core.REPLICATE_API_TOKEN else '⚠️  missing'}")
    print(f"   Gemini key  : {'✅ ' + drawbox_core.GEMINI_API_KEY[:8] + '...' if drawbox_core.GEMINI_API_KEY else '⚠️  missing'}")
    print(f"   Keys source : {API_KEYS_FILE} ({'exists' if API_KEYS_FILE.exists() else 'not found, using env'})")
    print(f"   Settings    : {SETTINGS_FILE} ({'exists' if SETTINGS_FILE.exists() else 'not found, using defaults'})")

    if not drawbox_core.OPENAI_API_KEY:
        print("❌ OPENAI_API_KEY not set. Export it or add to your service file.")
        return
    if not drawbox_core.ELEVENLABS_API_KEY:
        print("❌ ELEVENLABS_API_KEY not set. Add it via the web dashboard.")
        return

    # Warn about missing model keys but don't exit — only matters at generation time
    if IMAGE_MODEL == "flux-schnell" and not drawbox_core.REPLICATE_API_TOKEN:
        print("⚠️  REPLICATE_API_TOKEN not set (needed for flux-schnell). Set it via the web dashboard or service file.")
    if IMAGE_MODEL == "nano-banana" and not drawbox_core.GEMINI_API_KEY:
        print("⚠️  GEMINI_API_KEY not set (needed for nano-banana). Set it via the web dashboard or service file.")

    # Safety mode ON by default (create sentinel file if missing)
    SAFETY_MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not SAFETY_MODE_FILE.exists():
        SAFETY_MODE_FILE.touch()
    print(f"   Safety filter: {'ON' if safety_mode_enabled() else 'OFF'}")

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
                elif safety_mode_enabled() and not is_safe(text):
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
                    gen_t0 = time.time()
                    def gen_worker():
                        try: gen_result[0] = generate_image(text)
                        except Exception as e: gen_error[0] = e
                    gen_thread = threading.Thread(
                        target=gen_worker, daemon=True)
                    gen_thread.start()
                    voice.play_jokes_until_done(gen_thread)
                    gen_duration = time.time() - gen_t0
                    if gen_error[0]:
                        raise gen_error[0]
                    img = gen_result[0]

                    # PRINTING
                    voice.play("printing")
                    print_image(img)
                    log_print_event(text, IMAGE_MODEL, gen_duration)

                    # DONE
                    voice.play("done")

            except Exception as e:
                print(f"❌ Error: {e}")
                traceback.print_exc()
                voice.play("error")
            finally:
                is_busy = False
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n👋 Shutting down")

if __name__ == "__main__":
    main()
