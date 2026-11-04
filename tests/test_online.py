"""The online server: accounts with invite-only sign-up, each person's memory kept apart, home screen behind
a browser sign-in, AI connection behind OAuth, one address."""

import pytest
from starlette.testclient import TestClient

from thread.auth import SESSION_COOKIE, AuthStore
from thread.server import build_online_app
from thread.workspace import Workspaces

PUBLIC = "https://thread-test.fly.dev"
SAM, ALEX = "sam@andrew.cmu.edu", "alex@andrew.cmu.edu"
PASSWORD = "correct horse battery staple"
H = {"origin": PUBLIC}
TZ = "America/New_York"


@pytest.fixture
def online(tmp_path):
    store = AuthStore(tmp_path / "accounts.json")
    workspaces = Workspaces(tmp_path / "data")
    sam = store.create_user(SAM, PASSWORD)
    workspaces.get(sam).brain.remember("Exam 1 weak spots", "contingency tables", folder="classes/36200")
    app = build_online_app(store, PUBLIC, workspaces)
    with TestClient(app, base_url=PUBLIC) as client:
        client.store, client.workspaces, client.app_ = store, workspaces, app
        yield client


def sign_in(client, email=SAM, password=PASSWORD):
    return client.post("/signin", data={"email": email, "password": password}, follow_redirects=False)


def browser(online):
    """Another person's browser on the same server."""
    other = TestClient(online.app_, base_url=PUBLIC)
    return other


def test_home_screen_is_locked_until_you_sign_in(online):
    assert online.get("/", follow_redirects=False).headers["location"] == "/signin"
    assert online.get("/api/graph").status_code == 401
    assert online.get("/static/app.js", follow_redirects=False).status_code == 303
    page = online.get("/signin").text
    assert "Sign in to see your memory" in page and 'name="email"' in page and "/signup" in page


def test_wrong_password_or_unknown_email_look_the_same(online):
    for email, password in ((SAM, "nope"), ("nobody@andrew.cmu.edu", PASSWORD)):
        r = sign_in(online, email, password)
        assert r.status_code == 401 and "Wrong email or password" in r.text
        assert SESSION_COOKIE not in r.cookies


def test_sign_in_opens_the_map_with_a_safe_cookie(online):
    r = sign_in(online, "  Sam@Andrew.CMU.edu ", PASSWORD)     # email case and spaces don't matter
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    assert "max-age=604800" in cookie                          # 7 days
    assert online.get("/").status_code == 200 and '<canvas id="map"' in online.get("/").text
    assert online.get("/api/graph").json()["nodes"]
    assert online.get("/api/profile").json()["email"] == SAM
    token = r.cookies.get(SESSION_COOKIE) or online.cookies.get(SESSION_COOKIE)
    assert token and token not in online.store.path.read_text()   # sessions are stored only as hashes


def test_sign_up_needs_a_working_invite_code_that_works_once(online):
    assert online.post("/signup", data={"code": "made-up-code", "email": ALEX, "password": PASSWORD, "timezone": TZ},
                       follow_redirects=False).status_code == 403
    code = online.store.make_invite("Alex")
    assert code not in online.store.path.read_text()            # invite codes are stored only as hashes
    # a failed sign-up (password too short) doesn't use the code up
    r = online.post("/signup", data={"code": code, "email": ALEX, "password": "short", "timezone": TZ}, follow_redirects=False)
    assert r.status_code == 400 and "12 characters" in r.text
    r = online.post("/signup", data={"code": code.upper(), "email": ALEX, "password": PASSWORD, "timezone": TZ},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?welcome=4#home"   # 4 sign-up screens without a link
    assert online.get("/api/profile").json()["email"] == ALEX   # signed straight in
    again = browser(online).post("/signup", data={"code": code, "email": "eve@x.com", "password": PASSWORD, "timezone": TZ},
                                 follow_redirects=False)
    assert again.status_code == 403                             # one use only


def test_invite_made_by_thread_admin_works_without_a_restart(online):
    # `thread-admin invite` runs as a separate process writing the same file.
    online.get("/signin")                                         # server is up and has read the file
    code = AuthStore(online.store.path).make_invite("Jordan")   # second process, as via fly ssh
    sign_in(online)   # server writes the file (new session)
    assert AuthStore(online.store.path).invite_valid(code)   # without dropping the invite
    r = browser(online).post("/signup", data={"code": code, "email": ALEX, "password": PASSWORD, "timezone": TZ},
                             follow_redirects=False)
    assert r.status_code == 303


def test_cant_sign_up_twice_with_the_same_email(online):
    code = online.store.make_invite()
    r = online.post("/signup", data={"code": code, "email": SAM.upper(), "password": PASSWORD, "timezone": TZ},
                    follow_redirects=False)
    assert r.status_code == 400 and "already an account" in r.text
    assert online.store.invite_valid(code)


def test_each_person_sees_only_their_own_memory(online):
    alex = online.store.create_user(ALEX, PASSWORD)
    online.workspaces.get(alex).brain.remember("Alex's secret plan", "move to Boston", folder="personal")
    sign_in(online)                                    # Sam
    other = browser(online)
    sign_in(other, ALEX)                               # Alex, in another browser
    sam_ids = {n["id"] for n in online.get("/api/graph").json()["nodes"]}
    alex_ids = {n["id"] for n in other.get("/api/graph").json()["nodes"]}
    assert any("exam-1" in i for i in sam_ids) and not any("secret" in i for i in sam_ids)
    assert any("secret" in i for i in alex_ids) and not any("exam-1" in i for i in alex_ids)
    secret = next(i for i in alex_ids if "secret" in i)
    assert online.get("/api/note", params={"id": secret}).status_code == 404
    # writes land in the writer's own folder only
    online.post("/api/folder", json={"name": "Sam stuff"}, headers=H)
    assert "Sam stuff" not in str(other.get("/api/tree").json())
    # export contains only this account's notes
    assert b"secret" not in online.get("/api/export").content


def test_changes_need_the_session_and_thread_s_own_address(online):
    sign_in(online)
    assert online.post("/api/folder", json={"name": "Projects"}, headers=H).status_code == 200
    evil = online.post("/api/folder", json={"name": "Evil"}, headers={"origin": "https://evil.example"})
    assert evil.status_code == 403


def test_sign_out_and_new_password_end_only_that_person_s_sessions(online):
    alex = online.store.create_user(ALEX, PASSWORD)
    sign_in(online)
    online.get("/signout", follow_redirects=False)
    assert online.get("/api/graph").status_code == 401
    sign_in(online)
    other = browser(online)
    sign_in(other, ALEX)
    online.store.set_password(online.store.user_id(SAM), "a totally different long password")
    assert online.get("/api/graph").status_code == 401     # Sam is signed out
    assert other.get("/api/graph").status_code == 200      # Alex isn't


def test_lockout_is_per_account(online):
    online.store.create_user(ALEX, PASSWORD)
    for _ in range(5):
        sign_in(online, SAM, "wrong")
    assert sign_in(online, SAM, PASSWORD).status_code == 429   # Sam is locked, even with the right password
    assert sign_in(online, ALEX, PASSWORD).status_code == 303  # someone guessing at Sam can't lock Alex out


def test_ai_connection_still_needs_its_own_sign_in(online):
    sign_in(online)                       # a browser session is NOT an AI-app token
    r = online.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
        headers={"accept": "application/json, text/event-stream", "content-type": "application/json"})
    assert r.status_code == 401


def test_requests_to_other_hostnames_are_refused(online):
    r = online.post("/mcp", json={}, headers={"host": "attacker.example", "content-type": "application/json",
                                              "accept": "application/json, text/event-stream"})
    assert r.status_code in (401, 421)


def test_canvas_token_is_per_person_and_checked_first(online, monkeypatch):
    from thread import canvas, home

    class Fake:
        def __init__(self, token):
            self.token = token

        def courses(self):
            if self.token != "good-token":
                raise canvas.CanvasAuthError("Invalid access token.")
            return []
    monkeypatch.setattr(home, "CanvasClient", Fake)
    sign_in(online)
    assert online.get("/api/profile").json()["canvas"] is False
    assert online.post("/api/canvas", json={"token": "bad"}, headers=H).status_code == 400
    assert online.post("/api/canvas", json={"token": "good-token"}, headers=H).json() == {"canvas": True}
    sam = online.workspaces.get(online.store.user_id(SAM))
    assert sam.canvas_token() == "good-token" and oct(sam.canvas_token_file.stat().st_mode)[-3:] == "600"
    assert online.get("/api/profile").json()["canvas"] is True
    alex = online.store.create_user(ALEX, PASSWORD)
    assert online.workspaces.get(alex).canvas_token() is None
    online.post("/api/canvas/disconnect", json={}, headers=H)
    assert sam.canvas_token() is None


def test_google_sign_in_must_come_back_to_the_person_who_started_it(tmp_path):
    from thread import gcal
    gcal._pending["state-1"] = ("verifier", "sam-id")
    with pytest.raises(gcal.CalendarAuthError):
        gcal.finish_connect("https://x/google/callback", "state-1", "code", tmp_path / "t.json", owner="alex-id")


def test_admin_reset_gives_a_working_temporary_password(online, monkeypatch, capsys):
    import sys

    from thread import accounts as auth   # thread-admin uses the lightweight accounts module
    monkeypatch.setattr(auth, "AuthStore", lambda: online.store)
    sign_in(online)
    monkeypatch.setattr(sys, "argv", ["thread-admin", "reset", SAM])
    auth.admin_cli()
    temporary = capsys.readouterr().out.split(": ")[1].split()[0]
    assert online.get("/api/graph").status_code == 401             # signed out everywhere
    assert sign_in(online, SAM, temporary).status_code == 303
    monkeypatch.setattr(sys, "argv", ["thread-admin", "invite", "Alex"])
    auth.admin_cli()
    code = capsys.readouterr().out.split(": ")[1].split()[0]
    assert online.store.invite_valid(code)


def test_each_account_gets_its_own_browser_storage_key_and_sign_out_clears_it(online):
    r = sign_in(online)
    sam_key = r.cookies.get("thread_acct") or online.cookies.get("thread_acct")
    online.store.create_user(ALEX, PASSWORD)
    other = browser(online)
    alex = sign_in(other, ALEX)
    alex_key = alex.cookies.get("thread_acct") or other.cookies.get("thread_acct")
    assert sam_key and alex_key and sam_key != alex_key
    assert "thread_acct=" in online.get("/signout", follow_redirects=False).headers["set-cookie"]


def test_app_icon_loads_even_when_signed_out(online):
    assert online.get("/favicon.svg").status_code == 200
    assert online.get("/apple-touch-icon.png").headers["content-type"] == "image/png"
    assert 'rel="apple-touch-icon"' in online.get("/signin").text


def test_the_page_reports_the_browsers_time_zone(online):
    sign_in(online)
    sam = online.store.user_id(SAM)
    assert online.post("/api/timezone", json={"timezone": "America/Chicago"}, headers=H).status_code == 200
    assert str(online.workspaces.get(sam).timezone()) == "America/Chicago"
    assert online.post("/api/timezone", json={"timezone": "Nowhere/Real"}, headers=H).status_code == 400


def test_ai_is_told_to_save_everything_and_ask_once_until_the_user_chooses(online, tmp_path):
    from thread.server import ASK_SAVING, SAVE_EVERYTHING, SAVE_WHEN_ASKED
    sign_in(online)
    sam = online.store.user_id(SAM)
    brain = online.workspaces.get(sam).brain
    assert brain.saving() is None
    assert online.get("/api/profile").json()["saving"] == "everything"     # shown as the default
    assert online.post("/api/saving", json={"mode": "ask"}, headers=H).json()["saving"] == "ask"
    assert brain.saving() == "ask"
    assert online.post("/api/saving", json={"mode": "never"}, headers=H).status_code == 400
    assert "FIRST" in ASK_SAVING and "Save as you go" in SAVE_EVERYTHING and "only when the user asks" in SAVE_WHEN_ASKED


def test_a_zone_picked_at_sign_up_isn_t_questioned_right_after(online):
    # A zone chosen at sign-up counts as kept, so the device-zone prompt doesn't follow.
    code = online.store.make_invite("Jordan")
    r = online.post("/signup", data={"code": code, "email": ALEX, "password": PASSWORD, "timezone": "Asia/Kolkata",
                                     "device_zone": TZ}, follow_redirects=False)
    assert r.status_code == 303
    assert not online.get(f"/api/timezone?browser={TZ}").json()["differs"]
    assert online.get("/api/timezone?browser=America/Chicago").json()["differs"]   # a new place still asks


def test_keep_my_zone_is_remembered_on_the_server(online):
    sign_in(online)
    online.post("/api/timezone", json={"timezone": TZ}, headers=H)
    assert online.get("/api/timezone?browser=Asia/Kolkata").json()["differs"]
    assert online.post("/api/timezone", json={"keep": "Asia/Kolkata"}, headers=H).status_code == 200
    assert not browser_signed_in(online).get("/api/timezone?browser=Asia/Kolkata").json()["differs"]


def browser_signed_in(online):
    other = browser(online)
    sign_in(other)
    return other


def test_calendar_tool_online_without_google_says_it_isn_t_available(online, monkeypatch, tmp_path):
    # Setup must not point at a Google button that isn't configured.
    from thread import gcal, server
    monkeypatch.setattr(gcal, "CLIENT_FILE", tmp_path / "missing.json")
    assert server._no_calendar(gcal.CalendarAuthError("x"), online=True) == server.NO_GOOGLE_HERE
    assert server._no_calendar(gcal.CalendarAuthError("x"), online=False) != server.NO_GOOGLE_HERE
