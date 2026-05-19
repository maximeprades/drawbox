#!/usr/bin/env python3
"""DrawBox — the button-driven coloring-page printer for kids.

Runs on a Raspberry Pi 5 wired to:
  - a big red arcade button (GPIO 17)
  - a USB microphone (44.1 kHz)
  - a USB speaker (mpg123 playback)
  - a USB-connected laser printer (CUPS)

Press → listen → transcribe → safety-check → generate → print → done.
Hold for 5 seconds to reboot.
"""

import hashlib
import json
import logging
import os
import random
import subprocess
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import sounddevice as sd
import soundfile as sf
from gpiozero import Button as GpioButton

import drawbox_core
from drawbox_core import (
    API_KEYS_FILE, CACHE_DIR, DEFAULT_JOKES, DEFAULT_VOICE_LINES, IMAGE_MODEL,
    SAFETY_MODE_FILE, SETTINGS_FILE, apply_api_keys, generate_image,
    has_please, is_safe, load_settings, load_scripts, log_print_event,
    mask_key, please_mode_enabled, print_image, safety_mode_enabled,
)

log = logging.getLogger("drawbox")

# ── CONFIG ──────────────────────────────────────
BUTTON_PIN = 17
SAMPLE_RATE = 44100              # CHANGEEK USB mic only supports 44.1kHz
RECORD_SECONDS = 10
REBOOT_HOLD_SEC = 5              # hold button this long to trigger reboot
MIN_RECORDING_SEC = 0.5          # anything shorter is silence/accidental press

# TTS settings — overridden by ~/.drawbox/web_settings.json at startup
TTS_VOICE_ID = "xNtG3W2oqJs0cJZuTyBc"
TTS_STABILITY = 0.5
TTS_STYLE = 0.0
TTS_SIMILARITY_BOOST = 0.75

# ── VOICE LINES ─────────────────────────────────
# Built from the shared defaults; the dashboard's Scripts page can override
# either single strings or "one-per-line" pick-lists.

def _build_voice_lines():
    """Return {key: str or [str, ...]} from defaults + on-disk overrides."""
    defaults = {k: v["text"] for k, v in DEFAULT_VOICE_LINES.items()}
    saved = load_scripts()["voice_lines"]
    out = {}
    for key, text in {**defaults, **saved}.items():
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        out[key] = lines if len(lines) > 1 else (lines[0] if lines else "")
    return out


def _load_jokes():
    saved = load_scripts()["jokes"]
    jokes = [j for j in saved if j.strip()] or list(DEFAULT_JOKES)
    return jokes


VOICE_LINES = _build_voice_lines()
KIDS_JOKES = _load_jokes()


def _apply_tts_settings():
    """Pull TTS voice + tuning from settings.json on disk."""
    global TTS_VOICE_ID, TTS_STABILITY, TTS_STYLE
    s = load_settings()
    if s.get("tts_voice_id"):
        TTS_VOICE_ID = s["tts_voice_id"]
    TTS_STABILITY = max(0.0, min(1.0, float(s.get("tts_stability", TTS_STABILITY))))
    TTS_STYLE = max(0.0, min(1.0, float(s.get("tts_style", TTS_STYLE))))


_apply_tts_settings()


class VoiceFeedback:
    """Pre-generates ElevenLabs TTS lines once, caches .mp3 by content hash."""

    def __init__(self):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._cache = {}            # key → Path or [Path, ...]
        self._joke_paths = []
        self._silence_path = None
        self._warm_up()

    def _tts_path(self, text):
        """Cache filename keyed on voice + tuning + text."""
        h = hashlib.md5(
            f"{TTS_VOICE_ID}:{TTS_STABILITY}:{TTS_STYLE}:{text}".encode()
        ).hexdigest()[:12]
        return CACHE_DIR / f"{h}.mp3"

    def _generate_one(self, text):
        path = self._tts_path(text)
        if path.exists():
            return path
        log.info("caching TTS: %s…", text[:50])
        try:
            self._elevenlabs_tts(text, str(path))
        except Exception:
            log.exception("ElevenLabs TTS failed for %r", text[:80])
            return None
        return path

    def _elevenlabs_tts(self, text, out_path):
        import urllib.request
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{TTS_VOICE_ID}"
        body = json.dumps({
            "text": "... " + text,   # leading pause helps the USB speaker wake
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {
                "stability": TTS_STABILITY,
                "similarity_boost": TTS_SIMILARITY_BOOST,
                "style": TTS_STYLE,
                "use_speaker_boost": True,
            },
        }).encode()
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json",
            "xi-api-key": drawbox_core.ELEVENLABS_API_KEY,
            "Accept": "audio/mpeg",
        })
        with urllib.request.urlopen(req, timeout=30) as resp, open(out_path, "wb") as f:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                f.write(chunk)

    def _warm_up(self):
        log.info("warming up voice cache…")
        self._silence_path = self._ensure_silence_file()
        for key, val in VOICE_LINES.items():
            if isinstance(val, list):
                paths = [self._generate_one(line) for line in val]
                self._cache[key] = [p for p in paths if p]
            else:
                p = self._generate_one(val)
                if p:
                    self._cache[key] = p
        uncached = [j for j in KIDS_JOKES if not self._tts_path(j).exists()]
        if uncached:
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(self._generate_one, uncached))
        self._joke_paths = [self._tts_path(j) for j in KIDS_JOKES
                            if self._tts_path(j).exists()]
        log.info("voice cache ready: %d jokes, %d lines",
                 len(self._joke_paths), len(self._cache))

    def _ensure_silence_file(self):
        """Generate a 0.5s silent MP3 once. We prepend it to every playback so
        the USB speaker wakes before the first syllable lands."""
        path = CACHE_DIR / "silence.mp3"
        if path.exists():
            return path
        try:
            subprocess.run([
                "ffmpeg", "-y", "-f", "lavfi",
                "-i", "anullsrc=r=44100:cl=mono",
                "-t", "0.5", "-q:a", "9",
                str(path),
            ], check=True, capture_output=True)
            return path
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            log.warning("could not generate speaker wake-up file: %s", e)
            return None

    def play(self, key, block=True):
        """Play a cached line by key. Falls back to live TTS if not cached."""
        entry = self._cache.get(key)
        if entry is None:
            text = VOICE_LINES.get(key, key)
            if isinstance(text, list):
                text = random.choice(text)
            self._play_live(text)
            return
        path = random.choice(entry) if isinstance(entry, list) else entry
        if not path:
            return
        if block:
            self._play_file(path)
        else:
            threading.Thread(target=self._play_file, args=(path,), daemon=True).start()

    def play_dynamic(self, text, block=True):
        """Speak arbitrary text (not cached) — used for dynamic messages."""
        if block:
            self._play_live(text)
        else:
            threading.Thread(target=self._play_live, args=(text,), daemon=True).start()

    def _play_file(self, path):
        try:
            if self._silence_path and self._silence_path.exists():
                subprocess.run(
                    ["mpg123", "-q", str(self._silence_path), str(path)],
                    check=True)
            else:
                time.sleep(0.5)  # fallback: let the speaker wake up
                subprocess.run(["mpg123", "-q", str(path)], check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            # Fallback to aplay if mpg123 isn't available or refuses to play
            try:
                subprocess.run(["aplay", "-q", str(path)], check=False)
            except FileNotFoundError:
                log.error("no audio player available (tried mpg123 and aplay)")

    def _play_live(self, text):
        log.info("speaking: %s", text)
        fd, tmp_path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        try:
            self._elevenlabs_tts(text, tmp_path)
            self._play_file(tmp_path)
        except Exception:
            log.exception("live TTS failed; falling back to espeak")
            subprocess.run(["espeak", text], check=False)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def play_jokes_until_done(self, thread):
        """Tell one random joke if the generation thread is still working."""
        if not self._joke_paths:
            thread.join()
            return
        if thread.is_alive():
            log.info("telling a joke")
            self._play_file(random.choice(self._joke_paths))
        thread.join()


# ── RECORD ──────────────────────────────────────
def record_audio(seconds=RECORD_SECONDS):
    """Record for ``seconds`` seconds and return a WAV path, or None if silent."""
    log.info("recording for %ds", seconds)
    frames = []

    def cb(indata, _frame_count, _time_info, _status):
        frames.append(indata.copy())

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, callback=cb):
        time.sleep(seconds)
    if not frames:
        return None
    audio = np.concatenate(frames)
    duration = len(audio) / SAMPLE_RATE
    if duration < MIN_RECORDING_SEC:
        return None
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    sf.write(path, audio, SAMPLE_RATE)
    log.info("recorded %.1fs to %s", duration, path)
    return path


# ── TRANSCRIBE ──────────────────────────────────
def transcribe(path):
    log.info("transcribing with whisper-1")
    t0 = time.time()
    try:
        with open(path, "rb") as f:
            r = drawbox_core.client.audio.transcriptions.create(
                model="whisper-1", file=f)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    log.info("transcribed in %.1fs: %r", time.time() - t0, r.text)
    return r.text


# ── MAIN ────────────────────────────────────────
_busy = False
_busy_lock = threading.Lock()


def _set_busy(value):
    global _busy
    with _busy_lock:
        _busy = value


def _is_busy():
    with _busy_lock:
        return _busy


def _print_config():
    log.info("DrawBox configuration:")
    log.info("  image_model = %s", IMAGE_MODEL)
    log.info("  openai      = %s", mask_key(drawbox_core.OPENAI_API_KEY) or "missing")
    log.info("  elevenlabs  = %s", mask_key(drawbox_core.ELEVENLABS_API_KEY) or "missing")
    log.info("  replicate   = %s", mask_key(drawbox_core.REPLICATE_API_TOKEN) or "missing")
    log.info("  gemini      = %s", mask_key(drawbox_core.GEMINI_API_KEY) or "missing")
    log.info("  keys file   = %s (%s)",
             API_KEYS_FILE,
             "present" if API_KEYS_FILE.exists() else "missing, using env")
    log.info("  settings    = %s (%s)",
             SETTINGS_FILE,
             "present" if SETTINGS_FILE.exists() else "missing, using defaults")


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    apply_api_keys()
    _print_config()

    if not drawbox_core.OPENAI_API_KEY:
        log.error("OPENAI_API_KEY not set; aborting.")
        return
    if not drawbox_core.ELEVENLABS_API_KEY:
        log.error("ELEVENLABS_API_KEY not set; aborting.")
        return

    if IMAGE_MODEL == "flux-schnell" and not drawbox_core.REPLICATE_API_TOKEN:
        log.warning("REPLICATE_API_TOKEN not set (needed for flux-schnell).")
    if IMAGE_MODEL == "nano-banana" and not drawbox_core.GEMINI_API_KEY:
        log.warning("GEMINI_API_KEY not set (needed for nano-banana).")

    # Safety mode defaults ON
    SAFETY_MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not SAFETY_MODE_FILE.exists():
        SAFETY_MODE_FILE.touch()
    log.info("safety filter: %s", "on" if safety_mode_enabled() else "off")

    btn = GpioButton(BUTTON_PIN, pull_up=True, bounce_time=0.1)
    voice = VoiceFeedback()

    log.info("ready — press the red button")
    voice.play("ready")

    try:
        while True:
            btn.wait_for_press()
            if _handle_long_press(btn, voice):
                return  # reboot triggered
            if _is_busy():
                voice.play("busy", block=False)
                continue
            _handle_press(voice)
            time.sleep(0.5)
    except KeyboardInterrupt:
        log.info("shutting down")


def _handle_long_press(btn, voice):
    """If the user holds the button >= REBOOT_HOLD_SEC, reboot. Returns True
    iff a reboot was kicked off (caller should exit)."""
    held = 0.0
    while btn.is_pressed:
        time.sleep(0.1)
        held += 0.1
        if held >= REBOOT_HOLD_SEC:
            log.info("reboot requested (held %.1fs)", held)
            voice.play("reboot")
            subprocess.run(["sudo", "reboot"], check=False)
            return True
    return False


def _handle_press(voice):
    """Run the listen → generate → print pipeline once. Never raises."""
    _set_busy(True)
    try:
        voice.play("listening")
        path = record_audio()
        if not path:
            voice.play("too_short")
            return

        text = transcribe(path)
        if not text or len(text.strip()) < 2:
            voice.play("too_short")
            return

        if safety_mode_enabled() and not is_safe(text):
            log.info("blocked: %r", text)
            voice.play("blocked")
            return

        if please_mode_enabled() and not has_please(text):
            log.info("missing please: %r", text)
            voice.play("say_please")
            return

        voice.play("thinking")
        img, duration = _generate_with_jokes(text, voice)
        voice.play("printing")
        print_image(img)
        log_print_event(text, IMAGE_MODEL, duration)
        voice.play("done")
    except Exception:
        log.exception("error in press handler")
        traceback.print_exc()
        voice.play("error")
    finally:
        _set_busy(False)


def _generate_with_jokes(text, voice):
    """Run image generation in a background thread and tell jokes meanwhile."""
    result, error = [None], [None]

    def worker():
        try:
            result[0] = generate_image(text)
        except Exception as e:
            error[0] = e

    t0 = time.time()
    th = threading.Thread(target=worker, daemon=True)
    th.start()
    voice.play_jokes_until_done(th)
    if error[0]:
        raise error[0]
    return result[0], time.time() - t0


if __name__ == "__main__":
    main()
