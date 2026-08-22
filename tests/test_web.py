"""Flask route tests — anything that doesn't shell out to hardware-specific
binaries. We don't exercise /api/status, /api/logs, /api/diagnostics,
/api/test/* or /api/service/* here because they shell out to systemctl,
journalctl, nmcli, aplay, etc. that don't exist on macOS."""

import json
import re
from pathlib import Path

import pytest

import drawbox_core
import drawbox_web


# ── CORS policy ────────────────────────────────────

def test_cors_rejects_substring_lookalike():
    assert drawbox_web._allowed_origin("https://evil.drawbox.attacker.com") == ""
    assert drawbox_web._allowed_origin("https://x.drawbox.pages.dev.attacker.com") == ""


def test_cors_accepts_pages_dev_subdomains():
    o = "https://kitchen.drawbox.pages.dev"
    assert drawbox_web._allowed_origin(o) == o


def test_cors_rejects_javascript_scheme():
    assert drawbox_web._allowed_origin("javascript:alert(1)") == ""


def test_cors_extra_origins_can_be_extended(monkeypatch):
    new = drawbox_web._compile_extra_origins("*.example.com, hub.local")

    def allow_with(origin):
        from urllib.parse import urlparse
        host = urlparse(origin).hostname
        return any(p.match(host) for p in new)

    assert allow_with("https://hub.example.com")
    assert allow_with("https://x.y.example.com")
    assert not allow_with("https://example.com")       # bare apex, no subdomain
    assert allow_with("https://hub.local")
    assert not allow_with("https://hublocal")


# ── /api/settings ──────────────────────────────────

def test_settings_get_returns_defaults(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    body = r.get_json()
    assert body["coloring_prompt"] == drawbox_core.DEFAULT_COLORING_PROMPT
    assert body["record_seconds"] == 10


def test_settings_post_resolves_tts_voice(client):
    client.post("/api/settings", json={"tts_voice_id": "NOVA"})
    assert client.get("/api/settings").get_json()["tts_voice_id"] == "nova"
    client.post("/api/settings", json={"tts_voice_id": "not-a-voice"})
    assert client.get("/api/settings").get_json()["tts_voice_id"] == "alloy"


def test_settings_post_clamps_record_seconds(client):
    client.post("/api/settings", json={"record_seconds": 999})
    assert client.get("/api/settings").get_json()["record_seconds"] == 30
    client.post("/api/settings", json={"record_seconds": 1})
    assert client.get("/api/settings").get_json()["record_seconds"] == 3


def test_settings_rejects_invalid_model(client):
    client.post("/api/settings", json={"image_model": "not-real"})
    assert client.get("/api/settings").get_json().get("image_model") != "not-real"


def test_settings_accepts_valid_model(client):
    client.post("/api/settings", json={"image_model": "flux-schnell"})
    assert client.get("/api/settings").get_json()["image_model"] == "flux-schnell"


def test_settings_accepts_gateway_model(client):
    client.post("/api/settings", json={"image_model": "bfl/flux-pro-1.1"})
    assert client.get("/api/settings").get_json()["image_model"] == "bfl/flux-pro-1.1"


def test_settings_accepts_voice_provider_and_grok_voice(client):
    client.post("/api/settings",
                json={"voice_provider": "grok", "grok_voice_id": "ara"})
    body = client.get("/api/settings").get_json()
    assert body["voice_provider"] == "grok"
    assert body["grok_voice_id"] == "ara"


def test_settings_rejects_unknown_voice_provider(client):
    client.post("/api/settings", json={"voice_provider": "alexa"})
    assert client.get("/api/settings").get_json()["voice_provider"] == "gateway"


def test_settings_caps_prompt_length(client):
    huge = "x" * 10000
    client.post("/api/settings", json={"coloring_prompt": huge})
    assert len(client.get("/api/settings").get_json()["coloring_prompt"]) == 5000


def test_settings_rejects_garbage_types(client):
    r = client.post("/api/settings", json={"record_seconds": "abc"})
    assert r.status_code == 400


def test_settings_ignores_non_dict_body(client):
    r = client.post("/api/settings", json=[1, 2, 3])
    assert r.status_code == 400


def test_settings_get_includes_printer_defaults(client):
    body = client.get("/api/settings").get_json()
    assert body["printer_type"] == "cups"
    assert body["serial_port"] == "/dev/ttyUSB0"
    assert body["serial_baud"] == 9600


def test_settings_printer_fields_round_trip(client):
    client.post("/api/settings", json={
        "printer_type": "escpos_serial",
        "serial_port": "/dev/ttyACM7",
        "serial_baud": 115200,
    })
    body = client.get("/api/settings").get_json()
    assert body["printer_type"] == "escpos_serial"
    assert body["serial_port"] == "/dev/ttyACM7"
    assert body["serial_baud"] == 115200


def test_settings_rejects_bogus_printer_type(client):
    r = client.post("/api/settings", json={"printer_type": "bogus"})
    assert r.status_code == 400
    assert client.get("/api/settings").get_json()["printer_type"] == "cups"


def test_settings_rejects_unsupported_baud(client):
    client.post("/api/settings", json={"serial_baud": 19200})
    r = client.post("/api/settings", json={"serial_baud": 12345})
    assert r.status_code == 400
    assert client.get("/api/settings").get_json()["serial_baud"] == 19200


def test_settings_rejects_blank_or_non_string_serial_port(client):
    assert client.post("/api/settings", json={"serial_port": ""}).status_code == 400
    assert client.post("/api/settings", json={"serial_port": 123}).status_code == 400


# ── /api/scripts ───────────────────────────────────

def test_scripts_get_includes_defaults(client):
    body = client.get("/api/scripts").get_json()
    assert set(body["voice_lines"]) == set(drawbox_core.DEFAULT_VOICE_LINES)
    assert set(body["defaults"]["voice_lines"]) == set(drawbox_core.DEFAULT_VOICE_LINES)


def test_scripts_save_and_reset(client):
    client.post("/api/scripts", json={
        "voice_lines": {"ready": "Custom ready"},
        "jokes": ["Joke 1"],
    })
    body = client.get("/api/scripts").get_json()
    assert body["voice_lines"]["ready"] == "Custom ready"
    assert body["jokes"] == ["Joke 1"]

    client.post("/api/scripts", json={"reset": True})
    body = client.get("/api/scripts").get_json()
    assert body["voice_lines"]["ready"] == drawbox_core.DEFAULT_VOICE_LINES["ready"]["text"]


def test_scripts_post_rejects_non_dict(client):
    assert client.post("/api/scripts", json=["not", "a", "dict"]).status_code == 400


# ── /api/please-mode + /api/safety-mode ────────────

@pytest.mark.parametrize("endpoint, default", [
    ("/api/please-mode", False),
    ("/api/safety-mode", True),
])
def test_mode_toggle(client, endpoint, default):
    if default:
        # Default is ON (sentinel created at import time)
        drawbox_core.SAFETY_MODE_FILE.touch()
    assert client.get(endpoint).get_json()["enabled"] is default
    client.post(endpoint, json={"enabled": not default})
    assert client.get(endpoint).get_json()["enabled"] is (not default)
    client.post(endpoint, json={"enabled": default})
    assert client.get(endpoint).get_json()["enabled"] is default


@pytest.mark.parametrize("endpoint", ["/api/please-mode", "/api/safety-mode", "/api/poop-mode"])
def test_mode_toggle_rejects_non_boolean(client, endpoint):
    assert client.post(endpoint, json={"enabled": "true"}).status_code == 400
    assert client.post(endpoint, json={}).status_code == 400


def test_poop_mode_defaults_enabled(client):
    assert client.get("/api/poop-mode").get_json()["enabled"] is True


def test_poop_mode_toggle(client):
    assert client.get("/api/poop-mode").get_json()["enabled"] is True
    client.post("/api/poop-mode", json={"enabled": False})
    assert client.get("/api/poop-mode").get_json()["enabled"] is False
    client.post("/api/poop-mode", json={"enabled": True})
    assert client.get("/api/poop-mode").get_json()["enabled"] is True


# ── /api/keys ──────────────────────────────────────

def test_keys_get_returns_masked(client, monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "vck-abcdef123456")
    body = client.get("/api/keys").get_json()
    assert body["ai_gateway"].startswith("vck-")
    assert body["ai_gateway"].endswith("3456")
    assert "abcdef" not in body["ai_gateway"]
    assert "openai" not in body


def test_keys_post_writes_file(client):
    r = client.post("/api/keys", json={"ai_gateway": "vck-new"})
    assert r.status_code == 200
    on_disk = json.loads(drawbox_core.API_KEYS_FILE.read_text())
    assert on_disk == {"ai_gateway": "vck-new"}


def test_keys_post_skips_blank_values(client):
    drawbox_core.API_KEYS_FILE.write_text(json.dumps({"ai_gateway": "vck-old"}))
    client.post("/api/keys", json={"ai_gateway": "   "})
    on_disk = json.loads(drawbox_core.API_KEYS_FILE.read_text())
    assert on_disk == {"ai_gateway": "vck-old"}


def test_keys_post_ignores_non_string_values(client):
    drawbox_core.API_KEYS_FILE.write_text(json.dumps({"ai_gateway": "vck-old"}))
    client.post("/api/keys", json={"ai_gateway": 42})
    on_disk = json.loads(drawbox_core.API_KEYS_FILE.read_text())
    assert on_disk == {"ai_gateway": "vck-old"}


def test_keys_post_drops_legacy_provider_keys(client):
    drawbox_core.API_KEYS_FILE.write_text(json.dumps({
        "openai": "sk-old",
        "ai_gateway": "vck-keep",
    }))
    client.post("/api/keys", json={"ai_gateway": "vck-new"})
    on_disk = json.loads(drawbox_core.API_KEYS_FILE.read_text())
    assert on_disk == {"ai_gateway": "vck-new"}


# ── /api/generate input validation (mocked image generator) ──

@pytest.mark.parametrize("payload, error_substr", [
    ({"description": ""}, None),
    ({}, None),
    ({"description": "x" * 501}, "long"),
    ({"description": 123}, None),
])
def test_generate_rejects_invalid_descriptions(client, payload, error_substr):
    body = client.post("/api/generate", json=payload).get_json()
    assert body["ok"] is False
    if error_substr:
        assert error_substr in body["error"].lower()


def test_generate_rejects_blocked_when_safety_on(client):
    drawbox_core.SAFETY_MODE_FILE.touch()
    r = client.post("/api/generate", json={"description": "a gun and a knife"}).get_json()
    assert r["ok"] is False
    assert "blocked" in r["error"].lower()


def test_generate_allows_poop_when_poop_mode_on(client, monkeypatch, tmp_path):
    img = tmp_path / "out.png"
    img.write_bytes(b"fake-png")
    monkeypatch.setattr(drawbox_web, "generate_image", lambda *a, **k: str(img))
    monkeypatch.setattr(drawbox_web, "print_image", lambda *a, **k: None)

    r = client.post("/api/generate", json={"description": "a car with poop on the roof"}).get_json()

    assert r["ok"] is True
    assert r["image"]


def test_generate_rejects_poop_when_poop_mode_off(client):
    client.post("/api/poop-mode", json={"enabled": False})
    r = client.post("/api/generate", json={"description": "a car with poop on the roof"}).get_json()
    assert r["ok"] is False
    assert r["error"] == drawbox_core.DEFAULT_VOICE_LINES["poop_blocked"]["text"]


def test_generate_rejects_poop_family_when_poop_mode_off(client):
    client.post("/api/poop-mode", json={"enabled": False})
    for word in ("poops", "pooped", "pooping", "poopy"):
        r = client.post("/api/generate", json={"description": f"a {word} dinosaur"}).get_json()
        assert r["ok"] is False
        assert "poop" in r["error"]


def test_generate_uses_custom_poop_blocked_message(client):
    client.post("/api/poop-mode", json={"enabled": False})
    client.post("/api/scripts", json={
        "voice_lines": {"poop_blocked": "Custom poop nope."},
    })
    r = client.post("/api/generate", json={"description": "poopy car"}).get_json()
    assert r["ok"] is False
    assert r["error"] == "Custom poop nope."


def test_generate_checks_poop_before_safety(client):
    client.post("/api/poop-mode", json={"enabled": False})
    drawbox_core.SAFETY_MODE_FILE.touch()
    r = client.post("/api/generate", json={"description": "poop with a gun"}).get_json()
    assert r["ok"] is False
    assert r["error"] == drawbox_core.DEFAULT_VOICE_LINES["poop_blocked"]["text"]


def test_generate_rejects_missing_please_when_please_mode_on(client):
    drawbox_core.PLEASE_MODE_FILE.touch()
    r = client.post("/api/generate", json={"description": "a happy puppy"}).get_json()
    assert r["ok"] is False
    assert r["error"] == drawbox_core.DEFAULT_VOICE_LINES["say_please"]["text"]


def test_generate_uses_custom_say_please_message(client):
    drawbox_core.PLEASE_MODE_FILE.touch()
    client.post("/api/scripts", json={
        "voice_lines": {"say_please": "Magic word, tiny human."},
    })
    r = client.post("/api/generate", json={"description": "a happy puppy"}).get_json()
    assert r["ok"] is False
    assert r["error"] == "Magic word, tiny human."


def test_generate_allows_please_mode_request_with_please(client, monkeypatch, tmp_path):
    drawbox_core.PLEASE_MODE_FILE.touch()
    img = tmp_path / "out.png"
    img.write_bytes(b"fake-png")
    monkeypatch.setattr(drawbox_web, "generate_image", lambda *a, **k: str(img))
    monkeypatch.setattr(drawbox_web, "print_image", lambda *a, **k: None)

    r = client.post("/api/generate", json={"description": "a happy puppy please"}).get_json()

    assert r["ok"] is True
    assert r["image"]


def test_generate_returns_generic_error_on_failure(client, monkeypatch):
    drawbox_core.SAFETY_MODE_FILE.unlink(missing_ok=True)
    monkeypatch.setattr(drawbox_web, "generate_image",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("internal /home/secret")))
    r = client.post("/api/generate", json={"description": "a happy puppy"}).get_json()
    assert r["ok"] is False
    assert "/home/secret" not in r["error"]


# ── /api/wifi/connect input validation ─────────────

@pytest.mark.parametrize("payload, error_substr", [
    ({"ssid": ""}, "SSID"),
    ({"ssid": "ok-net", "password": "bad\x00pw"}, None),
    ({"ssid": "x" * 65}, None),
    ({"ssid": 123}, None),
])
def test_wifi_connect_rejects_invalid_payloads(client, payload, error_substr):
    body = client.post("/api/wifi/connect", json=payload).get_json()
    assert body["ok"] is False
    if error_substr:
        assert error_substr in body["error"]


def test_parse_saved_wifi_profiles_sorts_and_unescapes():
    out = (
        "Home\\:Main:11111111-1111-1111-1111-111111111111:wifi:20\n"
        "Ethernet:22222222-2222-2222-2222-222222222222:ethernet::0\n"
        "Grandma:33333333-3333-3333-3333-333333333333:802-11-wireless:100\n"
    )
    saved = drawbox_web._parse_saved_wifi_profiles(out)
    assert [n["ssid"] for n in saved] == ["Grandma", "Home:Main"]
    assert [n["priority"] for n in saved] == [100, 20]


def test_parse_saved_wifi_profiles_ignores_extra_summary_fields():
    uuid = "11111111-1111-1111-1111-111111111111"
    saved = drawbox_web._parse_saved_wifi_profiles(f"Home:{uuid}:wifi:10:unexpected\n")
    assert saved == [{
        "name": "Home",
        "uuid": uuid,
        "ssid": "Home",
        "priority": 10,
    }]


def test_wifi_saved_get_parses_nmcli(client, monkeypatch):
    calls = []
    uuid = "11111111-1111-1111-1111-111111111111"

    class Result:
        def __init__(self, stdout):
            self.returncode = 0
            self.stdout = stdout
            self.stderr = ""

    def fake_run(args, *a, **k):
        calls.append(args)
        if args == ["nmcli", "-t", "-f", "NAME,UUID,TYPE,AUTOCONNECT-PRIORITY", "con", "show"]:
            return Result(f"Home:{uuid}:wifi:10\n")
        if args == ["nmcli", "-g", "connection.uuid,802-11-wireless.ssid", "con", "show", uuid]:
            return Result(f"{uuid}\nHome\\:Main\n")
        raise AssertionError(f"unexpected nmcli call: {args}")

    monkeypatch.setattr(drawbox_web.subprocess, "run", fake_run)
    body = client.get("/api/wifi/saved").get_json()
    assert body["saved"] == [{
        "name": "Home",
        "uuid": uuid,
        "ssid": "Home:Main",
        "priority": 10,
    }]
    assert "802-11-wireless.ssid" not in calls[0][3]
    assert len(calls) == 2  # one listing + one batched SSID lookup


def test_wifi_saved_get_falls_back_to_names_when_ssid_lookup_fails(client, monkeypatch):
    uuid = "11111111-1111-1111-1111-111111111111"

    class Result:
        def __init__(self, stdout, returncode=0):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    def fake_run(args, *a, **k):
        if args[:3] == ["nmcli", "-t", "-f"]:
            return Result(f"Home:{uuid}:wifi:10\n")
        return Result("", returncode=1)

    monkeypatch.setattr(drawbox_web.subprocess, "run", fake_run)
    body = client.get("/api/wifi/saved").get_json()
    assert body["saved"][0]["ssid"] == "Home"


def test_restore_missing_template_copies_from_repo(tmp_path, monkeypatch):
    app_template = tmp_path / "app" / "templates" / "index.html"
    repo = tmp_path / "repo"
    (repo / "templates").mkdir(parents=True)
    (repo / "templates" / "index.html").write_text("<html>from repo</html>")
    monkeypatch.setattr(drawbox_web, "_TEMPLATE_FILE", app_template)
    monkeypatch.setattr(drawbox_web, "REPO_DIR", repo)

    drawbox_web._restore_missing_template()

    assert app_template.read_text() == "<html>from repo</html>"


def test_restore_missing_template_never_overwrites(tmp_path, monkeypatch):
    app_template = tmp_path / "templates" / "index.html"
    app_template.parent.mkdir(parents=True)
    app_template.write_text("<html>deployed</html>")
    repo = tmp_path / "repo"
    (repo / "templates").mkdir(parents=True)
    (repo / "templates" / "index.html").write_text("<html>older repo copy</html>")
    monkeypatch.setattr(drawbox_web, "_TEMPLATE_FILE", app_template)
    monkeypatch.setattr(drawbox_web, "REPO_DIR", repo)

    drawbox_web._restore_missing_template()

    assert app_template.read_text() == "<html>deployed</html>"


def test_wifi_saved_add_caps_priority_at_networkmanager_max(client, monkeypatch):
    calls = []
    monkeypatch.setattr(drawbox_web, "_saved_wifi_profiles", lambda: [
        {"name": "Home", "uuid": "11111111-1111-1111-1111-111111111111",
         "ssid": "Home", "priority": 995},
    ])
    monkeypatch.setattr(drawbox_web, "_run_sudo_nmcli", lambda args, timeout=30: calls.append(args))

    body = client.post("/api/wifi/saved", json={"ssid": "Cafe", "password": "x"}).get_json()

    assert body["ok"] is True
    args = calls[0]
    prio = args[args.index("connection.autoconnect-priority") + 1]
    assert prio == "999"


def test_wifi_saved_add_preconfigures_network(client, monkeypatch):
    calls = []
    monkeypatch.setattr(drawbox_web, "_saved_wifi_profiles", lambda: [])
    monkeypatch.setattr(drawbox_web, "_run_sudo_nmcli", lambda args, timeout=30: calls.append(args))

    body = client.post("/api/wifi/saved", json={
        "ssid": "Grandma",
        "name": "Grandma house",
        "password": "secret123",
    }).get_json()

    assert body["ok"] is True
    assert body["updated"] is False
    assert calls[0][:7] == ["con", "add", "type", "wifi", "ifname", "wlan0", "con-name"]
    assert "Grandma" in calls[0]
    assert "wifi-sec.psk" in calls[0]
    assert "connection.autoconnect-priority" in calls[0]


def test_wifi_saved_updates_existing_network(client, monkeypatch):
    calls = []
    uuid = "11111111-1111-1111-1111-111111111111"
    monkeypatch.setattr(drawbox_web, "_saved_wifi_profiles", lambda: [{
        "name": "Home", "uuid": uuid, "ssid": "Home", "priority": 5,
    }])
    monkeypatch.setattr(drawbox_web, "_run_sudo_nmcli", lambda args, timeout=30: calls.append(args))

    body = client.post("/api/wifi/saved", json={"ssid": "Home", "password": "newpass"}).get_json()

    assert body["ok"] is True
    assert body["updated"] is True
    assert calls[0][:3] == ["con", "modify", uuid]
    assert "wifi-sec.psk" in calls[0]


def test_wifi_saved_update_empty_password_clears_wpa_settings(client, monkeypatch):
    calls = []
    uuid = "11111111-1111-1111-1111-111111111111"
    monkeypatch.setattr(drawbox_web, "_saved_wifi_profiles", lambda: [{
        "name": "Cafe", "uuid": uuid, "ssid": "Cafe", "priority": 5,
    }])
    monkeypatch.setattr(drawbox_web, "_run_sudo_nmcli", lambda args, timeout=30: calls.append(args))

    body = client.post("/api/wifi/saved", json={"ssid": "Cafe", "password": ""}).get_json()

    assert body["ok"] is True
    assert body["updated"] is True
    assert calls[0][-4:] == ["wifi-sec.key-mgmt", "", "wifi-sec.psk", ""]


def test_wifi_saved_reorder_rejects_invalid_body(client):
    assert client.post("/api/wifi/saved/reorder", json={"uuids": []}).status_code == 400
    assert client.post("/api/wifi/saved/reorder", json={"uuids": ["not-a-uuid"]}).status_code == 400


def test_wifi_saved_reorder_sets_priorities(client, monkeypatch):
    calls = []
    uuids = [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    ]
    monkeypatch.setattr(drawbox_web, "_set_wifi_priority", lambda uuid, priority: calls.append((uuid, priority)))

    body = client.post("/api/wifi/saved/reorder", json={"uuids": uuids}).get_json()

    assert body["ok"] is True
    assert calls == [(uuids[0], 990), (uuids[1], 980)]


def test_wifi_priority_stays_inside_networkmanager_range():
    assert drawbox_web._clamp_wifi_priority(1000) == 999
    assert drawbox_web._clamp_wifi_priority(-2000) == -999
    assert drawbox_web._clamp_wifi_priority(50) == 50
    assert drawbox_web._next_wifi_priority([{"priority": 995}]) == 999
    assert drawbox_web._next_wifi_priority([]) == 10


def test_set_wifi_priority_clamps_before_nmcli(monkeypatch):
    calls = []
    uuid = "11111111-1111-1111-1111-111111111111"
    monkeypatch.setattr(drawbox_web, "_run_sudo_nmcli", lambda args, timeout=30: calls.append(args))

    drawbox_web._set_wifi_priority(uuid, 1000)

    assert calls[0][-2:] == ["connection.autoconnect-priority", "999"]


def test_wifi_saved_delete_rejects_invalid_uuid(client):
    assert client.delete("/api/wifi/saved/not-a-uuid").status_code == 400


def test_wifi_saved_delete_calls_nmcli(client, monkeypatch):
    calls = []
    uuid = "11111111-1111-1111-1111-111111111111"
    monkeypatch.setattr(drawbox_web, "_run_sudo_nmcli", lambda args, timeout=30: calls.append(args))

    body = client.delete(f"/api/wifi/saved/{uuid}").get_json()

    assert body["ok"] is True
    assert calls == [["con", "delete", uuid]]


# ── /api/diagnostics allowlist ─────────────────────

def test_diagnostics_rejects_unknown_command(client):
    r = client.post("/api/diagnostics", json={"command": "rm -rf /"})
    assert r.status_code == 400


def test_diagnostics_rejects_non_string(client):
    r = client.post("/api/diagnostics", json={"command": ["ls"]})
    assert r.status_code == 400


def test_diagnostics_allowlist_has_no_shell_metachars():
    """Defense in depth: every allowlisted argv must be a plain token list,
    not a shell command. This guards against future edits sneaking in
    something like `["sh", "-c", ...]`."""
    for key, cmd in drawbox_web.DIAGNOSTIC_COMMANDS.items():
        assert isinstance(cmd, list)
        for arg in cmd:
            assert isinstance(arg, str)
            # Reject shell metacharacters in argv entries
            for ch in ";&|`$<>":
                assert ch not in arg, f"{key} contains shell metachar {ch!r}"


# ── /api/service/<action> ──────────────────────────

def test_service_rejects_unknown_action(client):
    r = client.post("/api/service/restart-evil-stuff")
    assert r.status_code == 400


# ── /api/analytics ─────────────────────────────────

def test_analytics_empty(client):
    body = client.get("/api/analytics").get_json()
    assert body["total_prints"] == 0
    assert body["recent"] == []


def test_analytics_aggregates(client):
    from datetime import datetime
    entries = [
        {"ts": datetime.now().isoformat(timespec="seconds"),
         "prompt": "cat", "model": "nano-banana", "duration_s": 2.5, "source": "button"},
        {"ts": datetime.now().isoformat(timespec="seconds"),
         "prompt": "dog", "model": "nano-banana", "duration_s": 3.5, "source": "web"},
        {"ts": "2020-01-01T00:00:00",
         "prompt": "cat", "model": "flux-schnell", "duration_s": 1.0, "source": "button"},
    ]
    drawbox_core.PRINT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    drawbox_core.PRINT_LOG_FILE.write_text(
        "\n".join(json.dumps(e) for e in entries))
    body = client.get("/api/analytics").get_json()
    assert body["total_prints"] == 3
    assert body["prints_today"] == 2
    assert body["model_counts"] == {"nano-banana": 2, "flux-schnell": 1}
    top = {p["prompt"]: p["count"] for p in body["top_prompts"]}
    assert top == {"cat": 2, "dog": 1}


def test_analytics_skips_garbage_lines(client):
    drawbox_core.PRINT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    drawbox_core.PRINT_LOG_FILE.write_text(
        '{"ts": "2024-01-01", "prompt": "cat", "model": "x", "duration_s": 1}\n'
        'not-json\n'
    )
    body = client.get("/api/analytics").get_json()
    assert body["total_prints"] == 1


def test_simulator_defines_poop_mode_dependencies():
    html = Path("drawbox-simulator.html").read_text()
    for snippet in (
        'id="poopmode"',
        "function containsPoop",
        "function parseAdminPoopCommand",
        "poop_blocked:",
        "poop_mode_enabled:",
        "poop_mode_disabled:",
    ):
        assert snippet in html


def test_dashboard_uses_in_page_confirm_instead_of_native_dialogs(client):
    """The Mac app's WKWebView treats window.confirm() as a silent no-op.

    Every destructive dashboard action has to go through askConfirm(), or
    the button click returns immediately and looks like it did nothing.
    """
    html = client.get("/").get_data(as_text=True)
    assert "function askConfirm(" in html
    assert 'id="confirmOverlay"' in html
    assert "await askConfirm(" in html
    assert not re.search(r"""(?<!ask)confirm\s*\(\s*['"`]""", html)


def test_dashboard_keys_match_the_registry(client):
    """One gateway key for images/STT, plus the two optional voice keys.

    The direct image-provider keys retired by the gateway migration must not
    resurface.
    """
    html = client.get("/").get_data(as_text=True)
    assert 'id="keyGateway"' in html
    assert "AI Gateway API Key" in html
    assert 'id="keyElevenlabs"' in html
    assert 'id="keyXai"' in html
    assert 'id="keyOpenai"' not in html
    assert 'id="keyReplicate"' not in html
    assert 'id="keyGemini"' not in html
    assert "TTS_VOICES" in html


def test_dashboard_setting_toggles_use_the_whole_row(client):
    html = client.get("/").get_data(as_text=True)
    assert "function toggleFlag(" in html
    for name in ("togglePlease", "toggleSafety", "togglePoop"):
        assert re.search(
            rf'<button type="button" class="setting-row"[^>]*onclick="{name}\(\)"',
            html,
        )
    assert 'id="pleaseToggle" onclick=' not in html


def test_dashboard_exposes_gateway_and_voice_provider_controls(client):
    html = client.get("/").get_data(as_text=True)
    assert 'id="cfgVoiceProvider"' in html
    assert 'id="keyXai"' in html
    assert 'id="keyElevenlabs"' in html
    assert 'value="spacexai/grok-imagine-image"' in html
