#!/usr/bin/env python3
"""DrawBox web dashboard — Flask control panel for the Pi."""

import base64
import io
import json
import logging
import os
import re
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
    API_KEYS_FILE, DRAWBOX_DIR, IMAGE_MODEL, PLEASE_MODE_FILE, PRINT_LOG_FILE,
    SAFETY_MODE_FILE, SUPPORTED_MODELS, _load_api_keys, _write_secure_json,
    apply_api_keys, contains_poop, default_scripts, ensure_safety_mode_default,
    generate_image, is_safe, is_valid_device_token, list_paired_devices,
    load_scripts, load_settings, log_print_event, mask_key,
    please_mode_enabled, poop_blocked_message, poop_mode_enabled, print_image,
    redeem_pairing_code, revoke_paired_device, safety_mode_enabled,
    save_scripts, save_settings, set_poop_mode_enabled,
)

log = logging.getLogger("drawbox.web")

# ── CONFIG ───────────────────────────────────────
REPO_DIR = Path.home() / "drawbox-repo"
GUIDE_PATH = Path.home() / "drawbox-guide.html"
SIMULATOR_PATH = Path.home() / "drawbox-simulator.html"

# ── IMAGE GENERATION STATE ────────────────────────
_gen_lock = threading.Lock()

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


@app.route("/api/pair", methods=["POST"])
def api_pair():
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
    return jsonify(ok=True)


# ── ROUTES ───────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

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

@app.route("/api/status")
def api_status():
    # Service status
    svc = subprocess.run(["systemctl", "is-active", "drawbox"],
                         capture_output=True, text=True)
    running = svc.stdout.strip() == "active"

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

    return jsonify(service_running=running, temperature=temp,
                   uptime=up, rpi_connect=rpi)

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
    if not poop_mode_enabled() and contains_poop(desc):
        return jsonify(ok=False, error=poop_blocked_message())
    if safety_mode_enabled() and not is_safe(desc):
        return jsonify(
            ok=False,
            error="That description contains blocked words. "
                  "Try something fun like an animal or a rainbow!",
        )

    if not _gen_lock.acquire(blocking=False):
        return jsonify(ok=False, error="Already generating — please wait.")
    try:
        settings = load_settings()
        model = settings.get("image_model", IMAGE_MODEL)
        t0 = _time.time()
        path = generate_image(desc, model=model)
        duration = _time.time() - t0
        with open(path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode()
        print_image(path)
        log_print_event(desc, model, duration, source="web")
        return jsonify(ok=True, image=image_b64)
    except Exception:
        log.exception("generate failed")
        return jsonify(ok=False, error="Generation failed; check logs.")
    finally:
        _gen_lock.release()

@app.route("/api/logs")
def api_logs():
    def stream():
        # Cap the backfill at 1000 lines so the live view stays responsive on
        # a chatty Pi. Full 24h is available via /api/logs/download.
        proc = subprocess.Popen(
            ["journalctl", "-u", "drawbox", "-u", "drawbox-web",
             "--since", "24 hours ago", "-n", "1000", "-f", "--no-pager"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            for line in proc.stdout:
                yield f"data: {line.rstrip()}\n\n"
        finally:
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

def _clamp_float(v, lo, hi):
    return max(lo, min(hi, float(v)))

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
        if isinstance(data.get("tts_voice_id"), str) and data["tts_voice_id"].strip():
            settings["tts_voice_id"] = data["tts_voice_id"].strip()[:64]
        if "tts_stability" in data:
            settings["tts_stability"] = _clamp_float(data["tts_stability"], 0.0, 1.0)
        if "tts_style" in data:
            settings["tts_style"] = _clamp_float(data["tts_style"], 0.0, 1.0)
        if "record_seconds" in data:
            settings["record_seconds"] = max(3, min(30, int(data["record_seconds"])))
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
    for k in drawbox_core.API_KEY_NAMES:
        val = data.get(k)
        if isinstance(val, str) and val.strip():
            existing[k] = val.strip()
    _write_secure_json(API_KEYS_FILE, existing)
    apply_api_keys()
    return jsonify(ok=True)


_WIFI_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


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
    return (max((p["priority"] for p in profiles), default=0) + 10)


def _run_sudo_nmcli(args, timeout=30):
    r = subprocess.run(["sudo", "nmcli", *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip() or "nmcli failed")
    return r


def _set_wifi_priority(uuid, priority):
    if not _WIFI_UUID_RE.match(uuid or ""):
        raise ValueError("Invalid network id")
    _run_sudo_nmcli(["con", "modify", uuid, "connection.autoconnect", "yes",
                     "connection.autoconnect-priority", str(int(priority))])


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
        start = 1000
        for idx, uuid in enumerate(uuids):
            _set_wifi_priority(uuid, start - (idx * 10))
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
        files_to_copy = ["drawbox_core.py", "drawbox.py", "drawbox_web.py",
                         "templates/index.html",
                         "drawbox-guide.html", "drawbox-simulator.html",
                         "check.sh"]
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

# ── INIT ─────────────────────────────────────────
DRAWBOX_DIR.mkdir(parents=True, exist_ok=True)
ensure_safety_mode_default()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if not drawbox_core.OPENAI_API_KEY:
        log.warning("OPENAI_API_KEY not set — voice features won't work.")
    log.info("image_model=%s safety_filter=%s",
             IMAGE_MODEL, "on" if safety_mode_enabled() else "off")
    log.info("starting on http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
