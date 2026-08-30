"""Device pairing: voice command, code redemption, and the API token guard."""

import os
import types

import drawbox_core


def _wrong_code(code):
    return "000000" if code != "000000" else "000001"


# ── voice command ───────────────────────────────────

def test_is_pairing_command_matches_authorize_variants():
    assert drawbox_core.is_pairing_command("Authorize!")
    assert drawbox_core.is_pairing_command("please authorise this laptop")
    # Whisper frequently transcribes the spoken command in the past tense.
    assert drawbox_core.is_pairing_command("Authorized.")
    assert drawbox_core.is_pairing_command("Authorised")
    assert not drawbox_core.is_pairing_command("draw a fire truck")
    assert not drawbox_core.is_pairing_command("")
    # Accepted trade-off: any prompt containing "authorized" opens a pairing
    # window instead of drawing. Physical button + 2-minute window + code only
    # audible in the room keeps this safe; the kid just retries the drawing.
    assert drawbox_core.is_pairing_command("draw an authorized personnel sign")


# ── pairing window ──────────────────────────────────

def test_pairing_code_redeems_once(drawbox_dir):
    code = drawbox_core.open_pairing_window()
    assert len(code) == 6 and code.isdigit()

    token = drawbox_core.redeem_pairing_code(code, "Dad's MacBook")
    assert token
    assert drawbox_core.is_valid_device_token(token)
    # success closes the window
    assert drawbox_core.redeem_pairing_code(code, "again") is None


def test_wrong_guess_keeps_window_open_for_retry(drawbox_dir):
    code = drawbox_core.open_pairing_window()
    assert drawbox_core.redeem_pairing_code(_wrong_code(code), "typo") is None
    assert drawbox_core.redeem_pairing_code(code, "second try")


def test_pairing_window_burns_after_max_attempts(drawbox_dir):
    code = drawbox_core.open_pairing_window()
    for _ in range(drawbox_core.PAIRING_MAX_ATTEMPTS):
        assert drawbox_core.redeem_pairing_code(_wrong_code(code), "x") is None
    # even the right code is dead now
    assert drawbox_core.redeem_pairing_code(code, "x") is None


def test_pairing_window_expires(drawbox_dir, monkeypatch):
    code = drawbox_core.open_pairing_window()
    real_time = drawbox_core.time.time
    monkeypatch.setattr(drawbox_core.time, "time",
                        lambda: real_time() + drawbox_core.PAIRING_WINDOW_SEC + 1)
    assert drawbox_core.redeem_pairing_code(code, "x") is None


def test_print_pairing_code_renders_and_prints_a_card(monkeypatch, tmp_path):
    printed = []

    def fake_print(path):
        from PIL import Image
        with Image.open(path) as img:
            # getextrema()[0] == 0 proves black ink landed on the canvas;
            # a blank white render would report a minimum of 255.
            printed.append((img.size, img.getextrema()[0]))

    monkeypatch.setattr(drawbox_core, "print_image", fake_print)

    drawbox_core.print_pairing_code("012345")

    assert printed == [((drawbox_core.CANVAS_W, drawbox_core.CANVAS_H), 0)]


def test_revoked_device_token_stops_working(drawbox_dir):
    code = drawbox_core.open_pairing_window()
    token = drawbox_core.redeem_pairing_code(code, "old laptop")
    device = drawbox_core.list_paired_devices()[0]

    assert drawbox_core.revoke_paired_device(device["id"]) is True
    assert not drawbox_core.is_valid_device_token(token)
    assert drawbox_core.revoke_paired_device(device["id"]) is False


# ── API token guard ─────────────────────────────────

def test_api_requires_pairing_token(client):
    import drawbox_web
    anon = drawbox_web.app.test_client()

    r = anon.get("/api/settings")
    assert r.status_code == 401
    assert r.get_json()["error"] == "Not paired"

    # the dashboard page and CORS preflights stay public
    assert anon.get("/").status_code == 200
    # Flask answers OPTIONS on registered routes itself (200); the catch-all
    # preflight route returns 204. Either way: no 401.
    assert anon.open("/api/settings", method="OPTIONS").status_code in (200, 204)


def test_pair_endpoint_issues_working_token(client, drawbox_dir):
    import drawbox_web
    anon = drawbox_web.app.test_client()
    code = drawbox_core.open_pairing_window()

    body = anon.post("/api/pair", json={"code": code, "name": "Mac app"}).get_json()
    assert body["ok"] is True

    r = anon.get("/api/settings",
                 headers={"Authorization": f"Bearer {body['token']}"})
    assert r.status_code == 200


def test_pair_endpoint_rejects_wrong_code(client, drawbox_dir):
    import drawbox_web
    anon = drawbox_web.app.test_client()
    code = drawbox_core.open_pairing_window()

    r = anon.post("/api/pair", json={"code": _wrong_code(code), "name": "x"})
    assert r.status_code == 403
    r = anon.post("/api/pair", json={"code": 123456})
    assert r.status_code == 400


def test_pair_accepts_typed_code_without_leading_zero(client, drawbox_dir, monkeypatch):
    import drawbox_web
    monkeypatch.setattr(drawbox_core.secrets, "randbelow", lambda n: 12345)
    code = drawbox_core.open_pairing_window()
    assert code == "012345"  # spoken with the leading zero

    anon = drawbox_web.app.test_client()
    body = anon.post("/api/pair", json={"code": "12345", "name": "hasty typist"}).get_json()
    assert body["ok"] is True


def test_paired_devices_list_and_revoke_via_api(client):
    devices = client.get("/api/pair/devices").get_json()["devices"]
    assert [d["name"] for d in devices] == ["tests"]
    assert "token_hash" not in devices[0]

    r = client.delete(f"/api/pair/devices/{devices[0]['id']}")
    assert r.get_json()["ok"] is True
    # the client just revoked itself
    assert client.get("/api/pair/devices").status_code == 401


def test_query_token_works_only_for_the_log_endpoints(client, drawbox_dir, monkeypatch, fake_journal):
    import drawbox_web
    anon = drawbox_web.app.test_client()
    code = drawbox_core.open_pairing_window()
    token = drawbox_core.redeem_pairing_code(code, "log viewer")

    # One line, then EOF, so the SSE stream terminates and the test client
    # can consume the whole response.
    os.write(fake_journal, b"hello from journalctl\n")
    os.close(fake_journal)
    monkeypatch.setattr(
        drawbox_web.subprocess, "run",
        lambda *a, **k: types.SimpleNamespace(stdout="log text", stderr="", returncode=0))

    # EventSource and <a download> can't send headers, so the log endpoints
    # accept ?token= ...
    r = anon.get(f"/api/logs?token={token}")
    assert r.status_code == 200
    assert "hello from journalctl" in r.get_data(as_text=True)
    assert anon.get(f"/api/logs/download?token={token}").status_code == 200

    # ... but nothing else does.
    assert anon.get(f"/api/analytics?token={token}").status_code == 401
