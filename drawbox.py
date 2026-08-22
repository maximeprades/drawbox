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
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
from urllib.error import HTTPError

import numpy as np
import sounddevice as sd
import soundfile as sf
from gpiozero import Button as GpioButton
from openai import APIStatusError, RateLimitError

import drawbox_core
from drawbox_core import (
    API_KEYS_FILE, CACHE_DIR, DEFAULT_JOKES, DEFAULT_VOICE_LINES, IMAGE_MODEL,
    SAFETY_MODE_FILE, SETTINGS_FILE, apply_api_keys, generate_image,
    contains_poop, has_please, is_pairing_command, is_safe, load_settings,
    load_scripts, log_print_event, mask_key, open_pairing_window,
    parse_admin_poop_command, please_mode_enabled, poop_mode_enabled,
    print_image, print_pairing_code, safety_mode_enabled,
    set_poop_mode_enabled,
)

log = logging.getLogger("drawbox")

# ── CONFIG ──────────────────────────────────────
BUTTON_PIN = 17
SAMPLE_RATE = 44100              # CHANGEEK USB mic only supports 44.1kHz
USB_MIC_NAME_HINTS = ("USB", "PnP", "CHANGEEK")  # matched against sd.query_devices() names
RECORD_SECONDS = 10
REBOOT_HOLD_SEC = 5              # hold button this long to trigger reboot
MIN_RECORDING_SEC = 0.5          # anything shorter is silence/accidental press

# TTS settings — overridden by ~/.drawbox/web_settings.json at startup
VOICE_PROVIDER = "gateway"
TTS_VOICE_ID = "alloy"
ELEVENLABS_VOICE_ID = "xNtG3W2oqJs0cJZuTyBc"
TTS_STABILITY = 0.5
TTS_STYLE = 0.0
TTS_SIMILARITY_BOOST = 0.75
GROK_VOICE_ID = "eve"

# The drawbox_core key attribute each voice provider needs; keeps the startup
# key gate in lockstep with the providers _synthesize can dispatch to.
TTS_PROVIDER_KEYS = {
    "gateway": "AI_GATEWAY_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
    "grok": "XAI_API_KEY",
}

_TTS_WAKE_PREFIX = "... "   # leading pause helps the USB speaker wake

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
    """Pull the voice provider + per-provider tuning from settings.json."""
    global VOICE_PROVIDER, TTS_VOICE_ID, ELEVENLABS_VOICE_ID, GROK_VOICE_ID, \
        TTS_STABILITY, TTS_STYLE
    s = load_settings()  # load_settings already clamps voice_provider
    VOICE_PROVIDER = s["voice_provider"]
    TTS_VOICE_ID = drawbox_core.resolve_tts_voice(s.get("tts_voice_id"))
    if isinstance(s.get("elevenlabs_voice_id"), str) and s["elevenlabs_voice_id"].strip():
        ELEVENLABS_VOICE_ID = s["elevenlabs_voice_id"].strip()
    if isinstance(s.get("grok_voice_id"), str) and s["grok_voice_id"].strip():
        GROK_VOICE_ID = s["grok_voice_id"].strip()
    TTS_STABILITY = max(0.0, min(1.0, float(s.get("tts_stability", TTS_STABILITY))))
    TTS_STYLE = max(0.0, min(1.0, float(s.get("tts_style", TTS_STYLE))))


_apply_tts_settings()


class VoiceFeedback:
    """Caches TTS lines as .mp3 keyed by content hash.

    Construction is cheap and offline; call :meth:`warm_up` once at startup
    to generate the audio cache (network).
    """

    def __init__(self, provider="gateway"):
        # Explicit so dispatch never depends on whatever the host's
        # ~/.drawbox/web_settings.json happens to say (tests included).
        self.provider = provider
        self._cache = {}            # key → Path or non-empty [Path, ...]
        self._joke_paths = []
        self._silence_path = None
        self._tts_rate_limited_until = 0.0
        self._tts_rate_limit_logged = False

    def _tts_path(self, text):
        """Cache filename keyed on provider + voice + tuning + text."""
        if self.provider == "elevenlabs":
            # Byte-identical to the historical ElevenLabs format so existing
            # on-disk caches stay valid.
            key = f"{ELEVENLABS_VOICE_ID}:{TTS_STABILITY}:{TTS_STYLE}:{text}"
        elif self.provider == "grok":
            key = f"grok:{GROK_VOICE_ID}:{text}"
        else:
            key = f"{TTS_VOICE_ID}:{text}"
        h = hashlib.md5(key.encode()).hexdigest()[:12]
        return CACHE_DIR / f"{h}.mp3"

    def _generate_one(self, text):
        path = self._tts_path(text)
        if path.exists():
            return path
        return path if self._synthesize(text, str(path)) else None

    def _synthesize(self, text, out_path):
        """Fetch TTS audio for ``text`` into ``out_path``.

        The single place that talks to the TTS provider: owns the rate-limit
        gate and all error handling. Returns True iff audio was written.
        """
        if self._tts_rate_limit_remaining() > 0:
            return False
        log.info("generating TTS: %s…", text[:50])
        try:
            if self.provider == "elevenlabs":
                self._elevenlabs_tts(text, out_path)
            elif self.provider == "grok":
                self._grok_tts(text, out_path)
            else:
                self._gateway_tts(text, out_path)
            return True
        except RateLimitError as e:
            self._handle_tts_rate_limit(e)
        except HTTPError as e:
            # urllib providers (elevenlabs, grok) surface HTTP errors here.
            if e.code == 429:
                self._handle_tts_rate_limit(e)
            else:
                log.warning("%s TTS HTTP %s failed for %r",
                            self.provider, e.code, text[:80])
        except APIStatusError as e:
            log.warning("%s TTS HTTP %s failed for %r",
                        self.provider, e.status_code, text[:80])
        except Exception as e:
            log.warning("%s TTS failed for %r: %s", self.provider, text[:80], e)
            # A mid-download failure can leave a partial file; don't let it
            # poison the cache.
            try:
                os.unlink(out_path)
            except OSError:
                pass
        return False

    def _tts_rate_limit_remaining(self):
        return max(0.0, self._tts_rate_limited_until - time.time())

    def _handle_tts_rate_limit(self, error):
        retry_after = 60.0
        # SDK errors carry headers on .response; urllib's HTTPError on .headers.
        headers = getattr(getattr(error, "response", None), "headers", None)
        if headers is None:
            headers = getattr(error, "headers", None)
        raw = None
        if headers is not None:
            raw = headers.get("retry-after") or headers.get("Retry-After")
        try:
            retry_after = max(1.0, float(raw))
        except (TypeError, ValueError):
            pass
        self._tts_rate_limited_until = max(
            self._tts_rate_limited_until,
            time.time() + retry_after,
        )
        if not self._tts_rate_limit_logged:
            log.warning(
                "%s TTS rate-limited (HTTP 429); using cached audio "
                "and espeak fallback for %.0fs",
                self.provider,
                self._tts_rate_limit_remaining(),
            )
            self._tts_rate_limit_logged = True

    def _gateway_tts(self, text, out_path):
        if not drawbox_core.client:
            raise RuntimeError("AI_GATEWAY_API_KEY not set")
        response = drawbox_core.client.audio.speech.create(
            model=drawbox_core.GATEWAY_TTS_MODEL,
            voice=TTS_VOICE_ID,
            input=_TTS_WAKE_PREFIX + text,
            response_format="mp3",
        )
        response.write_to_file(out_path)

    def _elevenlabs_tts(self, text, out_path):
        self._fetch_audio(
            f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
            {
                "text": _TTS_WAKE_PREFIX + text,
                "model_id": "eleven_multilingual_v2",
                "voice_settings": {
                    "stability": TTS_STABILITY,
                    "similarity_boost": TTS_SIMILARITY_BOOST,
                    "style": TTS_STYLE,
                    "use_speaker_boost": True,
                },
            },
            {"xi-api-key": drawbox_core.ELEVENLABS_API_KEY},
            out_path,
        )

    def _grok_tts(self, text, out_path):
        self._fetch_audio(
            "https://api.x.ai/v1/tts",
            {
                "text": _TTS_WAKE_PREFIX + text,
                "voice_id": GROK_VOICE_ID,
                "language": "en",
            },
            {"Authorization": f"Bearer {drawbox_core.XAI_API_KEY}"},
            out_path,
        )

    def _fetch_audio(self, url, payload, auth_headers, out_path):
        import urllib.request
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
            **auth_headers,
        })
        with urllib.request.urlopen(req, timeout=30) as resp, open(out_path, "wb") as f:
            shutil.copyfileobj(resp, f, length=8192)

    def warm_up(self):
        """Generate and cache every voice line and joke. Needs the network;
        call once at startup, before the button loop."""
        log.info("warming up voice cache…")
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._silence_path = self._ensure_silence_file()
        for key, val in VOICE_LINES.items():
            if isinstance(val, list):
                paths = []
                for line in val:
                    p = self._generate_one(line)
                    if p:
                        paths.append(p)
                if paths:
                    self._cache[key] = paths
            else:
                p = self._generate_one(val)
                if p:
                    self._cache[key] = p
        for joke in KIDS_JOKES:
            self._generate_one(joke)
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
            if self._synthesize(text, tmp_path):
                self._play_file(tmp_path)
            else:
                self._speak_with_espeak(text)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _speak_with_espeak(self, text):
        try:
            subprocess.run(["espeak", text], check=False)
        except FileNotFoundError:
            log.error("no fallback speech command available (espeak)")

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
def _candidate_input_devices():
    """Return input-device candidates in preference order.

    USB cards can be renumbered after unplug/reboot. PortAudio may still list a
    stale ``hw:N,0`` device that ALSA refuses to open, so recording tries every
    plausible candidate instead of trusting the first USB-looking match.
    """
    try:
        devices = sd.query_devices()
    except Exception as e:
        log.warning("could not query audio devices: %s", e)
        return [None]

    candidates = []

    def add(device):
        if device not in candidates:
            candidates.append(device)

    for i, d in enumerate(devices):
        if d.get("max_input_channels", 0) <= 0:
            continue
        name = d.get("name", "")
        if any(kw in name for kw in USB_MIC_NAME_HINTS):
            add(i)

    try:
        default_input = sd.default.device[0]
    except Exception:
        default_input = None
    if isinstance(default_input, int) and default_input >= 0:
        add(default_input)

    for i, d in enumerate(devices):
        if d.get("max_input_channels", 0) > 0:
            add(i)

    if candidates:
        add(None)  # last resort: PortAudio's default device
    return candidates


def _input_device_label(device):
    if device is None:
        return "default input device"
    try:
        info = sd.query_devices(device)
        return f"input device {device}: {info.get('name', 'unknown')}"
    except Exception:
        return f"input device {device}"


def record_audio(seconds=RECORD_SECONDS):
    """Record for ``seconds`` seconds and return a WAV path, or None if silent."""
    log.info("recording for %ds", seconds)

    last_error = None
    for device in _candidate_input_devices():
        frames = []
        statuses = []
        device_label = _input_device_label(device)

        def cb(indata, _frame_count, _time_info, status):
            if status:
                statuses.append(status)
            frames.append(indata.copy())

        try:
            log.info("trying %s", device_label)
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                                callback=cb, device=device):
                time.sleep(seconds)
        except Exception as e:
            last_error = e
            log.warning("could not record from %s: %s", device_label, e)
            continue

        if statuses:
            log.debug("input stream status from %s: %s",
                      device_label, "; ".join(str(s) for s in statuses))
        if not frames:
            log.warning("no audio frames captured from %s", device_label)
            continue
        audio = np.concatenate(frames)
        duration = len(audio) / SAMPLE_RATE
        if duration < MIN_RECORDING_SEC:
            log.warning("recording from %s too short: %.1fs",
                        device_label, duration)
            continue
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        sf.write(path, audio, SAMPLE_RATE)
        log.info("recorded %.1fs to %s", duration, path)
        return path

    if last_error:
        log.warning("all input devices failed; last error: %s", last_error)
    else:
        log.warning("no usable input devices found; check microphone connection")
    return None


# ── TRANSCRIBE ──────────────────────────────────
def transcribe(path):
    log.info("transcribing with whisper-1")
    t0 = time.time()
    try:
        with open(path, "rb") as f:
            r = drawbox_core.client.audio.transcriptions.create(
                model=drawbox_core.GATEWAY_STT_MODEL, file=f)
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
    log.info("  voice       = %s", VOICE_PROVIDER)
    log.info("  ai_gateway  = %s", mask_key(drawbox_core.AI_GATEWAY_API_KEY) or "missing")
    log.info("  elevenlabs  = %s", mask_key(drawbox_core.ELEVENLABS_API_KEY) or "missing")
    log.info("  xai         = %s", mask_key(drawbox_core.XAI_API_KEY) or "missing")
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

    if not drawbox_core.AI_GATEWAY_API_KEY:
        log.error("AI_GATEWAY_API_KEY not set; aborting.")
        return
    voice_key_name = TTS_PROVIDER_KEYS[VOICE_PROVIDER]
    if not getattr(drawbox_core, voice_key_name):
        log.error("%s not set (needed for %s voice); aborting.",
                  voice_key_name, VOICE_PROVIDER)
        return

    drawbox_core.ensure_safety_mode_default()
    log.info("safety filter: %s", "on" if safety_mode_enabled() else "off")

    btn = GpioButton(BUTTON_PIN, pull_up=True, bounce_time=0.1)
    voice = VoiceFeedback(provider=VOICE_PROVIDER)
    voice.warm_up()

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

        admin_poop_action = parse_admin_poop_command(text)
        if admin_poop_action:
            enabled = admin_poop_action == "enable"
            set_poop_mode_enabled(enabled)
            log.info("poop mode %s via voice command", "enabled" if enabled else "disabled")
            voice.play("poop_mode_enabled" if enabled else "poop_mode_disabled")
            return

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
                message = ("Pairing mode! I printed the code for you. It is "
                           + spoken_code + ". Type it within two minutes.")
            else:
                message = ("Pairing mode! The code is " + spoken_code
                           + ". Type it within two minutes.")
            voice.play_dynamic(message)
            return

        if not poop_mode_enabled() and contains_poop(text):
            log.info("poop blocked: %r", text)
            voice.play("poop_blocked")
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
