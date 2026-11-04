"""Account security: two-step sign-in with an authenticator app,
recovery codes, step-up before sensitive actions, Recent sign-ins, and "sign out everywhere else"."""

import base64
import io
import json
import re
import sys
import time
import zipfile
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet
from starlette.testclient import TestClient

from thread import auth as auth_mod
from thread import twofactor
from thread.accounts import SESSION_COOKIE, STEP_UP_SECONDS
from thread.sealed import Sealer

from test_auth import REDIRECT, pkce
from test_online import PASSWORD, PUBLIC, SAM, H, online, sign_in  # noqa: F401


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    async def instant(_):
        return None
    monkeypatch.setattr(auth_mod.anyio, "sleep", instant)


@pytest.fixture
def secure(online):
    """The online server with THREAD_SECRET_KEY set (it refuses to start without one)."""
    online.store.sealer = Sealer(Fernet.generate_key().decode())
    return online


def uid(client):
    return client.store.user_id(SAM)


def code_now(secret, offset=0):
    return twofactor.code_at(secret, twofactor.current_step() + offset)


def turn_on(client):
    """Sign in, turn on two-step sign-in, and return (secret, recovery codes)."""
    sign_in(client)
    start = client.post("/api/2fa/start", json={}, headers=H)
    assert start.status_code == 200, start.text
    secret = start.json()["key"].replace(" ", "")
    done = client.post("/api/2fa/confirm", json={"code": code_now(secret)}, headers=H)
    assert done.status_code == 200, done.text
    return secret, done.json()["codes"]


def make_stale(client):
    for v in client.store.data["sessions"].values():
        v["auth_at"] = time.time() - STEP_UP_SECONDS - 5
    client.store.save()


def password_step(client):
    """Sign in with the password only; returns the code screen and its ticket."""
    page = sign_in(client)
    assert page.status_code == 200 and "Enter your code" in page.text
    return page, re.search(r'name="ticket" value="([^"]+)"', page.text).group(1)


def use_a_new_step(client):
    """Rewind the account's last-used step so the same code can be reused in a test."""
    client.store.data["users"][uid(client)]["totp"]["last_step"] = -1
    client.store.save()


# ---------- the code itself (RFC 6238) ----------

def test_codes_match_the_rfc_6238_test_vectors():
    secret = base64.b32encode(b"12345678901234567890").decode()
    # RFC 6238 Appendix B (SHA-1), last six digits of the eight-digit values.
    for t, code in ((59, "287082"), (1111111109, "081804"), (1234567890, "005924"), (2000000000, "279037")):
        assert twofactor.code_at(secret, t // 30) == code


def test_one_step_of_drift_either_side_and_never_the_same_step_twice():
    secret, now = twofactor.new_secret(), 1_800_000_000
    step = now // 30
    for offset in (-1, 0, 1):
        assert twofactor.match_step(secret, twofactor.code_at(secret, step + offset), now=now) == step + offset
    assert twofactor.match_step(secret, twofactor.code_at(secret, step - 2), now=now) is None
    assert twofactor.match_step(secret, twofactor.code_at(secret, step), last_step=step, now=now) is None
    assert twofactor.match_step(secret, "12345", now=now) is None


def test_qr_code_is_made_on_the_server_not_by_an_outside_service():
    uri = twofactor.otpauth_uri("sam@andrew.cmu.edu", "JBSWY3DPEHPK3PXP")
    assert uri.startswith("otpauth://totp/Thread:sam%40andrew.cmu.edu?secret=JBSWY3DPEHPK3PXP&issuer=Thread")
    qr = twofactor.qr_data_uri(uri)
    assert qr.startswith("data:image/svg+xml;base64,")
    svg = base64.b64decode(qr.split(",", 1)[1]).decode()
    assert "<svg" in svg and "http" not in svg.replace("http://www.w3.org/2000/svg", "")


def test_recovery_codes_are_random_and_hashed_with_a_salt():
    codes = twofactor.new_recovery_codes()
    assert len(codes) == 10 == len(set(codes)) and all(re.fullmatch(r"[a-z2-9]{5}-[a-z2-9]{5}", c) for c in codes)
    hashes = [twofactor.hash_recovery(c) for c in codes]
    assert twofactor.hash_recovery(codes[0]) != hashes[0]               # salted
    assert twofactor.recovery_index(codes[3].upper().replace("-", " "), hashes) == 3   # forgiving to type
    assert twofactor.recovery_index("aaaaa-aaaaa", hashes) is None


# ---------- turning it on ----------

def test_turning_on_two_step_stores_the_secret_encrypted_and_recovery_codes_only_as_hashes(secure):
    secret, codes = turn_on(secure)
    raw = (secure.store.path).read_text()
    assert secret not in raw and secret.lower() not in raw
    assert not any(c in raw or c.replace("-", "") in raw for c in codes)
    t = secure.store.data["users"][uid(secure)]["totp"]
    assert t["secret"].startswith("thread-sealed:v1:")
    assert all(h.startswith("scrypt12$") for h in t["recovery"])
    assert secure.get("/api/account").json()["twofa"] == {"on": True, "since": t["since"], "recovery_left": 10}


def test_a_wrong_code_doesn_t_turn_it_on(secure):
    sign_in(secure)
    secret = secure.post("/api/2fa/start", json={}, headers=H).json()["key"].replace(" ", "")
    wrong = str((int(code_now(secret)) + 1) % 1_000_000).zfill(6)
    r = secure.post("/api/2fa/confirm", json={"code": wrong}, headers=H)
    assert r.status_code == 400 and not secure.store.has_2fa(uid(secure))


def test_setup_must_be_finished_within_15_minutes(secure):
    sign_in(secure)
    secret = secure.post("/api/2fa/start", json={}, headers=H).json()["key"].replace(" ", "")
    secure.store.data["users"][uid(secure)]["totp_pending"]["created"] -= 16 * 60
    secure.store.save()
    r = secure.post("/api/2fa/confirm", json={"code": code_now(secret)}, headers=H)
    assert r.status_code == 400 and "too long" in r.json()["error"]


def test_two_step_can_t_be_turned_on_without_the_server_key(online):
    sign_in(online)
    r = online.post("/api/2fa/start", json={}, headers=H)
    assert r.status_code == 400 and "THREAD_SECRET_KEY" in r.json()["error"]


def test_turning_it_on_signs_out_other_browsers_but_keeps_this_one(secure):
    other = TestClient(secure.app_, base_url=PUBLIC)
    sign_in(other)
    assert other.get("/api/graph").status_code == 200
    turn_on(secure)
    assert other.get("/api/graph").status_code == 401
    assert secure.get("/api/graph").status_code == 200


def test_setup_answers_are_never_cached(secure):
    sign_in(secure)
    assert secure.post("/api/2fa/start", json={}, headers=H).headers["cache-control"] == "no-store"
    assert secure.get("/api/account").headers["cache-control"] == "no-store"


# ---------- signing in with it ----------

def test_sign_in_asks_for_the_code_after_the_password(secure):
    secret, _ = turn_on(secure)
    secure.cookies.clear()
    page, ticket = password_step(secure)
    assert SESSION_COOKIE not in page.headers.get("set-cookie", "")
    assert 'autocomplete="one-time-code"' in page.text and "2 of 2" in page.text
    # Enter in the code field submits Continue (the form's first submit button).
    form = page.text.split('<form method="post" action="/signin" class="q"')[1].split("</form>")[0]
    assert re.findall(r'<button[^>]*type="submit"[^>]*>([^<]*)', form)[0] == "Continue"
    use_a_new_step(secure)
    done = secure.post("/signin", data={"ticket": ticket, "code": code_now(secret)}, follow_redirects=False)
    assert done.status_code == 303 and SESSION_COOKIE in done.headers["set-cookie"]
    assert secure.get("/api/graph").status_code == 200


def test_a_code_works_only_once(secure):
    secret, _ = turn_on(secure)
    use_a_new_step(secure)
    code = code_now(secret)
    secure.cookies.clear()
    _, ticket = password_step(secure)
    assert secure.post("/signin", data={"ticket": ticket, "code": code}, follow_redirects=False).status_code == 303
    secure.cookies.clear()
    _, ticket = password_step(secure)
    again = secure.post("/signin", data={"ticket": ticket, "code": code}, follow_redirects=False)
    assert again.status_code == 401 and "Codes change every 30 seconds" in again.text


def test_the_password_alone_or_a_made_up_ticket_gets_nowhere(secure):
    turn_on(secure)
    secure.cookies.clear()
    password_step(secure)
    assert secure.get("/api/graph").status_code == 401
    r = secure.post("/signin", data={"ticket": "made-up", "code": "123456"}, follow_redirects=False)
    assert r.status_code == 401 and "Sign in again" in r.text


def test_the_code_screen_expires(secure):
    secret, _ = turn_on(secure)
    secure.cookies.clear()
    _, ticket = password_step(secure)
    tickets = auth_provider(secure).second_step
    for k, (at, user, purpose) in list(tickets.items()):
        tickets[k] = (at - auth_mod.SECOND_STEP_TTL - 1, user, purpose)
    use_a_new_step(secure)
    r = secure.post("/signin", data={"ticket": ticket, "code": code_now(secret)}, follow_redirects=False)
    assert r.status_code == 401


def auth_provider(client):
    """The running ThreadAuthProvider (it holds the code-screen tickets)."""
    import gc
    return next(o for o in gc.get_objects() if isinstance(o, auth_mod.ThreadAuthProvider) and o.store is client.store)


def test_five_wrong_codes_lock_the_code_screen_for_15_minutes(secure):
    secret, _ = turn_on(secure)
    secure.cookies.clear()
    _, ticket = password_step(secure)
    for _ in range(5):
        assert secure.post("/signin", data={"ticket": ticket, "code": "000000"}).status_code == 401
    use_a_new_step(secure)
    locked = secure.post("/signin", data={"ticket": ticket, "code": code_now(secret)})
    assert locked.status_code == 429 and "15 minutes" in locked.text


def test_a_recovery_code_signs_you_in_once(secure):
    _, codes = turn_on(secure)
    secure.cookies.clear()
    _, ticket = password_step(secure)
    switched = secure.post("/signin", data={"ticket": ticket, "mode": "app", "switch": "1"})
    assert "Use a recovery code" in switched.text and 'value="recovery"' in switched.text
    ok = secure.post("/signin", data={"ticket": ticket, "mode": "recovery", "code": codes[0]}, follow_redirects=False)
    assert ok.status_code == 303
    assert secure.get("/api/account").json()["twofa"]["recovery_left"] == 9
    secure.cookies.clear()
    _, ticket = password_step(secure)
    assert secure.post("/signin", data={"ticket": ticket, "code": codes[0]}).status_code == 401


def test_few_recovery_codes_left_sends_you_to_settings(secure):
    _, codes = turn_on(secure)
    for c in codes[:7]:
        assert secure.store.check_second_step(uid(secure), c) == "recovery"
    secure.cookies.clear()
    _, ticket = password_step(secure)
    r = secure.post("/signin", data={"ticket": ticket, "mode": "recovery", "code": codes[7]}, follow_redirects=False)
    assert r.headers["location"] == "/?recovery-low=1#me"


def test_new_recovery_codes_replace_the_old_ones(secure):
    _, old = turn_on(secure)
    new = secure.post("/api/2fa/recovery", json={}, headers=H).json()["codes"]
    assert set(new).isdisjoint(old)
    assert secure.store.check_second_step(uid(secure), old[0]) is None
    assert secure.store.check_second_step(uid(secure), new[0]) == "recovery"


# ---------- OAuth clients ----------

def app_login(client):
    reg = client.post("/register", json={"redirect_uris": [REDIRECT], "client_name": "Claude",
                                         "token_endpoint_auth_method": "none",
                                         "grant_types": ["authorization_code", "refresh_token"],
                                         "response_types": ["code"]})
    verifier, challenge = pkce()
    auth = client.get("/authorize", params={"response_type": "code", "client_id": reg.json()["client_id"],
                                            "redirect_uri": REDIRECT, "code_challenge": challenge,
                                            "code_challenge_method": "S256", "state": "xyz"}, follow_redirects=False)
    return parse_qs(urlparse(auth.headers["location"]).query)["request"][0]


def test_an_ai_app_sign_in_also_needs_the_code(secure):
    secret, _ = turn_on(secure)
    fresh = TestClient(secure.app_, base_url=PUBLIC)
    request_id = app_login(fresh)
    page = fresh.post("/login", data={"request": request_id, "email": SAM, "password": PASSWORD}, follow_redirects=False)
    assert page.status_code == 200 and "Enter your code" in page.text      # no code handed to the app yet
    ticket = re.search(r'name="ticket" value="([^"]+)"', page.text).group(1)
    # A ticket from this app's screen can't be used on the browser sign-in (and the other way round).
    assert fresh.post("/signin", data={"ticket": ticket, "code": code_now(secret)}).status_code == 401
    wrong = fresh.post("/login", data={"request": request_id, "ticket": ticket, "code": "000000"}, follow_redirects=False)
    assert wrong.status_code == 401
    use_a_new_step(secure)
    done = fresh.post("/login", data={"request": request_id, "ticket": ticket, "code": code_now(secret)},
                      follow_redirects=False)
    assert done.status_code == 302 and "code=" in done.headers["location"]
    assert any("Claude" in s["device"] for s in secure.store.signins(uid(secure)))


def test_continue_as_you_still_works_in_a_browser_that_passed_two_step(secure):
    turn_on(secure)
    request_id = app_login(secure)
    page = secure.get("/login", params={"request": request_id})
    assert "Continue as" in page.text
    r = secure.post("/login", data={"request": request_id, "continue": "1"}, headers=H, follow_redirects=False)
    assert r.status_code == 302


# ---------- turning it off ----------

def test_turning_it_off_needs_the_password_and_a_code(secure):
    secret, _ = turn_on(secure)
    use_a_new_step(secure)
    assert secure.post("/api/2fa/off", json={"password": "nope", "code": code_now(secret)}, headers=H).status_code == 403
    assert secure.post("/api/2fa/off", json={"password": PASSWORD, "code": "000000"}, headers=H).status_code == 403
    assert secure.store.has_2fa(uid(secure))
    r = secure.post("/api/2fa/off", json={"password": PASSWORD, "code": code_now(secret)}, headers=H)
    assert r.status_code == 200 and not r.json()["twofa"]["on"] and not secure.store.has_2fa(uid(secure))


# ---------- step-up before sensitive actions ----------

def test_sensitive_actions_ask_you_to_confirm_it_s_you_after_15_minutes(secure):
    sign_in(secure)
    assert secure.get("/api/export").status_code == 200
    make_stale(secure)
    for r in (secure.get("/api/export"), secure.post("/api/backup-key", json={}, headers=H),
              secure.post("/api/2fa/start", json={}, headers=H)):
        assert r.status_code == 403 and r.json()["reauth"] is True
    assert secure.get("/api/account").json()["fresh"] is False
    assert secure.post("/api/reauth", json={"password": "wrong one"}, headers=H).status_code == 403
    assert secure.post("/api/reauth", json={"password": PASSWORD}, headers=H).json() == {"ok": True}
    assert secure.get("/api/export").status_code == 200


def test_step_up_with_two_step_on_needs_the_code_too(secure):
    secret, _ = turn_on(secure)
    make_stale(secure)
    assert secure.post("/api/reauth", json={"password": PASSWORD}, headers=H).json() == {"need_code": True}
    assert secure.get("/api/export").status_code == 403
    use_a_new_step(secure)
    assert secure.post("/api/reauth", json={"password": PASSWORD, "code": code_now(secret)}, headers=H).json() == {"ok": True}
    assert secure.get("/api/export").status_code == 200


def test_changing_the_password_with_two_step_on_needs_a_recent_sign_in(secure):
    turn_on(secure)
    make_stale(secure)
    r = secure.post("/api/password", json={"current": PASSWORD, "new": "a whole new passphrase"}, headers=H)
    assert r.status_code == 403 and r.json()["reauth"]


def test_another_website_can_t_use_the_account_endpoints(secure):
    sign_in(secure)
    evil = {"origin": "https://evil.example"}
    for path in ("/api/reauth", "/api/2fa/start", "/api/2fa/off", "/api/signout-others"):
        assert secure.post(path, json={"password": PASSWORD}, headers=evil).status_code == 403


# ---------- recent sign-ins and signing out elsewhere ----------

def test_recent_sign_ins_show_the_browser_and_never_the_address(secure):
    iphone = ("Mozilla/5.0 (iPhone; CPU iPhone OS 19_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
              "Version/19.0 Mobile/15E148 Safari/604.1")
    secure.post("/signin", data={"email": SAM, "password": PASSWORD}, headers={"user-agent": iphone},
                follow_redirects=False)
    rows = secure.get("/api/account").json()["signins"]
    assert rows[0]["device"] == "Safari on iPhone" and rows[0]["how"] == "password"
    raw = secure.store.path.read_text()
    assert "testclient" not in raw and "127.0.0.1" not in raw


def test_device_names():
    assert auth_mod.device_name("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/141.0.0.0 Safari/537.36") == "Chrome on Mac"
    assert auth_mod.device_name("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/141 Safari/537.36 Edg/141") == "Edge on Windows"
    assert auth_mod.device_name("") == "Unknown browser"


def test_sign_ins_are_kept_for_30_days_at_most_20(secure):
    user = uid(secure)
    for i in range(25):
        secure.store.note_signin(user, f"Browser {i}", "password")
    secure.store.data["users"][user]["signins"][0]["at"] -= 31 * 86400
    rows = secure.store.signins(user)
    assert len(rows) == 19 and rows[0]["device"] == "Browser 24"


def test_sign_out_everywhere_else(secure):
    other = TestClient(secure.app_, base_url=PUBLIC)
    sign_in(other)
    sign_in(secure)
    secure.store.data["refresh"]["x"] = {"user": uid(secure), "client_id": "c", "expires_at": time.time() + 999}
    secure.store.save()
    assert secure.get("/api/account").json()["other_browsers"] == 1
    r = secure.post("/api/signout-others", json={}, headers=H)
    assert r.status_code == 200 and r.json()["other_browsers"] == 0
    assert other.get("/api/graph").status_code == 401 and secure.get("/api/graph").status_code == 200
    assert "x" not in secure.store.data["refresh"]                       # AI apps signed out too


def test_home_suggests_two_step_from_the_third_sign_in_until_dismissed(secure):
    sign_in(secure)
    sign_in(secure)
    assert secure.get("/api/account").json()["nudge"] is False
    sign_in(secure)
    assert secure.get("/api/account").json()["nudge"] is True
    secure.post("/api/nudge-2fa", json={}, headers=H)
    assert secure.get("/api/account").json()["nudge"] is False


# ---------- support (thread-admin) ----------

def test_admin_can_turn_off_two_step_for_a_locked_out_person_and_it_s_logged(secure, monkeypatch, capsys):
    from thread import accounts
    turn_on(secure)
    monkeypatch.setattr(accounts, "AuthStore", lambda: secure.store)
    monkeypatch.setattr(sys, "argv", ["thread-admin", "twofa-off", SAM])
    accounts.admin_cli()
    assert "off" in capsys.readouterr().out and not secure.store.has_2fa(uid(secure))
    assert secure.get("/api/graph").status_code == 401                          # signed out everywhere
    log = [json.loads(line) for line in secure.store.path.with_name("admin-log.jsonl").read_text().splitlines()]
    assert log[-1]["action"] == "twofa-off" and log[-1]["user"] == uid(secure)
    assert "support" in secure.store.signins(uid(secure))[0]["how"]


def test_admin_reset_can_clear_two_step_too(secure, monkeypatch, capsys):
    from thread import accounts
    turn_on(secure)
    monkeypatch.setattr(accounts, "AuthStore", lambda: secure.store)
    monkeypatch.setattr(sys, "argv", ["thread-admin", "reset", SAM, "--2fa"])
    accounts.admin_cli()
    out = capsys.readouterr().out
    temporary = out.split(": ")[1].split()[0]
    assert "OFF" in out and not secure.store.has_2fa(uid(secure))
    assert sign_in(secure, SAM, temporary).status_code == 303                    # no code asked any more


# ---------- secrets never leak ----------

def test_no_secret_shows_up_in_api_answers_exports_or_printed_output(secure, capsys):
    secret, codes = turn_on(secure)
    w = secure.workspaces.get(uid(secure))
    w.sealer = secure.store.sealer
    from thread.sealed import write_secret
    write_secret(w.canvas_token_file, "canvas-token-SECRET-123", w.sealer)
    pages = [secure.get(p).text for p in ("/api/account", "/api/profile", "/api/setup", "/api/graph", "/api/tree")]
    blob = secure.get("/api/export").content
    names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
    printed = capsys.readouterr()
    for text in pages + [printed.out, printed.err, blob.decode("latin-1")]:
        assert secret not in text and "canvas-token-SECRET-123" not in text
        assert not any(c in text for c in codes)
    assert all(n.startswith("thread-memory/") and not n.endswith(("canvas-token", "google-token.json")) for n in names)
