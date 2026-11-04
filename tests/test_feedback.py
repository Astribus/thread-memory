"""Bug reports and ideas: from the Settings card and from the AI, into one shared file for the operator."""

import json
import sys

import pytest
from starlette.testclient import TestClient

from test_auth import BASE, PASSWORD, call, open_chat, sign_in
from thread.auth import SESSION_COOKIE, AuthStore
from thread.feedback import MAX_CHARS, MAX_PER_HOUR, Feedback, describe
from thread.server import build_online_app
from thread.workspace import Workspaces

SAM, ALEX = "sam@andrew.cmu.edu", "alex@andrew.cmu.edu"
H = {"origin": BASE}


@pytest.fixture
def online(tmp_path):
    store = AuthStore(tmp_path / "accounts.json")
    store.create_user(SAM, PASSWORD)
    store.create_user(ALEX, PASSWORD)
    workspaces = Workspaces(tmp_path / "data")
    with TestClient(build_online_app(store, BASE, workspaces), base_url=BASE) as client:
        client.store, client.reports = store, Feedback(tmp_path / "data" / "feedback" / "reports.jsonl")
        yield client


def browser_sign_in(client, email=SAM):
    r = client.post("/signin", data={"email": email, "password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 303 and (r.cookies.get(SESSION_COOKIE) or client.cookies.get(SESSION_COOKIE))


def test_settings_report_is_saved_with_its_context_never_notes(online):
    browser_sign_in(online)
    context = {"tab": "Me → settings", "screen": "440×956", "viewport": "440×837", "browser": "iPhone Safari",
               "notes": "should be dropped"}
    r = online.post("/api/feedback", json={"message": "Bottom bar vanished after New note → cancel", "context": context},
                    headers=H)
    assert r.status_code == 200, r.text
    [report] = online.reports.latest()
    assert report["message"].startswith("Bottom bar vanished") and report["source"] == "page"
    assert report["account"] == online.store.user_id(SAM) and report["email"] == SAM
    assert report["context"] == {"tab": "Me → settings", "screen": "440×956", "viewport": "440×837",
                                 "browser": "iPhone Safari"}                     # unknown keys are dropped
    assert oct(online.reports.path.stat().st_mode)[-3:] == "600"


def test_report_from_another_site_is_refused(online):
    browser_sign_in(online)
    r = online.post("/api/feedback", json={"message": "spam"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403 and online.reports.latest() == []


def test_the_ai_reports_for_its_own_person_and_both_land_in_one_file(online):
    browser_sign_in(online)                                         # Sam reports from the page…
    online.post("/api/feedback", json={"message": "Recenter is hard to find"}, headers=H)
    _, alex = sign_in(online, email=ALEX)                            # …and Alex's AI reports for Alex
    chat, init = open_chat(online, alex)
    assert "report_bug" in init                                     # every connected AI is told the tool exists
    reply = call(online, chat, "report_bug", {"description": "Exam dates show in UTC",
                                              "what_user_was_doing": "asked what's due"})
    assert "Sent to the Thread team" in reply
    newest, older = online.reports.latest()
    assert (newest["source"], newest["account"], newest["email"]) == ("ai", online.store.user_id(ALEX), ALEX)
    assert "What they were doing: asked what's due" in newest["message"]
    assert (older["source"], older["email"]) == ("page", SAM)


def test_limits_refuse_with_a_message_never_cut(tmp_path):
    fb = Feedback(tmp_path / "reports.jsonl")
    with pytest.raises(ValueError, match="Write what happened"):
        fb.add("   ", "a1", "page")
    with pytest.raises(ValueError, match="under 4,000"):
        fb.add("x" * (MAX_CHARS + 1), "a1", "page")
    for i in range(MAX_PER_HOUR):
        fb.add(f"report {i}", "a1", "page", now=1000.0 + i)
    with pytest.raises(ValueError, match="a lot of reports"):
        fb.add("one more", "a1", "page", now=1100.0)
    fb.add("someone else is fine", "b2", "page", now=1100.0)        # the limit is per person
    fb.add("an hour later is fine", "a1", "page", now=1000.0 + 3601 + MAX_PER_HOUR)
    assert len(fb.latest(100)) == MAX_PER_HOUR + 2


def test_admin_feedback_prints_the_latest_reports(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("THREAD_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("THREAD_CONFIG_DIR", str(tmp_path / "config"))
    fb = Feedback(tmp_path / "feedback" / "reports.jsonl")
    fb.add("first", "a1", "page", SAM, {"screen": "1440×900"}, now=1000.0)
    fb.add("second\nwith two lines", "b2", "ai", ALEX, now=2000.0)
    from thread import auth
    monkeypatch.setattr(sys, "argv", ["thread-admin", "feedback", "5"])
    monkeypatch.setattr(auth, "AuthStore", lambda: None)            # the command never needs the accounts file
    auth.admin_cli()
    out = capsys.readouterr().out
    assert out.index("second") < out.index("first")                 # newest first
    assert f"{ALEX} via ai" in out and "screen 1440×900" in out and "  with two lines" in out
    assert describe(json.loads(fb.path.read_text().splitlines()[0])).startswith("[1970-01-01T00:16:40+00:00]")
