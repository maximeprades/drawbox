"""ESP32 heartbeat + /api/devices listing."""

import drawbox_core
import drawbox_web


def test_heartbeat_401_without_token(drawbox_dir):
    anon = drawbox_web.app.test_client()
    r = anon.post("/api/device/heartbeat", json={"version": "1.0"})
    assert r.status_code == 401


def test_heartbeat_returns_current_settings(client):
    client.post("/api/settings", json={
        "esp32_volume": 33,
        "esp32_brightness": 44,
        "record_seconds": 12,
    })
    r = client.post("/api/device/heartbeat", json={
        "version": "1.2.3",
        "uptime_s": 90,
        "rssi": -61,
        "heap": 120000,
        "psram": 2000000,
        "cache_lines": 7,
        "cache_ready": True,
    })
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["volume"] == 33
    assert body["brightness"] == 44
    assert body["record_seconds"] == 12


def test_devices_lists_online_and_never_seen(client, drawbox_dir):
    client.post("/api/device/heartbeat", json={
        "version": "1.0.0",
        "uptime_s": 10,
        "rssi": -70,
        "heap": 80000,
        "psram": 1000000,
        "cache_lines": 3,
        "cache_ready": True,
    })
    code = drawbox_core.open_pairing_window()
    token2 = drawbox_core.redeem_pairing_code(code, "silent box")
    assert token2

    devices = client.get("/api/devices").get_json()
    assert isinstance(devices, list)
    by_name = {d["name"]: d for d in devices}
    live = by_name["tests"]
    assert live["online"] is True
    assert isinstance(live["last_seen_s"], int) and live["last_seen_s"] >= 0
    assert live["status"]["version"] == "1.0.0"
    assert live["status"]["uptime_s"] == 10
    assert live["status"]["rssi"] == -70
    assert live["status"]["heap"] == 80000
    assert live["status"]["psram"] == 1000000
    assert live["status"]["cache_lines"] == 3
    assert live["status"]["cache_ready"] is True
    assert "ts" not in live["status"]

    quiet = by_name["silent box"]
    assert quiet["online"] is False
    assert quiet["last_seen_s"] is None
    assert quiet["status"] is None


def test_heartbeat_ip_ignores_spoofed_forwarded_for(client):
    client.post("/api/device/heartbeat", json={"version": "1"},
                headers={"X-Forwarded-For": "10.0.0.99"})
    devices = client.get("/api/devices").get_json()
    assert devices[0]["status"]["ip"] != "10.0.0.99"


def test_device_for_token_returns_entry_or_none(drawbox_dir):
    code = drawbox_core.open_pairing_window()
    token = drawbox_core.redeem_pairing_code(code, "lookup")
    entry = drawbox_core.device_for_token(token)
    assert entry["name"] == "lookup"
    assert entry["id"]
    assert entry["token_hash"]
    assert drawbox_core.device_for_token("") is None
    assert drawbox_core.device_for_token("garbage") is None


def test_devices_survives_online_device_listed_before_offline(client, drawbox_dir):
    # Regression: a per-device local shadowed the loaded status map, so an
    # online device followed by a never-seen one crashed the endpoint.
    client.post("/api/device/heartbeat", json={"version": "t", "rssi": -60})
    code = drawbox_core.open_pairing_window()
    assert drawbox_core.redeem_pairing_code(code, "second box")
    body = client.get("/api/devices").get_json()
    assert [d["online"] for d in body] == [True, False]
    assert body[1]["status"] is None


def test_devices_survives_online_device_listed_before_offline(client, drawbox_dir):
    # Regression: a per-device local shadowed the loaded status map, so an
    # online device followed by a never-seen one crashed the endpoint.
    client.post("/api/device/heartbeat", json={"version": "t", "rssi": -60})
    code = drawbox_core.open_pairing_window()
    assert drawbox_core.redeem_pairing_code(code, "second box")
    body = client.get("/api/devices").get_json()
    assert [d["online"] for d in body] == [True, False]
    assert body[1]["status"] is None


def test_devices_survives_online_device_listed_before_offline(client, drawbox_dir):
    # Regression: a per-device local shadowed the loaded status map, so an
    # online device followed by a never-seen one crashed the endpoint.
    client.post("/api/device/heartbeat", json={"version": "t", "rssi": -60})
    code = drawbox_core.open_pairing_window()
    assert drawbox_core.redeem_pairing_code(code, "second box")
    body = client.get("/api/devices").get_json()
    assert [d["online"] for d in body] == [True, False]
    assert body[1]["status"] is None


def test_devices_marks_the_calling_device_as_self(client, drawbox_dir):
    code = drawbox_core.open_pairing_window()
    assert drawbox_core.redeem_pairing_code(code, "other box")
    body = client.get("/api/devices").get_json()
    assert [d["self"] for d in body] == [True, False]


def test_revoke_drops_heartbeat_record(client, drawbox_dir):
    client.post("/api/device/heartbeat", json={"version": "t"})
    import drawbox_web
    devices = client.get("/api/devices").get_json()
    dev_id = devices[0]["id"]
    assert dev_id in drawbox_web._load_device_status()
    client.delete(f"/api/pair/devices/{dev_id}")
    assert dev_id not in drawbox_web._load_device_status()
