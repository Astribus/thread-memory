"""Low-friction flows: one-tap "Continue as you" for AI apps, invite links, and a new account that
starts with what Thread can fill in itself."""

import time
from urllib.parse import parse_qs, urlparse

from starlette.testclient import TestClient

from thread.auth import SESSION_COOKIE, AuthStore
from thread.profile import guess_name, read_profile, school_from_email
from thread.server import build_online_app
from thread.workspace import Workspaces

PUBLIC = "https://thread-test.fly.dev"
SAM, PASSWORD = "sam@andrew.cmu.edu", "correct horse battery staple"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def online(tmp_path):
    store = AuthStore(tmp_path / "accounts.json")
    workspaces = Workspaces(tmp_path / "data")
    sam = store.create_user(SAM, PASSWORD)
    client = TestClient(build_online_app(store, PUBLIC, workspaces), base_url=PUBLIC)
    client.store, client.workspaces, client.sam = store, workspaces, sam
    return client


def start_app_sign_in(c):
    """Claude registers and starts sign-in; returns the /login request id (c.client_id / c.verifier saved)."""
    import base64, hashlib, secrets
    c.verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(c.verifier.encode()).digest()).rstrip(b"=").decode()
    reg = c.post("/register", json={"redirect_uris": [REDIRECT], "client_name": "Claude",
                                    "token_endpoint_auth_method": "none",
                                    "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
    client_id = c.client_id = reg.json()["client_id"]
    auth = c.get("/authorize", params={"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
                                       "code_challenge": challenge, "code_challenge_method": "S256", "state": "s"},
                 follow_redirects=False)
    return parse_qs(urlparse(auth.headers["location"]).query)["request"][0]


def browser_sign_in(c):
    r = c.post("/signin", data={"email": SAM, "password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 303


def test_signed_in_browser_approves_an_ai_app_in_one_tap(tmp_path):
    c = online(tmp_path)
    browser_sign_in(c)
    request_id = start_app_sign_in(c)
    page = c.get("/login", params={"request": request_id}).text
    assert f"Continue as {SAM}" in page and "claude.ai" in page and 'type="password"' not in page
    done = c.post("/login", data={"request": request_id, "continue": "1"}, headers={"origin": PUBLIC},
                  follow_redirects=False)
    assert done.status_code == 302 and done.headers["location"].startswith(REDIRECT)
    code = parse_qs(urlparse(done.headers["location"]).query)["code"][0]
    tokens = c.post("/token", data={"grant_type": "authorization_code", "code": code, "client_id": c.client_id,
                                    "redirect_uri": REDIRECT, "code_verifier": c.verifier}).json()
    assert c.store.connected_apps(c.sam) == ["Claude"] and "access_token" in tokens   # it's Sam's account


def test_without_a_session_it_asks_for_the_password(tmp_path):
    c = online(tmp_path)
    request_id = start_app_sign_in(c)
    page = c.get("/login", params={"request": request_id}).text
    assert 'type="password"' in page and "Continue as" not in page
    refused = c.post("/login", data={"request": request_id, "continue": "1"}, headers={"origin": PUBLIC},
                     follow_redirects=False)
    assert refused.status_code == 401 and 'type="password"' in refused.text   # falls back, no code


def test_one_tap_refuses_a_post_from_another_site(tmp_path):
    c = online(tmp_path)
    browser_sign_in(c)
    request_id = start_app_sign_in(c)
    for origin in ("https://evil.example", ""):
        r = c.post("/login", data={"request": request_id, "continue": "1"},
                   headers={"origin": origin} if origin else {}, follow_redirects=False)
        assert r.status_code == 401 and "code=" not in r.headers.get("location", "")


def test_expired_or_signed_out_session_falls_back_to_password(tmp_path):
    c = online(tmp_path)
    browser_sign_in(c)
    for entry in c.store.data["sessions"].values():
        entry["expires_at"] = time.time() - 1
    request_id = start_app_sign_in(c)
    assert "Continue as" not in c.get("/login", params={"request": request_id}).text


def test_use_a_different_account_shows_the_password_form(tmp_path):
    c = online(tmp_path)
    browser_sign_in(c)
    request_id = start_app_sign_in(c)
    page = c.get("/login", params={"request": request_id, "other": "1"}).text
    assert 'type="password"' in page and "Continue as" not in page


def test_invite_link_fills_the_code_and_says_who_invited(tmp_path):
    c = online(tmp_path)
    code = c.store.make_invite("Riley", by="Jordan")
    page = c.get(f"/join/{code}").text
    assert "<b>Jordan</b> invited you" in page and f'type="hidden" name="code" value="{code}"' in page
    assert 'passwordrules="minlength: 12;"' in page and 'autocomplete="new-password"' in page
    assert c.get("/join/made-up-code").status_code == 404
    r = c.post("/signup", data={"code": code, "via": "link", "email": "riley.park@andrew.cmu.edu",
                                "password": PASSWORD, "timezone": "America/New_York"}, follow_redirects=False)
    assert r.status_code == 303
    w = c.workspaces.get(c.store.user_id("riley.park@andrew.cmu.edu"))
    about = read_profile(w.brain)["about"]
    assert about["school"] == "Carnegie Mellon University" and about["timezone"] == "America/New_York"
    assert not about["name"]          # the name is asked once, in setup, pre-filled with a guess (not saved here)


def test_admin_invite_prints_a_link(tmp_path, monkeypatch, capsys):
    import sys
    from thread import accounts
    store = AuthStore(tmp_path / "a.json")
    monkeypatch.setattr(accounts, "AuthStore", lambda: store)
    monkeypatch.setenv("THREAD_PUBLIC_URL", PUBLIC)
    monkeypatch.setattr(sys, "argv", ["thread-admin", "invite", "Sam", "--by", "Jordan"])
    accounts.admin_cli()
    out = capsys.readouterr().out
    code = out.split("Invite code: ")[1].split()[0]
    assert f"{PUBLIC}/join/{code}" in out and store.invite_info(code)["by"] == "Jordan"


def test_name_and_school_guesses_are_careful():
    assert guess_name("riley.park@gmail.com") == "Riley"
    assert guess_name("rileypark550@gmail.com") == "" and guess_name("rpark4@andrew.cmu.edu") == ""
    assert school_from_email("x@andrew.cmu.edu") == "Carnegie Mellon University" and school_from_email("x@gmail.com") == ""


# ---- Canvas from the Calendar Feed link (no token) ----
FEED = "\r\n".join([
    "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Instructure//Canvas",
    "BEGIN:VEVENT", "UID:event-assignment-101", "DTSTART:{soon}", "SUMMARY:Homework 7 [21127 Concepts of Mathemat",
    " ics]", "URL:https://canvas.cmu.edu/courses/1/assignments/101", "END:VEVENT",
    "BEGIN:VEVENT", "UID:event-assignment-102", "DTSTART;VALUE=DATE:{day}", "SUMMARY:Exam 2 [21241 Matrices]",
    "URL:https://canvas.cmu.edu/courses/2/assignments/102", "END:VEVENT",
    "BEGIN:VEVENT", "UID:event-assignment-103", "DTSTART:20200101T120000Z", "SUMMARY:Old quiz [21241 Matrices]",
    "END:VEVENT",
    "END:VCALENDAR", ""])


def feed_text():
    from datetime import datetime, timedelta, timezone
    soon = datetime.now(timezone.utc) + timedelta(days=2)
    return FEED.format(soon=soon.strftime("%Y%m%dT%H%M%SZ"), day=(soon + timedelta(days=3)).strftime("%Y%m%d"))


def fake_feed(text=None, status=200):
    import httpx
    return httpx.MockTransport(lambda req: httpx.Response(status, text=text if text is not None else feed_text()))


def test_feed_gives_deadlines_by_class_without_a_token():
    from thread.canvas_feed import FeedCanvas
    canvas = FeedCanvas("https://canvas.cmu.edu/feeds/calendars/user_abc.ics", transport=fake_feed())
    items = canvas.upcoming(14)
    assert [i["title"] for i in items] == ["Homework 7", "Exam 2"]          # folded line joined, old one dropped
    assert items[0]["course"] == "21127 Concepts of Mathematics" and items[1]["type"] == "assignment"
    assert set(canvas.course_labels().values()) == {"21127 Concepts of Mathematics", "21241 Matrices"}


def test_feed_link_must_be_a_real_canvas_feed():
    import pytest
    from thread.canvas_feed import FeedError, check_feed_url
    for bad in ("http://canvas.cmu.edu/feeds/calendars/user_a.ics", "https://evil.example/steal",
                "https://127.0.0.1/feeds/calendars/user_a.ics", "https://localhost/feeds/calendars/user_a.ics"):
        with pytest.raises(FeedError):
            check_feed_url(bad)


def test_whats_due_from_feed_says_what_it_cant_see(tmp_path):
    import asyncio
    from thread.brain import Brain
    from thread.canvas_feed import FeedCanvas
    from thread.server import build_server
    canvas = FeedCanvas("https://canvas.cmu.edu/feeds/calendars/user_abc.ics", transport=fake_feed())
    out = str(asyncio.run(build_server(Brain(tmp_path), canvas).call_tool("whats_due", {"days": 14})))
    assert "Homework 7" in out and "Exam 2" in out and "calendar feed" in out and "Advanced" in out
    syl = str(asyncio.run(build_server(Brain(tmp_path), canvas).call_tool("read_syllabus", {"course": "21127"})))
    assert "access token" in syl


def test_pasting_a_feed_link_checks_it_and_marks_canvas_done(tmp_path, monkeypatch):
    from thread import canvas_feed
    c = online(tmp_path)
    browser_sign_in(c)
    H = {"origin": PUBLIC}
    monkeypatch.setattr(canvas_feed, "fetch_feed", lambda url: (_ for _ in ()).throw(canvas_feed.FeedError("bad link")))
    assert c.post("/api/canvas-feed", json={"url": "x"}, headers=H).status_code == 400
    monkeypatch.setattr(canvas_feed, "fetch_feed", lambda url: feed_text())
    r = c.post("/api/canvas-feed", json={"url": "https://canvas.cmu.edu/feeds/calendars/user_abc.ics"}, headers=H)
    assert r.status_code == 200 and r.json()["events"] == 3
    assert c.get("/api/setup").json()["done"]["canvas"] is True
    w = c.workspaces.get(c.sam)
    assert oct(w.canvas_feed_file.stat().st_mode)[-3:] == "600"
    assert c.post("/api/canvas-feed", json={"url": "x"}, headers={"origin": "https://evil.example"}).status_code == 403
    c.post("/api/canvas-feed/disconnect", json={}, headers=H)
    assert w.canvas_feed() is None


# ---- sign-up, one question per screen ----
def test_each_signup_screen_is_checked_without_creating_anything(tmp_path):
    c = online(tmp_path)
    code = c.store.make_invite("Alex", by="Jordan")
    assert c.post("/signup/check", data={"code": "nope"}).status_code == 403
    assert c.post("/signup/check", data={"code": code}).json() == {"ok": True}
    assert c.post("/signup/check", data={"code": code, "email": "not-an-email"}).status_code == 400
    taken = c.post("/signup/check", data={"code": code, "email": SAM.upper()})
    assert taken.status_code == 400 and "already an account" in taken.json()["error"]
    assert c.post("/signup/check", data={"code": code, "email": "alex@andrew.cmu.edu"}).json() == {"ok": True}
    assert c.store.invite_valid(code) and not c.store.user_id("alex@andrew.cmu.edu")   # nothing used or made


def test_signup_page_has_one_question_per_screen_and_returns_to_the_failing_one(tmp_path):
    c = online(tmp_path)
    code = c.store.make_invite("Alex", by="Jordan")
    page = c.get(f"/join/{code}").text
    assert page.count('class="q"') == 3            # email, password, time zone (the code rides along hidden)
    assert page.count('class="q" data-q="code"') == 0 and 'id="q-back"' in page and 'id="q-next"' in page
    assert c.get("/signup").text.count('class="q"') == 4       # without a link, the code gets its own screen
    short = c.post("/signup", data={"code": code, "via": "link", "email": "alex@andrew.cmu.edu", "password": "short",
                                    "timezone": "America/New_York"}, follow_redirects=False)
    assert short.status_code == 400 and 'const errorAt = "password"' in short.text
    assert 'value="alex@andrew.cmu.edu"' in short.text and c.store.invite_valid(code)   # answers kept, invite kept


def test_home_week_and_class_colours(tmp_path, monkeypatch):
    from thread import canvas_feed
    c = online(tmp_path)
    browser_sign_in(c)
    H = {"origin": PUBLIC}
    assert c.get("/api/due").json() == {"connected": False, "items": []}
    monkeypatch.setattr(canvas_feed, "fetch_feed", lambda url, transport=None: feed_text())
    c.post("/api/canvas-feed", json={"url": "https://canvas.cmu.edu/feeds/calendars/user_abc.ics"}, headers=H)
    due = c.get("/api/due").json()
    assert [i["title"] for i in due["items"]] == ["Homework 7", "Exam 2"] and due["source"] == "feed"
    assert due["items"][0]["class_key"] == "21127-concepts-of-mathematics" and due["items"][0]["time"]
    assert c.post("/api/class-colors", json={"key": "21127-concepts-of-mathematics", "index": 3}, headers=H).json() == {
        "21127-concepts-of-mathematics": 3}
    assert c.post("/api/class-colors", json={"key": "../x", "index": 1}, headers=H).status_code == 400
    assert c.post("/api/class-colors", json={"key": "a", "index": 1}, headers={"origin": "https://evil.example"}).status_code == 403
