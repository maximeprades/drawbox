#!/usr/bin/env python3
"""DrawBox web dashboard — Flask control panel for the Pi."""

import base64
import hashlib
import io
import json
import logging
import os
import re
import secrets
import select
import shutil
import subprocess
import tempfile
import threading
import time as _time
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, render_template, request, send_file
from werkzeug.middleware.proxy_fix import ProxyFix

import drawbox_core
from drawbox_core import (
    API_KEYS_FILE, DRAWBOX_DIR, IMAGE_MODEL, LAST_IMAGE_FILE,
    PLEASE_MODE_FILE, PRINT_LOG_FILE,
    OPENAI_TTS_VOICES, PRINTER_TYPES, SAFETY_MODE_FILE, SERIAL_BAUDS,
    SUPPORTED_MODELS, VOICE_PROVIDERS, _load_api_keys,
    _write_secure_json, apply_api_keys, contains_poop, default_scripts,
    ensure_safety_mode_default, resolve_tts_voice,
    device_for_token, generate_image, has_please, is_safe,
    is_valid_device_token, list_paired_devices, load_scripts,
    load_settings, log_print_event,
    mask_key, please_mode_enabled, poop_blocked_message, poop_mode_enabled,
    print_image, redeem_pairing_code, revoke_paired_device,
    safety_mode_enabled, save_scripts, save_settings, set_poop_mode_enabled,
    transcribe_audio,
)

log = logging.getLogger("drawbox.web")

# ── CONFIG ───────────────────────────────────────
REPO_DIR = Path.home() / "drawbox-repo"
GUIDE_PATH = Path.home() / "drawbox-guide.html"
SIMULATOR_PATH = Path.home() / "drawbox-simulator.html"
LOG_STREAM_KEEPALIVE_S = 15

# ── IMAGE GENERATION STATE ────────────────────────
_gen_lock = threading.Lock()

# Raw-audio uploads from the ESP32 voice box. 30 s of 16 kHz 16-bit mono is
# under 1 MB; the cap only exists to bounce runaway or hostile bodies.
VOICE_AUDIO_MAX_BYTES = 8 * 1024 * 1024
# A WAV header alone is 44 bytes; anything this small can't hold speech.
VOICE_AUDIO_MIN_BYTES = 1024

# ── DIAGNOSTICS ALLOWLIST ────────────────────────
DIAGNOSTIC_COMMANDS = {
    "printer_status": ["lpstat", "-p", "-d"],
    "printer_queue":  ["lpstat", "-o"],
    "audio_inputs":   ["arecord", "-l"],
    "audio_outputs":  ["aplay", "-l"],
    "service_status": ["systemctl", "status", "drawbox"],
    "web_service":    ["systemctl", "status", "drawbox-web"],
    "disk_usage":     ["df", "-h", "/"],
    "uptime":         ["uptime"],
    "temperature":    ["vcgencmd", "measure_temp"],
    "wifi_status":    ["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi"],
    "wifi_list":      ["nmcli", "dev", "wifi", "list"],
    "rpi_connect":    ["rpi-connect", "status"],
    "journal_errors": ["journalctl", "-u", "drawbox", "-p", "err", "-n", "20", "--no-pager"],
    "cups_log":       ["tail", "-n", "30", "/var/log/cups/error_log"],
}

# ── FLASK APP ────────────────────────────────────
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
# Backstop for bodies without a Content-Length (e.g. chunked uploads),
# which the per-route content_length check can't see. Nothing legitimate
# sends more than a voice WAV.
app.config["MAX_CONTENT_LENGTH"] = VOICE_AUDIO_MAX_BYTES


@app.errorhandler(413)
def request_too_large(_e):
    return jsonify(ok=False, error="Request too large.", code="too_large"), 413

# Origins that may call this dashboard from another host (the multi-Pi
# "hub" page). Anchored regex on the *host* portion of the Origin header —
# not a substring match on the whole URL, which would let
# ``evil.drawbox.attacker.com`` through.
#
# Defaults cover the canonical Cloudflare Pages deployment. Power users can
# extend the list with ``DRAWBOX_ALLOWED_ORIGINS`` (comma-separated exact
# hostnames or simple ``*.domain.tld`` patterns).
_DEFAULT_ORIGIN_PATTERN = re.compile(
    r"^(?:[a-z0-9-]+\.)*drawbox\.pages\.dev$", re.IGNORECASE,
)


def _compile_extra_origins(spec):
    out = []
    for raw in (spec or "").split(","):
        host = raw.strip().lower()
        if not host:
            continue
        if host.startswith("*."):
            suffix = re.escape(host[2:])
            out.append(re.compile(rf"^(?:[a-z0-9-]+\.)+{suffix}$", re.IGNORECASE))
        else:
            out.append(re.compile(rf"^{re.escape(host)}$", re.IGNORECASE))
    return out


_EXTRA_ORIGIN_PATTERNS = _compile_extra_origins(os.environ.get("DRAWBOX_ALLOWED_ORIGINS"))


def _allowed_origin(origin):
    """Return the origin string if it's allowed by our CORS policy, else ''."""
    if not origin:
        return ""
    try:
        parsed = urlparse(origin)
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return ""
    host = parsed.hostname
    if _DEFAULT_ORIGIN_PATTERN.match(host):
        return origin
    if any(p.match(host) for p in _EXTRA_ORIGIN_PATTERNS):
        return origin
    return ""


@app.after_request
def add_cors(response):
    allowed = _allowed_origin(request.headers.get("Origin", ""))
    if allowed:
        response.headers["Access-Control-Allow-Origin"] = allowed
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Vary"] = "Origin"
    return response


@app.route("/api/<path:path>", methods=["OPTIONS"])
def cors_preflight(path):
    return "", 204


# ── DEVICE AUTH ──────────────────────────────────
# Every /api route requires a paired-device token (see drawbox_core's
# DEVICE PAIRING section), except pairing itself and the health check
# used by deploy scripts and the post-update poller.
_PUBLIC_API_PATHS = {"/api/pair", "/api/status"}


# EventSource and plain <a download> navigation can't send headers, so the
# log endpoints (and only those) may pass the token as a query parameter.
_QUERY_TOKEN_PATHS = {"/api/logs", "/api/logs/download"}


def _request_token():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):].strip()
    if request.path in _QUERY_TOKEN_PATHS:
        return request.args.get("token", "")
    return ""


@app.before_request
def require_device_token():
    if request.method == "OPTIONS" or not request.path.startswith("/api/"):
        return None
    if request.path in _PUBLIC_API_PATHS:
        return None
    if is_valid_device_token(_request_token()):
        return None
    return jsonify(ok=False, error="Not paired"), 401


# /api/pair is the only unauthenticated write path, and the pairing
# window's 5-attempt budget is global — an anonymous flood could burn it
# faster than the household can type the code. A per-IP hourly budget,
# checked before the window file is ever touched, caps what one attacker
# can burn while leaving the household's own budget intact.
_PAIR_ATTEMPTS = {}
_PAIR_HOURLY_LIMIT = 10


def _peer_ip():
    """The TCP peer, not request.remote_addr: pairing throttle and
    heartbeat IP both key off this. ProxyFix honors a client
    supplied X-Forwarded-For, which would hand every spoofed header a
    fresh identity. Behind the tunnel this collapses to one shared
    peer, which is fine — pairing is a rare, LAN-side ceremony."""
    orig = request.environ.get("werkzeug.proxy_fix.orig") or {}
    return orig.get("REMOTE_ADDR") or request.remote_addr or ""


def _pair_throttled(ip):
    now = _time.time()
    times = [t for t in _PAIR_ATTEMPTS.get(ip, []) if now - t < 3600]
    throttled = len(times) >= _PAIR_HOURLY_LIMIT
    if not throttled:
        times.append(now)
    _PAIR_ATTEMPTS[ip] = times
    return throttled


@app.route("/api/pair", methods=["POST"])
def api_pair():
    if _pair_throttled(_peer_ip()):
        return jsonify(ok=False,
                       error="Too many attempts — wait a moment."), 429
    data = _request_dict()
    if data is None:
        return jsonify(ok=False, error="Invalid JSON body"), 400
    code = data.get("code")
    name = data.get("name")
    if not isinstance(code, str) or not code.strip():
        return jsonify(ok=False, error="Pairing code required"), 400
    code = code.strip()
    if code.isdigit():
        code = code.zfill(6)  # spoken codes keep leading zeros; typed may not
    token = redeem_pairing_code(code,
                                name if isinstance(name, str) else "")
    if not token:
        return jsonify(ok=False, error="Wrong or expired code. Press the "
                       "button, say \u201cauthorize\u201d, and try the new "
                       "code."), 403
    return jsonify(ok=True, token=token)


@app.route("/api/pair/devices")
def api_paired_devices():
    return jsonify(devices=[
        {"id": d.get("id", ""), "name": d.get("name", ""),
         "created": d.get("created", "")}
        for d in list_paired_devices()
    ])


@app.route("/api/pair/devices/<device_id>", methods=["DELETE"])
def api_revoke_device(device_id):
    if not revoke_paired_device(device_id):
        return jsonify(ok=False, error="Unknown device"), 404
    status = _load_device_status()
    if status.pop(device_id, None) is not None:
        _write_secure_json(drawbox_core.DEVICE_STATUS_FILE, status)
    return jsonify(ok=True)


# ── DEVICE HEARTBEAT ─────────────────────────────
# The ESP32 box posts here every 60 s. Stored on disk, not in memory:
# gunicorn runs two workers, and a per-process dict made online status
# flip depending on which worker answered. A concurrent heartbeat from
# a second box can lose one read-modify-write; the next beat heals it.
# 150 s of silence is offline. The response carries the knobs the box
# should apply (volume, brightness, record window) plus the voice
# cache_hash, so a box notices script/voice edits within one beat.
_DEVICE_ONLINE_S = 150


def _load_device_status():
    try:
        data = json.loads(drawbox_core.DEVICE_STATUS_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _optional_clamped_int(value, lo, hi):
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return None


@app.route("/api/device/heartbeat", methods=["POST"])
def api_device_heartbeat():
    device = device_for_token(_request_token())
    if device is None:
        return jsonify(ok=False, error="Not paired"), 401
    body = _request_dict()
    if body is None:
        return jsonify(ok=False, error="Invalid JSON body"), 400
    # Heap/PSRAM arrive in bytes; RSSI is dBm. The caps are what an
    # ESP32-class board can actually report, so a garbage heartbeat
    # can't blow out the dashboard.
    rec = {
        "version": str(body.get("version") or "")[:32],
        "uptime_s": _optional_clamped_int(body.get("uptime_s"), 0, 10 * 365 * 24 * 3600),
        "rssi": _optional_clamped_int(body.get("rssi"), -200, 20),
        "heap": _optional_clamped_int(body.get("heap"), 0, 64 * 1024 * 1024),
        "psram": _optional_clamped_int(body.get("psram"), 0, 64 * 1024 * 1024),
        "cache_lines": _optional_clamped_int(body.get("cache_lines"), 0, 1_000_000),
        "cache_ready": bool(body.get("cache_ready")),
        "ip": _peer_ip(),
        "ts": _time.time(),
    }
    status = _load_device_status()
    status[device.get("id", "")] = rec
    _write_secure_json(drawbox_core.DEVICE_STATUS_FILE, status)
    settings = load_settings()
    provider, voice_id, stability, style = _active_tts()
    return jsonify(ok=True,
                   volume=settings["esp32_volume"],
                   brightness=settings["esp32_brightness"],
                   record_seconds=settings["record_seconds"],
                   cache_hash=_voice_cache_hash(load_scripts(), provider,
                                                voice_id, stability, style))


@app.route("/api/devices")
def api_devices():
    now = _time.time()
    all_status = _load_device_status()
    caller = device_for_token(_request_token()) or {}
    out = []
    for d in list_paired_devices():
        rec = all_status.get(d.get("id"))
        if rec:
            age = now - rec["ts"]
            status = {k: v for k, v in rec.items() if k != "ts"}
            last_seen_s = int(age)
            online = age <= _DEVICE_ONLINE_S
        else:
            status = None
            last_seen_s = None
            online = False
        out.append({
            "id": d.get("id", ""),
            "name": d.get("name", ""),
            "created": d.get("created", ""),
            # The UI warns before a device revokes itself out of the
            # dashboard it is currently using.
            "self": d.get("id") == caller.get("id"),
            "online": online,
            "last_seen_s": last_seen_s,
            "status": status,
        })
    return jsonify(out)


# ── ROUTES ───────────────────────────────────────

@app.route("/")
def index():
    return render_template(
        "index.html",
        tts_voices=sorted(OPENAI_TTS_VOICES),
        gateway_models=drawbox_core.GATEWAY_IMAGE_CATALOG,
    )

@app.route("/guide")
def guide():
    if GUIDE_PATH.exists():
        return send_file(GUIDE_PATH)
    return "Guide not found. Copy drawbox-guide.html to ~/", 404

@app.route("/simulator")
def simulator():
    if SIMULATOR_PATH.exists():
        return send_file(SIMULATOR_PATH)
    return "Simulator not found. Copy drawbox-simulator.html to ~/", 404

# /api/status is public (deploy scripts poll it) and forks processes;
# the cache keeps an anonymous request loop from pegging the Pi.
_STATUS_CACHE = {"ts": 0.0, "payload": None}
_STATUS_CACHE_S = 5.0


@app.route("/api/status")
def api_status():
    now = _time.time()
    if _STATUS_CACHE["payload"] is not None and \
            now - _STATUS_CACHE["ts"] < _STATUS_CACHE_S:
        return jsonify(**_STATUS_CACHE["payload"])

    # Service status
    running = False
    try:
        svc = subprocess.run(["systemctl", "is-active", "drawbox"],
                             capture_output=True, text=True, timeout=5)
        running = svc.stdout.strip() == "active"
    except Exception:
        pass

    # Temperature
    temp = "--"
    try:
        t = subprocess.run(["vcgencmd", "measure_temp"],
                           capture_output=True, text=True, timeout=5)
        temp = t.stdout.strip().replace("temp=", "")
    except Exception:
        pass

    # Uptime
    up = "--"
    try:
        u = subprocess.run(["uptime", "-p"],
                           capture_output=True, text=True, timeout=5)
        up = u.stdout.strip().replace("up ", "")
    except Exception:
        pass

    # RPi Connect
    rpi = "unknown"
    try:
        rc = subprocess.run(["rpi-connect", "status"],
                            capture_output=True, text=True, timeout=5)
        out = rc.stdout.strip().lower()
        if "connected" in out:
            rpi = "connected"
        elif "not" in out or rc.returncode != 0:
            rpi = "off"
        else:
            rpi = out[:20]
    except FileNotFoundError:
        rpi = "not installed"
    except Exception:
        pass

    payload = dict(service_running=running, temperature=temp,
                   uptime=up, rpi_connect=rpi)
    # Stamped after the probes: a hung systemctl eats its 5s timeout
    # while gathering, and a before-the-probes stamp would already be
    # expired by the time it lands.
    _STATUS_CACHE.update(ts=_time.time(), payload=payload)
    return jsonify(**payload)

def _voice_line(key):
    """The kid-facing script line for ``key`` (dashboard overrides honored)."""
    return load_scripts()["voice_lines"].get(key) or \
        drawbox_core.DEFAULT_VOICE_LINES[key]["text"]


def _rejection_error(desc, blocked_message):
    """Run the poop → safety → please gates.

    Returns ``(voice_key, message)`` or None. The check order matches the
    button daemon's flow. ``blocked_message`` differs per caller: the
    dashboard explains, the voice box uses the spoken script line.
    """
    if not poop_mode_enabled() and contains_poop(desc):
        return "poop_blocked", poop_blocked_message()
    if safety_mode_enabled() and not is_safe(desc):
        return "blocked", blocked_message
    if please_mode_enabled() and not has_please(desc):
        return "say_please", _voice_line("say_please")
    return None


def _generate_and_print(desc, printer_type=None, source="web",
                        include_image=True):
    """Generate, print, and log one validated request; returns the response
    dict. The single pipeline behind both the dashboard and the voice box.

    Failures carry a machine-readable ``code`` so the ESP32 can pick its
    display message without parsing English.
    """
    if not _gen_lock.acquire(blocking=False):
        return {"ok": False, "error": "Already generating — please wait.",
                "code": "busy"}
    try:
        settings = load_settings()
        model = settings.get("image_model", IMAGE_MODEL)
        t0 = _time.time()
        path = generate_image(desc, model=model)
        duration = _time.time() - t0
        out = {"ok": True}
        if include_image:
            with open(path, "rb") as f:
                out["image"] = base64.b64encode(f.read()).decode()
        # A print failure after a successful (and paid-for) generation gets
        # its own message — "Generation failed" sent people hunting the
        # wrong bug, e.g. a missing serial port read as a model problem.
        try:
            print_image(path, printer_type=printer_type)
        except Exception as e:
            log.exception("print failed")
            # OSErrors name the user's own devices (missing serial port,
            # printer host down) — useful verbatim. Anything else (lp
            # command lines, PIL internals) stays in the journal.
            reason = str(e) if isinstance(e, OSError) else type(e).__name__
            out["ok"] = False
            out["error"] = f"Generated, but printing failed: {reason}"
            out["code"] = "print_failed"
            return out
        if printer_type in PRINTER_TYPES and printer_type != settings["printer_type"]:
            settings["printer_type"] = printer_type
            save_settings(settings)
        log_print_event(desc, model, duration, source=source)
        return out
    except Exception:
        log.exception("generate failed")
        return {"ok": False, "error": "Generation failed; check logs.",
                "code": "generate_failed"}
    finally:
        _gen_lock.release()


@app.route("/api/generate", methods=["POST"])
def api_generate():
    data = request.get_json(silent=True) or {}
    desc = data.get("description")
    if not isinstance(desc, str):
        return jsonify(ok=False, error="Please describe what to draw.")
    desc = desc.strip()
    if not desc:
        return jsonify(ok=False, error="Please describe what to draw.")
    if len(desc) > 500:
        return jsonify(ok=False, error="Description too long (max 500 chars).")
    rejected = _rejection_error(
        desc,
        "That description contains blocked words. "
        "Try something fun like an animal or a rainbow!",
    )
    if rejected:
        return jsonify(ok=False, error=rejected[1])

    printer_type = data.get("printer_type")
    if printer_type is not None and printer_type not in PRINTER_TYPES:
        return jsonify(ok=False, error="Unknown printer.")

    return jsonify(**_generate_and_print(desc, printer_type=printer_type))


# ── VOICE JOB (two-phase ESP32 flow) ─────────────
# With the X-DrawBox-Ack header, /api/voice/generate answers as soon as the
# transcript clears the gates — carrying a personalized ack clip key — and
# generation+print continue in a background thread. The box plays the ack,
# then long-polls /api/voice/result. Single-slot job state lives on disk
# (like device_status.json) because gunicorn runs two workers and the
# result poll may land on the other one.


def _voice_job_path():
    return drawbox_core.DRAWBOX_DIR / "voice_job.json"


def _read_voice_job():
    try:
        job = json.loads(_voice_job_path().read_text())
        return job if isinstance(job, dict) else None
    except (OSError, ValueError):
        return None


def _run_voice_job(job_id, desc):
    out = _generate_and_print(desc, source="esp32", include_image=False)
    if out.get("ok"):
        result = {"ok": True, "transcript": desc,
                  "message": _voice_line("printing"), "voice_key": "printing"}
    else:
        code = out.get("code", "generate_failed")
        key = "busy" if code == "busy" else "error"
        result = {"ok": False, "transcript": desc, "error": _voice_line(key),
                  "code": code, "voice_key": key}
    _write_secure_json(_voice_job_path(),
                       {"id": job_id, "status": "done", "ts": _time.time(),
                        "result": result})


@app.route("/api/voice/generate", methods=["POST"])
def api_voice_generate():
    """Request from the ESP32 voice box: raw audio in, print out.

    Body is the recorded audio itself (Content-Type: audio/wav), not JSON.
    Mirrors the button daemon's listen → transcribe → gate → generate →
    print flow, with the same spoken script lines as display messages.

    Legacy (no X-DrawBox-Ack header): blocks until printing starts and
    returns the final outcome — old firmware keeps working. Ack mode
    (X-DrawBox-Ack: 1, firmware >= 1.6.0): returns right after the gates
    with an ``ack_key`` clip to speak; the final outcome comes from
    /api/voice/result.
    """
    if (request.content_length or 0) > VOICE_AUDIO_MAX_BYTES:
        return jsonify(ok=False, error="Audio too large.",
                       code="too_large"), 413
    audio = request.get_data(cache=False)
    if len(audio) < VOICE_AUDIO_MIN_BYTES:
        return jsonify(ok=False, error=_voice_line("too_short"),
                       code="too_short", voice_key="too_short"), 400
    mime = request.mimetype or ""
    media_type = mime if mime.startswith("audio/") else "audio/wav"
    try:
        transcript = transcribe_audio(audio, media_type=media_type)
    except Exception:
        log.exception("voice transcription failed")
        return jsonify(ok=False, error=_voice_line("error"),
                       code="transcribe_failed", voice_key="error"), 502
    transcript = (transcript or "").strip()
    if len(transcript) < 2:
        return jsonify(ok=False, transcript=transcript,
                       error=_voice_line("too_short"), code="too_short",
                       voice_key="too_short")
    # Spoken admin commands work from this box too (closing the old drift
    # where "authorize" at the ESP32 did nothing). "blocked" hits fall
    # through to _rejection_error, which owns the legacy response shape.
    hit = drawbox_core.intercept_transcript(transcript)
    if hit and hit["action"] != "blocked":
        ack_key = None
        try:
            _, ack_key = _ensure_line_wav(hit["say"])
        except Exception:
            log.exception("intercept clip synthesis failed")
        return jsonify(ok=False, transcript=transcript, error=hit["say"],
                       code="intercepted", action=hit["action"],
                       voice_key=hit["voice_key"] or "", ack_key=ack_key)
    rejected = _rejection_error(transcript, _voice_line("blocked"))
    if rejected:
        voice_key, error = rejected
        return jsonify(ok=False, transcript=transcript, error=error,
                       code="rejected", voice_key=voice_key)

    if request.headers.get("X-DrawBox-Ack") == "1":
        return _start_voice_job(transcript[:500])

    out = _generate_and_print(transcript[:500], source="esp32",
                              include_image=False)
    if not out.get("ok"):
        code = out.get("code", "generate_failed")
        if code == "busy":
            line, voice_key = _voice_line("busy"), "busy"
        else:
            line, voice_key = _voice_line("error"), "error"
        return jsonify(ok=False, transcript=transcript, error=line, code=code,
                       voice_key=voice_key)
    return jsonify(ok=True, transcript=transcript,
                   message=_voice_line("printing"), voice_key="printing")


# A running job older than this is presumed dead (worker crash mid-job);
# generation itself is bounded well under it by the gunicorn timeout.
_VOICE_JOB_STALE_S = 180


def _start_voice_job(transcript):
    """Phase 1 of the ack flow: synthesize the ack, kick generation, return.

    Busy is decided from the on-disk job slot, not just the per-process
    lock: gunicorn runs two workers, and a second box hitting the other
    worker would otherwise clobber the single job file mid-generation and
    strand the first box's result poll on no_job (Bugbot, PR #39). The
    job thread's non-blocking lock acquire remains the in-process guard.
    """
    job = _read_voice_job()
    if job and job.get("status") == "running" and \
            _time.time() - job.get("ts", 0) < _VOICE_JOB_STALE_S:
        return jsonify(ok=False, transcript=transcript,
                       error=_voice_line("busy"), code="busy",
                       voice_key="busy")
    if _gen_lock.locked():
        return jsonify(ok=False, transcript=transcript,
                       error=_voice_line("busy"), code="busy",
                       voice_key="busy")
    # Claim the slot BEFORE ack synthesis: the LLM + TTS round trips take
    # seconds, plenty for a second box to pass the busy checks above and
    # clobber this job (Bugbot round 2). The remaining check-to-claim race
    # is sub-millisecond — fine for a household of two boxes.
    job_id = secrets.token_hex(8)
    _write_secure_json(_voice_job_path(),
                       {"id": job_id, "status": "running", "ts": _time.time()})
    ack_key = None
    if load_settings()["natural_ack"]:
        try:
            ack_text = drawbox_core.generate_ack_text(transcript)
            _, ack_key = _ensure_line_wav(ack_text)
            log.info("ack for %r: %s", transcript[:60], ack_text)
        except Exception:
            # Box falls back to its cached "thinking" line — never fatal.
            log.exception("ack synthesis failed")
    threading.Thread(target=_run_voice_job, args=(job_id, transcript),
                     daemon=True).start()
    return jsonify(ok=True, transcript=transcript, ack_key=ack_key,
                   job=job_id)


@app.route("/api/voice/result")
def api_voice_result():
    """Long-poll the single-slot voice job until it finishes.

    The firmware passes ``id`` from phase 1 so a stale result from an
    earlier job can't be mistaken for this one, and ``timeout`` (seconds,
    capped) mostly so tests don't wait 90 s.
    """
    job_id = request.args.get("id", "")
    try:
        timeout = min(90.0, max(0.0, float(request.args.get("timeout", 90))))
    except (TypeError, ValueError):
        timeout = 90.0
    deadline = _time.time() + timeout
    while True:
        job = _read_voice_job()
        if not job or (job_id and job.get("id") != job_id):
            return jsonify(ok=False, error="No drawing in progress",
                           code="no_job", voice_key="error"), 404
        if job.get("status") == "done":
            return jsonify(**(job.get("result") or {}))
        if _time.time() >= deadline:
            return jsonify(ok=False, error=_voice_line("error"),
                           code="timeout", voice_key="error"), 504
        _time.sleep(0.25)


_CLIP_KEY_RE = re.compile(r"^[0-9a-f]{12}$")


@app.route("/api/voice/clip")
def api_voice_clip():
    """Serve one synthesized 16 kHz WAV by cache key (the phase-1 ack_key)."""
    key = request.args.get("k", "")
    if not _CLIP_KEY_RE.match(key):
        return jsonify(ok=False, error="Unknown clip"), 404
    wav_path = drawbox_core.CACHE_DIR / f"{key}.16k.wav"
    if not wav_path.exists():
        return jsonify(ok=False, error="Unknown clip"), 404
    return send_file(wav_path, mimetype="audio/wav", max_age=0)


def _variant_count(text):
    n = sum(1 for ln in (text or "").split("\n") if ln.strip())
    return max(1, n)


def _line_variants(text):
    return [ln.strip() for ln in (text or "").split("\n") if ln.strip()]


def _active_tts():
    """Provider, voice id, and ElevenLabs knobs, matching the daemon."""
    s = load_settings()
    provider = s["voice_provider"]
    if provider == "elevenlabs":
        raw = s.get("elevenlabs_voice_id")
        voice_id = raw.strip() if isinstance(raw, str) and raw.strip() else \
            drawbox_core.DEFAULT_SETTINGS["elevenlabs_voice_id"]
    elif provider == "grok":
        raw = s.get("grok_voice_id")
        voice_id = raw.strip() if isinstance(raw, str) and raw.strip() else \
            drawbox_core.DEFAULT_SETTINGS["grok_voice_id"]
    else:
        voice_id = resolve_tts_voice(s.get("tts_voice_id"))
    stability = max(0.0, min(1.0, float(s.get("tts_stability", 0.5))))
    style = max(0.0, min(1.0, float(s.get("tts_style", 0.0))))
    return provider, voice_id, stability, style


def _voice_cache_hash(scripts, provider, voice_id, stability, style):
    """12-hex digest the voice box uses to drop its on-device audio cache.

    The payload is a JSON list so field order cannot drift: provider, voice,
    ElevenLabs knobs, sorted voice-line texts, then jokes in list order.
    """
    payload = [
        provider,
        voice_id,
        stability,
        style,
        [[k, scripts["voice_lines"][k]] for k in sorted(scripts["voice_lines"])],
        scripts["jokes"],
    ]
    blob = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    return hashlib.md5(blob.encode()).hexdigest()[:12]


def _write_bytes_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@app.route("/api/voice/lines")
def api_voice_lines():
    scripts = load_scripts()
    provider, voice_id, stability, style = _active_tts()
    body = {k: _variant_count(v) for k, v in scripts["voice_lines"].items()}
    body["jokes"] = len(scripts["jokes"])
    body["cache_hash"] = _voice_cache_hash(
        scripts, provider, voice_id, stability, style)
    return jsonify(body)


@app.route("/api/voice/line")
def api_voice_line():
    key = request.args.get("key", "")
    try:
        index = int(request.args.get("i", 0))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Unknown line"), 404
    scripts = load_scripts()
    if key == "joke":
        items = scripts["jokes"]
        if index < 0 or index >= len(items):
            return jsonify(ok=False, error="Unknown line"), 404
        text = items[index]
    elif key in scripts["voice_lines"]:
        items = _line_variants(scripts["voice_lines"][key])
        if index < 0 or index >= len(items):
            return jsonify(ok=False, error="Unknown line"), 404
        text = items[index]
    else:
        return jsonify(ok=False, error="Unknown line"), 404

    try:
        wav_path, _ = _ensure_line_wav(text)
    except Exception:
        log.exception("voice line synthesis failed")
        return jsonify(ok=False, error="Synthesis failed"), 503
    return send_file(wav_path, mimetype="audio/wav", max_age=0)


def _ensure_line_wav(text):
    """Synthesize ``text`` with the active TTS into the shared voice cache;
    returns (wav_path, cache_key). Raises on synthesis/transcode failure.

    Serves both the script-line route and the per-request ack clips. Ack
    clips are unique per transcript so they accumulate (~50 KB each); the
    software-update deploy already wipes the cache, which is enough.
    """
    provider, voice_id, stability, style = _active_tts()
    cache_key = drawbox_core.tts_cache_key(
        text, provider, voice_id, stability, style)
    cache_dir = drawbox_core.CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    mp3_path = cache_dir / f"{cache_key}.mp3"
    wav_path = cache_dir / f"{cache_key}.16k.wav"

    if not mp3_path.exists():
        try:
            data = drawbox_core.synthesize_speech(
                text, provider, voice_id, stability, style)
            _write_bytes_atomic(mp3_path, data)
        except Exception:
            try:
                mp3_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    if not wav_path.exists():
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(mp3_path),
                 "-ar", "16000", "-ac", "1", "-f", "wav", str(wav_path)],
                check=True, capture_output=True, timeout=30,
            )
        except Exception:
            try:
                wav_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    return wav_path, cache_key

@app.route("/api/last-image")
def api_last_image():
    """The most recent generated page (button or web), for the dashboard."""
    if not LAST_IMAGE_FILE.exists():
        return jsonify(ok=False, error="Nothing generated yet"), 404
    return send_file(LAST_IMAGE_FILE, mimetype="image/png", max_age=0)

# Each log stream holds a journalctl -f and a worker thread until the
# client leaves; unbounded streams could starve every other route.
_LOG_STREAM_SLOTS = threading.BoundedSemaphore(2)


@app.route("/api/logs")
def api_logs():
    def stream():
        # Acquired inside the generator: a generator that never starts
        # never runs its finally, so acquiring before the Response could
        # leak a slot when a client vanishes mid-headers.
        if not _LOG_STREAM_SLOTS.acquire(blocking=False):
            yield ": too many log streams open\n\n"
            return
        proc = None
        try:
            # Cap the backfill at 1000 lines so the live view stays
            # responsive on a chatty Pi. Full 24h via /api/logs/download.
            proc = subprocess.Popen(
                ["journalctl", "-u", "drawbox", "-u", "drawbox-web",
                 "--since", "24 hours ago", "-n", "1000", "-f", "--no-pager"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            fd = proc.stdout.fileno()
            # journalctl -f never ends, and a vanished client is only
            # noticed when a write fails. On a quiet journal that pinned
            # worker threads forever and saturated gunicorn (2026-08-30
            # outage). The periodic SSE comment forces that write.
            # Raw os.read, not proc.stdout.read: no BufferedReader between
            # select and the fd, so ready means readable and b"" means EOF.
            buf = b""
            while True:
                ready, _, _ = select.select([fd], [], [],
                                            LOG_STREAM_KEEPALIVE_S)
                if not ready:
                    yield ": keepalive\n\n"
                    continue
                chunk = os.read(fd, 65536)
                if chunk == b"":
                    break  # journalctl exited
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    yield "data: %s\n\n" % line.decode("utf-8", "replace").rstrip()
        finally:
            _LOG_STREAM_SLOTS.release()
            if proc is not None:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
    return Response(
        stream(), mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.route("/api/logs/download")
def api_logs_download():
    try:
        proc = subprocess.run(
            ["journalctl", "-u", "drawbox", "-u", "drawbox-web",
             "--since", "24 hours ago", "--no-pager"],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return Response("journalctl timed out", mimetype="text/plain", status=504)
    text = proc.stdout or proc.stderr or "(no logs)"
    ts = _time.strftime("%Y%m%d-%H%M%S")
    return send_file(
        io.BytesIO(text.encode("utf-8")),
        mimetype="text/plain",
        as_attachment=True,
        download_name=f"drawbox-logs-{ts}.txt",
    )

@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "GET":
        return jsonify(load_settings())
    data = _request_dict()
    if data is None:
        return jsonify(ok=False, error="Invalid JSON body"), 400
    settings = load_settings()
    try:
        if isinstance(data.get("coloring_prompt"), str):
            settings["coloring_prompt"] = data["coloring_prompt"][:5000]
        if data.get("image_model") in SUPPORTED_MODELS:
            settings["image_model"] = data["image_model"]
        if data.get("voice_provider") in VOICE_PROVIDERS:
            settings["voice_provider"] = data["voice_provider"]
        if data.get("stt_provider") in drawbox_core.STT_PROVIDERS:
            settings["stt_provider"] = data["stt_provider"]
        if "natural_ack" in data:
            if data["natural_ack"] is not True and data["natural_ack"] is not False:
                raise ValueError("natural_ack must be true or false")
            settings["natural_ack"] = data["natural_ack"]
        if "conversation_mode" in data:
            if data["conversation_mode"] is not True and data["conversation_mode"] is not False:
                raise ValueError("conversation_mode must be true or false")
            settings["conversation_mode"] = data["conversation_mode"]
        if isinstance(data.get("tts_voice_id"), str) and data["tts_voice_id"].strip():
            settings["tts_voice_id"] = resolve_tts_voice(data["tts_voice_id"])
        if isinstance(data.get("elevenlabs_voice_id"), str) and data["elevenlabs_voice_id"].strip():
            settings["elevenlabs_voice_id"] = data["elevenlabs_voice_id"].strip()[:64]
        if isinstance(data.get("grok_voice_id"), str) and data["grok_voice_id"].strip():
            settings["grok_voice_id"] = data["grok_voice_id"].strip()[:64]
        if "tts_stability" in data:
            settings["tts_stability"] = max(0.0, min(1.0, float(data["tts_stability"])))
        if "tts_style" in data:
            settings["tts_style"] = max(0.0, min(1.0, float(data["tts_style"])))
        if "record_seconds" in data:
            settings["record_seconds"] = max(3, min(30, int(data["record_seconds"])))
        if "printer_type" in data:
            if data["printer_type"] not in PRINTER_TYPES:
                raise ValueError(f"unknown printer_type: {data['printer_type']!r}")
            settings["printer_type"] = data["printer_type"]
        if "serial_port" in data:
            port = data["serial_port"]
            if not isinstance(port, str) or not port.strip():
                raise ValueError(f"serial_port must be a non-empty string: {port!r}")
            port = port.strip()
            # Device nodes only: the print path opens this with O_RDWR as
            # the pi user, so an arbitrary path would spray raster bytes
            # at any TTY it can open.
            if not re.match(r"^/dev/[A-Za-z0-9._/:-]+$", port):
                raise ValueError(f"serial_port must be a /dev device: {port!r}")
            settings["serial_port"] = port
        if "serial_baud" in data:
            baud = int(data["serial_baud"])
            if baud not in SERIAL_BAUDS:
                raise ValueError(f"serial_baud must be one of {SERIAL_BAUDS}: {baud}")
            settings["serial_baud"] = baud
        if "tcp_host" in data:
            host = data["tcp_host"]
            if not isinstance(host, str) or not host.strip():
                raise ValueError(f"tcp_host must be a non-empty string: {host!r}")
            settings["tcp_host"] = host.strip()
        if "tcp_port" in data:
            tcp_port = int(data["tcp_port"])
            if not 1 <= tcp_port <= 65535:
                raise ValueError(f"tcp_port must be 1-65535: {tcp_port}")
            settings["tcp_port"] = tcp_port
        if "esp32_volume" in data:
            vol = int(data["esp32_volume"])
            if not 0 <= vol <= 100:
                raise ValueError(f"esp32_volume must be 0-100: {vol}")
            settings["esp32_volume"] = vol
        if "esp32_brightness" in data:
            bri = int(data["esp32_brightness"])
            if not 10 <= bri <= 255:
                raise ValueError(f"esp32_brightness must be 10-255: {bri}")
            settings["esp32_brightness"] = bri
    except (TypeError, ValueError) as e:
        return jsonify(ok=False, error=f"Invalid value: {e}"), 400
    save_settings(settings)
    return jsonify(ok=True)

@app.route("/api/scripts", methods=["GET", "POST"])
def api_scripts():
    if request.method == "GET":
        scripts = load_scripts()
        scripts["defaults"] = default_scripts()
        return jsonify(scripts)
    data = _request_dict()
    if data is None:
        return jsonify(ok=False, error="Invalid JSON body"), 400
    if data.get("reset"):
        from drawbox_core import SCRIPTS_FILE
        SCRIPTS_FILE.unlink(missing_ok=True)
        return jsonify(ok=True)
    save_scripts(data)
    return jsonify(ok=True)

def _request_dict():
    """Return the JSON request body if it's a dict, else None."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def _toggle_sentinel(path):
    data = _request_dict() or {}
    if data.get("enabled") is True:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    elif data.get("enabled") is False:
        path.unlink(missing_ok=True)
    else:
        return jsonify(ok=False, error='"enabled" must be true or false'), 400
    return jsonify(ok=True, enabled=path.exists())


def _sentinel_route(path):
    if request.method == "GET":
        return jsonify(enabled=path.exists())
    return _toggle_sentinel(path)


@app.route("/api/please-mode", methods=["GET", "POST"])
def api_please_mode():
    return _sentinel_route(PLEASE_MODE_FILE)


@app.route("/api/safety-mode", methods=["GET", "POST"])
def api_safety_mode():
    return _sentinel_route(SAFETY_MODE_FILE)

@app.route("/api/poop-mode", methods=["GET", "POST"])
def api_poop_mode():
    if request.method == "GET":
        return jsonify(enabled=poop_mode_enabled())
    data = _request_dict() or {}
    enabled = data.get("enabled")
    if enabled is not True and enabled is not False:
        return jsonify(ok=False, error='"enabled" must be true or false'), 400
    return jsonify(ok=True, enabled=set_poop_mode_enabled(enabled))

@app.route("/api/keys", methods=["GET", "POST"])
def api_keys():
    if request.method == "GET":
        keys = _load_api_keys()
        return jsonify({k: mask_key(v, head=4, tail=4) for k, v in keys.items()})
    data = _request_dict()
    if data is None:
        return jsonify(ok=False, error="Invalid JSON body"), 400
    try:
        existing = json.loads(API_KEYS_FILE.read_text()) if API_KEYS_FILE.exists() else {}
        if not isinstance(existing, dict):
            existing = {}
    except (OSError, ValueError):
        existing = {}
    clean = {}
    for k in drawbox_core.API_KEY_NAMES:
        prior = existing.get(k)
        if isinstance(prior, str) and prior.strip():
            clean[k] = prior.strip()
        val = data.get(k)
        if isinstance(val, str) and val.strip():
            clean[k] = val.strip()
    _write_secure_json(API_KEYS_FILE, clean)
    apply_api_keys()
    return jsonify(ok=True)


_WIFI_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# NetworkManager rejects connection.autoconnect-priority outside this range.
_WIFI_PRIORITY_MIN = -999
_WIFI_PRIORITY_MAX = 999
_WIFI_PRIORITY_STEP = 10
# Leave headroom under 999 so a newly saved network can sit above the list.
_WIFI_PRIORITY_TOP = 990


def _clamp_wifi_priority(priority):
    return max(_WIFI_PRIORITY_MIN, min(_WIFI_PRIORITY_MAX, int(priority)))


def _validate_wifi_text(value, field, max_len, allow_empty=False):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"Invalid {field}")
    value = value.strip()
    if not value and not allow_empty:
        raise ValueError(f"{field} required")
    if len(value) > max_len:
        raise ValueError(f"{field} too long")
    if any(ord(c) < 32 for c in value):
        raise ValueError(f"Invalid characters in {field}")
    return value


def _nmcli_split(line):
    """Split nmcli -t output, honoring backslash-escaped colons."""
    fields, buf, escaped = [], [], False
    for ch in line:
        if escaped:
            buf.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ":":
            fields.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if escaped:
        buf.append("\\")
    fields.append("".join(buf))
    return fields


def _parse_saved_wifi_profiles(output):
    """Parse ``nmcli -t con show`` summary lines into WiFi profile dicts.

    The summary listing has no SSID column, so ``ssid`` starts as the profile
    name; ``_saved_wifi_profiles`` overlays real SSIDs afterwards.
    """
    profiles = []
    for line in (output or "").splitlines():
        if not line.strip():
            continue
        parts = _nmcli_split(line)
        parts += [""] * (4 - len(parts))
        name, uuid, typ, priority = parts[:4]
        if typ.lower() not in {"wifi", "802-11-wireless"}:
            continue
        try:
            prio = int(priority or 0)
        except ValueError:
            prio = 0
        profiles.append({
            "name": name,
            "uuid": uuid,
            "ssid": name,
            "priority": prio,
        })
    profiles.sort(key=lambda p: (-p["priority"], p["name"].lower()))
    return profiles


def _wifi_ssids_by_uuid(uuids):
    """Fetch SSIDs for saved connections in one nmcli call: {uuid: ssid}.

    ``nmcli -g a,b con show ID...`` prints one value per line — uuid then
    ssid for each connection, in argument order.
    """
    uuids = [u for u in uuids if _WIFI_UUID_RE.match(u or "")]
    if not uuids:
        return {}
    r = subprocess.run(
        ["nmcli", "-g", "connection.uuid,802-11-wireless.ssid",
         "con", "show", *uuids],
        capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        log.warning("could not read WiFi SSIDs: %s",
                    r.stderr.strip() or r.stdout.strip() or "nmcli failed")
        return {}
    values = [_nmcli_split(line)[0] for line in r.stdout.splitlines()]
    if len(values) != 2 * len(uuids):
        log.warning("unexpected nmcli output shape for WiFi SSIDs "
                    "(%d lines for %d connections)", len(values), len(uuids))
        return {}
    return dict(zip(values[0::2], values[1::2]))


def _saved_wifi_profiles():
    r = subprocess.run(
        ["nmcli", "-t", "-f", "NAME,UUID,TYPE,AUTOCONNECT-PRIORITY", "con", "show"],
        capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip() or "nmcli failed")
    profiles = _parse_saved_wifi_profiles(r.stdout)
    ssids = _wifi_ssids_by_uuid([p["uuid"] for p in profiles])
    for profile in profiles:
        profile["ssid"] = ssids.get(profile["uuid"]) or profile["ssid"]
    return profiles


def _next_wifi_priority(profiles=None):
    profiles = profiles if profiles is not None else _saved_wifi_profiles()
    return _clamp_wifi_priority(
        max((p["priority"] for p in profiles), default=0) + _WIFI_PRIORITY_STEP
    )


def _run_sudo_nmcli(args, timeout=30):
    r = subprocess.run(["sudo", "nmcli", *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip() or "nmcli failed")
    return r


def _set_wifi_priority(uuid, priority):
    if not _WIFI_UUID_RE.match(uuid or ""):
        raise ValueError("Invalid network id")
    _run_sudo_nmcli(["con", "modify", uuid, "connection.autoconnect", "yes",
                     "connection.autoconnect-priority",
                     str(_clamp_wifi_priority(priority))])


def _promote_wifi_profile_for_ssid(ssid):
    try:
        profiles = _saved_wifi_profiles()
        match = next((p for p in profiles if p["ssid"] == ssid or p["name"] == ssid), None)
        if match:
            _set_wifi_priority(match["uuid"], _next_wifi_priority(profiles))
    except Exception as e:
        log.warning("could not promote WiFi profile for %s: %s", ssid, e)

@app.route("/api/wifi/networks")
def api_wifi_networks():
    try:
        r = subprocess.run(
            ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"],
            capture_output=True, text=True, timeout=15)
        networks = []
        seen = set()
        for line in r.stdout.strip().splitlines():
            parts = line.split(":")
            if len(parts) >= 3 and parts[0] and parts[0] not in seen:
                seen.add(parts[0])
                networks.append({
                    "ssid": parts[0],
                    "signal": int(parts[1]) if parts[1].isdigit() else 0,
                    "security": parts[2] or "Open"
                })
        networks.sort(key=lambda n: n["signal"], reverse=True)
        return jsonify(networks=networks)
    except Exception as e:
        return jsonify(networks=[], error=str(e))

@app.route("/api/wifi/connect", methods=["POST"])
def api_wifi_connect():
    data = request.get_json(silent=True) or {}
    ssid = data.get("ssid") if isinstance(data.get("ssid"), str) else ""
    password = data.get("password") if isinstance(data.get("password"), str) else ""
    ssid, password = ssid.strip(), password.strip()
    if not ssid:
        return jsonify(ok=False, error="SSID required")
    if len(ssid) > 64 or len(password) > 128:
        return jsonify(ok=False, error="SSID or password too long")
    if any(ord(c) < 32 for c in ssid + password):
        return jsonify(ok=False, error="Invalid characters in SSID or password")
    try:
        cmd = ["sudo", "nmcli", "dev", "wifi", "connect", ssid]
        if password:
            cmd += ["password", password]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            _promote_wifi_profile_for_ssid(ssid)
            return jsonify(ok=True, message=f"Connected to {ssid}")
        return jsonify(ok=False, error=r.stderr.strip() or r.stdout.strip())
    except subprocess.TimeoutExpired:
        return jsonify(ok=False, error="nmcli timed out")
    except OSError as e:
        return jsonify(ok=False, error=str(e))


@app.route("/api/wifi/saved", methods=["GET", "POST"])
def api_wifi_saved():
    if request.method == "GET":
        try:
            return jsonify(saved=_saved_wifi_profiles())
        except Exception as e:
            return jsonify(saved=[], error=str(e))

    data = request.get_json(silent=True) or {}
    try:
        ssid = _validate_wifi_text(data.get("ssid"), "SSID", 64)
        password = _validate_wifi_text(data.get("password", ""), "password", 128, allow_empty=True)
        name = _validate_wifi_text(data.get("name") or ssid, "network name", 64)
        profiles = _saved_wifi_profiles()
        priority = _next_wifi_priority(profiles)
        existing = next((p for p in profiles if p["uuid"] and (p["ssid"] == ssid or p["name"] == name)), None)
        if existing:
            args = ["con", "modify", existing["uuid"],
                    "connection.autoconnect", "yes",
                    "connection.autoconnect-priority", str(priority),
                    "802-11-wireless.ssid", ssid]
            if password:
                args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
            else:
                # Blank password means an open network; clear any prior WPA settings.
                args += ["wifi-sec.key-mgmt", "", "wifi-sec.psk", ""]
            _run_sudo_nmcli(args)
            return jsonify(ok=True, updated=True, uuid=existing["uuid"])

        args = ["con", "add", "type", "wifi", "ifname", "wlan0",
                "con-name", name, "ssid", ssid,
                "connection.autoconnect", "yes",
                "connection.autoconnect-priority", str(priority)]
        if password:
            args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
        _run_sudo_nmcli(args)
        return jsonify(ok=True, updated=False)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    except subprocess.TimeoutExpired:
        return jsonify(ok=False, error="nmcli timed out")
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.route("/api/wifi/saved/reorder", methods=["POST"])
def api_wifi_saved_reorder():
    data = request.get_json(silent=True) or {}
    uuids = data.get("uuids")
    if not isinstance(uuids, list) or not uuids:
        return jsonify(ok=False, error="Ordered network ids required"), 400
    if len(uuids) != len(set(uuids)):
        return jsonify(ok=False, error="Duplicate network ids"), 400
    if any(not isinstance(u, str) or not _WIFI_UUID_RE.match(u) for u in uuids):
        return jsonify(ok=False, error="Invalid network id"), 400
    try:
        start = _WIFI_PRIORITY_TOP
        for idx, uuid in enumerate(uuids):
            _set_wifi_priority(uuid, start - (idx * _WIFI_PRIORITY_STEP))
        return jsonify(ok=True)
    except subprocess.TimeoutExpired:
        return jsonify(ok=False, error="nmcli timed out")
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.route("/api/wifi/saved/<uuid>", methods=["DELETE"])
def api_wifi_saved_delete(uuid):
    if not _WIFI_UUID_RE.match(uuid or ""):
        return jsonify(ok=False, error="Invalid network id"), 400
    try:
        _run_sudo_nmcli(["con", "delete", uuid])
        return jsonify(ok=True)
    except subprocess.TimeoutExpired:
        return jsonify(ok=False, error="nmcli timed out")
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@app.route("/api/diagnostics", methods=["POST"])
def api_diagnostics():
    data = request.get_json(silent=True) or {}
    cmd_key = data.get("command")
    if not isinstance(cmd_key, str) or cmd_key not in DIAGNOSTIC_COMMANDS:
        return jsonify(error=f"Unknown command: {cmd_key!r}"), 400
    cmd = DIAGNOSTIC_COMMANDS[cmd_key]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        output = r.stdout
        if r.stderr:
            output += "\n" + r.stderr
        return jsonify(output=output.strip() or "(no output)")
    except FileNotFoundError:
        return jsonify(output=f"Command not found: {cmd[0]}")
    except subprocess.TimeoutExpired:
        return jsonify(output="Command timed out after 15 seconds.")
    except OSError as e:
        return jsonify(error=str(e)), 500

@app.route("/api/test/speaker", methods=["POST"])
def api_test_speaker():
    # Find the USB speaker card number from aplay -l
    aplay = subprocess.run(["aplay", "-l"], capture_output=True, text=True)
    card_match = re.search(r"card (\d+).*(?:USB|Speaker)", aplay.stdout, re.IGNORECASE)
    if not card_match:
        return jsonify(message=f"No USB speaker detected.\naplay -l output:\n{aplay.stdout.strip() or '(empty)'}")
    card = card_match.group(1)
    lines = [f"USB speaker found on card {card}."]

    # Check if ~/.asoundrc matches
    asoundrc = Path.home() / ".asoundrc"
    if asoundrc.exists():
        text = asoundrc.read_text()
        m = re.search(r"defaults\.pcm\.card\s+(\d+)", text)
        if m:
            configured = m.group(1)
            if configured != card:
                lines.append(f"⚠️  ~/.asoundrc says card {configured} but speaker is on card {card} — fix with:")
                lines.append(f"   printf 'defaults.pcm.card {card}\\ndefaults.ctl.card {card}\\n' > ~/.asoundrc")
            else:
                lines.append(f"~/.asoundrc correctly set to card {card}.")
    else:
        lines.append("~/.asoundrc not found.")

    # Play test tone directly on the detected card, bypassing ~/.asoundrc
    try:
        r = subprocess.run(
            ["speaker-test", "-D", f"plughw:{card},0", "-c", "1", "-t", "sine", "-f", "440", "-l", "1"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            lines.append("✅ Tone played successfully — you should have heard a beep.")
        else:
            lines.append(f"❌ speaker-test failed:\n{r.stderr.strip() or r.stdout.strip()}")
    except FileNotFoundError:
        lines.append("speaker-test not found. Run: sudo apt install alsa-utils")
    except subprocess.TimeoutExpired:
        lines.append("speaker-test timed out.")
    return jsonify(message="\n".join(lines))

@app.route("/api/test/mic", methods=["POST"])
def api_test_mic():
    import struct
    import wave
    # mkstemp avoids the mktemp race; we close the fd immediately because
    # arecord wants to own the file.
    fd, tmp = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        r = subprocess.run(
            ["arecord", "-d", "2", "-f", "S16_LE", "-r", "44100", tmp],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0 or os.path.getsize(tmp) == 0:
            return jsonify(message=f"Recording failed — check mic connection\n{r.stderr.strip()}")
        with wave.open(tmp) as wf:
            raw = wf.readframes(wf.getnframes())
        if not raw:
            return jsonify(message="Recording was empty")
        samples = struct.unpack(f"{len(raw) // 2}h", raw)
        peak = max(abs(s) for s in samples) / 32768.0
        if peak < 0.01:
            return jsonify(message=f"Mic silent — peak level {peak:.3f} (is the mic plugged in?)")
        return jsonify(message=f"Mic working — peak level {peak:.3f}")
    except subprocess.TimeoutExpired:
        return jsonify(message="arecord timed out")
    except OSError as e:
        return jsonify(message=str(e))
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

@app.route("/api/service/<action>", methods=["POST"])
def api_service(action):
    if action not in ("restart", "stop", "start"):
        return jsonify(ok=False, message="Invalid action"), 400
    try:
        subprocess.run(["sudo", "systemctl", action, "drawbox"],
                       check=True, timeout=30)
        return jsonify(ok=True,
                       message=f"DrawBox service {action}ed successfully.")
    except Exception as e:
        return jsonify(ok=False, message=str(e))

@app.route("/api/reboot", methods=["POST"])
def api_reboot():
    try:
        subprocess.Popen(["sudo", "reboot"])
        return jsonify(ok=True, message="Rebooting...")
    except Exception as e:
        return jsonify(ok=False, message=str(e))

# ── REALTIME AGENT (conversation mode) ───────────
# The ESP32 box's server-side half of a Grok Voice Agent session: it
# fetches an ephemeral token + the shared session config here, forwards
# the agent's draw_coloring_page calls to /api/agent/draw, and checks
# every transcript against /api/agent/intercept. The Pi daemon calls the
# same core functions directly — one behavior, two boxes.


def _mint_client_secret():
    import urllib.request

    req = urllib.request.Request(
        drawbox_core.XAI_CLIENT_SECRETS_URL, data=b"{}",
        headers={
            "Authorization": f"Bearer {drawbox_core.XAI_API_KEY}",
            "Content-Type": "application/json",
        })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


@app.route("/api/realtime/token", methods=["POST"])
def api_realtime_token():
    """Ephemeral xAI client secret + session config for a paired box.

    The real XAI key never leaves the Pi; the box connects to xAI with a
    short-lived secret minted here.
    """
    if not load_settings()["conversation_mode"]:
        return jsonify(ok=False, error="Conversation mode is off",
                       code="conversation_off"), 403
    drawbox_core.apply_api_keys()
    if not drawbox_core.XAI_API_KEY:
        return jsonify(ok=False, error="XAI_API_KEY not set",
                       code="no_key"), 503
    try:
        secret = _mint_client_secret()
    except Exception:
        log.exception("client secret mint failed")
        return jsonify(ok=False, error="Could not reach xAI",
                       code="mint_failed"), 502
    return jsonify(ok=True, token=secret.get("value"),
                   expires_at=secret.get("expires_at"),
                   url=drawbox_core.XAI_REALTIME_URL,
                   session=drawbox_core.realtime_session_config(),
                   max_session_s=drawbox_core.AGENT_SESSION_MAX_S)


@app.route("/api/agent/draw", methods=["POST"])
def api_agent_draw():
    """Execute the agent's draw_coloring_page tool call (gated, async)."""
    data = _request_dict()
    if data is None:
        return jsonify(ok=False, error="Invalid JSON body"), 400
    desc = data.get("description")
    if not isinstance(desc, str) or not desc.strip():
        return jsonify(ok=False,
                       message="Ask the child what they would like drawn first.")
    return jsonify(**drawbox_core.execute_draw_tool(desc))


@app.route("/api/agent/intercept", methods=["POST"])
def api_agent_intercept():
    """Run a session transcript through the deterministic interceptor.

    Returns ``action: null`` to let the conversation proceed. On a hit the
    admin side effects have already run server-side; ``ack_key`` (when
    synthesis worked) lets the box speak the exact line via /api/voice/clip.
    """
    data = _request_dict()
    if data is None:
        return jsonify(ok=False, error="Invalid JSON body"), 400
    transcript = data.get("transcript")
    if not isinstance(transcript, str) or not transcript.strip():
        return jsonify(action=None)
    hit = drawbox_core.intercept_transcript(transcript)
    if not hit:
        return jsonify(action=None)
    ack_key = None
    try:
        _, ack_key = _ensure_line_wav(hit["say"])
    except Exception:
        log.exception("intercept clip synthesis failed")
    return jsonify(action=hit["action"], say=hit["say"],
                   voice_key=hit["voice_key"], ack_key=ack_key)


# ── ANALYTICS ────────────────────────────────────

@app.route("/api/analytics")
def api_analytics():
    events = []
    if PRINT_LOG_FILE.exists():
        try:
            for line in PRINT_LOG_FILE.read_text().strip().splitlines():
                try:
                    events.append(json.loads(line))
                except (json.JSONDecodeError, TypeError):
                    continue
        except OSError:
            pass

    total = len(events)
    today = datetime.now().date().isoformat()
    prints_today = sum(1 for e in events if e.get("ts", "").startswith(today))

    # Model counts
    model_counts = dict(Counter(e.get("model", "unknown") for e in events))

    # Average generation time
    durations = [e["duration_s"] for e in events if "duration_s" in e]
    avg_duration = round(sum(durations) / len(durations), 1) if durations else 0

    # Top prompts
    prompt_counts = Counter()
    for e in events:
        p = e.get("prompt", "").strip().lower()
        if p:
            prompt_counts[p] += 1
    top_prompts = [{"prompt": p, "count": c} for p, c in prompt_counts.most_common(8)]

    # Recent (last 10, newest first)
    recent = list(reversed(events[-10:]))

    return jsonify(
        total_prints=total,
        prints_today=prints_today,
        model_counts=model_counts,
        avg_duration=avg_duration,
        top_prompts=top_prompts,
        recent=recent,
    )

# ── SOFTWARE UPDATE ──────────────────────────────

@app.route("/api/update/check")
def api_update_check():
    if not REPO_DIR.exists():
        return jsonify(error="Software updates not set up yet. Re-run deploy-web.sh from your Mac to set it up.",
                       has_update=False)
    try:
        subprocess.run(["git", "fetch", "origin"], cwd=str(REPO_DIR),
                       capture_output=True, timeout=30)
        local = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO_DIR),
                               capture_output=True, text=True).stdout.strip()
        remote = subprocess.run(["git", "rev-parse", "origin/main"],
                                cwd=str(REPO_DIR),
                                capture_output=True, text=True).stdout.strip()
        behind = subprocess.run(
            ["git", "rev-list", "--count", f"{local}..{remote}"],
            cwd=str(REPO_DIR), capture_output=True, text=True
        ).stdout.strip()
        behind = int(behind) if behind.isdigit() else 0
        msg = ""
        if behind > 0:
            msg = subprocess.run(
                ["git", "log", "-1", "--format=%s", "origin/main"],
                cwd=str(REPO_DIR), capture_output=True, text=True
            ).stdout.strip()
        return jsonify(
            has_update=behind > 0,
            local_sha=local,
            remote_sha=remote,
            commits_behind=behind,
            latest_message=msg,
        )
    except Exception as e:
        return jsonify(error=str(e), has_update=False)

def _files_to_deploy(repo_dir):
    """Every file the running Pi needs copied out of the repo clone.

    Python modules are globbed, not listed, so a new drawbox_*.py can never
    be left behind by a stale list. A stale list has bricked deployed boxes
    twice: templates/index.html (see _restore_missing_template) and
    drawbox_escpos.py (2026-08-22, import error killed both services).
    """
    modules = sorted(p.name for p in repo_dir.glob("drawbox*.py"))
    return modules + ["templates/index.html",
                      "drawbox-guide.html", "drawbox-simulator.html",
                      "check.sh"]


@app.route("/api/update/deploy", methods=["POST"])
def api_update_deploy():
    if not REPO_DIR.exists():
        return jsonify(ok=False, error="No repo found")
    try:
        pull = subprocess.run(["git", "pull", "origin", "main"],
                              cwd=str(REPO_DIR),
                              capture_output=True, text=True, timeout=60)
        output = pull.stdout + "\n" + pull.stderr

        home = Path.home()
        files_to_copy = _files_to_deploy(REPO_DIR)
        for fname in files_to_copy:
            src = REPO_DIR / fname
            if src.exists():
                dest = home / fname
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src), str(dest))
                output += f"\nCopied {fname} to ~/"

        # Clear voice cache so TTS lines are regenerated with new code/settings
        cache_dir = Path.home() / ".drawbox" / "voice_cache"
        if cache_dir.exists():
            shutil.rmtree(str(cache_dir))
            output += "\nCleared voice cache (will regenerate on restart)"

        # Schedule service restart (runs after response is sent)
        def restart_later():
            _time.sleep(1)
            subprocess.run(["sudo", "systemctl", "restart", "drawbox-web"],
                           capture_output=True)
            subprocess.run(["sudo", "systemctl", "restart", "drawbox"],
                           capture_output=True)

        threading.Thread(target=restart_later, daemon=True).start()

        return jsonify(ok=True, output=output.strip())
    except Exception as e:
        return jsonify(ok=False, error=str(e))

_TEMPLATE_FILE = Path(__file__).resolve().parent / "templates" / "index.html"


def _restore_missing_template():
    """Self-heal after an update performed by a pre-templates self-updater.

    Older deployed code copied drawbox_web.py from the repo but not
    templates/, leaving render_template with nothing to serve. If the repo
    clone has the template and the app dir doesn't, copy it into place.

    Delete once every deployed box has self-updated past the pre-templates
    updater (fleet transition began Aug 2026).
    """
    repo_template = REPO_DIR / "templates" / "index.html"
    if _TEMPLATE_FILE.exists() or not repo_template.exists():
        return
    _TEMPLATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(repo_template), str(_TEMPLATE_FILE))
    log.warning("restored missing dashboard template from %s", repo_template)


# ── INIT ─────────────────────────────────────────
DRAWBOX_DIR.mkdir(parents=True, exist_ok=True)
ensure_safety_mode_default()
_restore_missing_template()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if not drawbox_core.AI_GATEWAY_API_KEY:
        log.warning("AI_GATEWAY_API_KEY not set — generation will fail until you add it.")
    log.info("image_model=%s safety_filter=%s",
             IMAGE_MODEL, "on" if safety_mode_enabled() else "off")
    log.info("starting on http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
