"""Time zones: chosen at sign-up, stored in the profile, never silently replaced by the browser's zone."""

import json

import httpx
import pytest
from starlette.testclient import TestClient

from thread import timezones
from thread.auth import AuthStore
from thread.brain import Brain
from thread.canvas import CanvasClient
from thread.server import build_online_app
from thread.workspace import Workspace, Workspaces

PUBLIC = "https://thread-test.fly.dev"
H = {"origin": PUBLIC}
PASSWORD = "correct horse battery staple"


@pytest.fixture
def site(tmp_path):
    store = AuthStore(tmp_path / "accounts.json")
    workspaces = Workspaces(tmp_path / "data")
    app = build_online_app(store, PUBLIC, workspaces)
    with TestClient(app, base_url=PUBLIC) as client:
        client.store, client.workspaces = store, workspaces
        yield client


def sign_up(site, zone, email="lagos@example.com"):
    code = site.store.make_invite()
    return site.post("/signup", data={"code": code, "email": email, "password": PASSWORD, "timezone": zone},
                     follow_redirects=False), code


def test_every_real_zone_is_offered_and_old_aliases_are_not():
    ids = {z["id"] for g in timezones.choices() for z in g["zones"]}
    assert {"Asia/Kolkata", "Africa/Lagos", "America/Sao_Paulo", "Pacific/Auckland", "Asia/Kathmandu", "UTC"} <= ids
    assert not ids & {"Asia/Calcutta", "US/Eastern", "EST5EDT", "Etc/GMT+5", "Japan", "Europe/Kiev"}
    assert len(ids) > 400
    assert timezones.label("Asia/Kolkata").startswith("Kolkata · India (UTC+5:30")
    assert timezones.canonical("Asia/Calcutta") == "Asia/Kolkata"      # old names a browser may report
    assert timezones.canonical("Mars/Olympus_Mons") is None and timezones.canonical("EST") is None


def test_sign_up_page_asks_for_a_time_zone_from_the_full_list(site):
    page = site.get("/signup").text
    assert 'name="timezone"' in page and "required" in page and "Kolkata · India" in page
    assert "Intl.DateTimeFormat" in page                                # pre-selects the device's zone


def test_sign_up_requires_a_real_zone_and_saves_it_to_the_profile(site):
    r, code = sign_up(site, "")
    assert r.status_code == 400 and "time zone" in r.text and site.store.invite_valid(code)   # code not used up
    r, _ = sign_up(site, "Mars/Olympus_Mons")
    assert r.status_code == 400
    r, _ = sign_up(site, "Africa/Lagos")
    assert r.status_code == 303
    w = site.workspaces.get(site.store.user_id("lagos@example.com"))
    assert str(w.timezone()) == "Africa/Lagos"
    assert "- Time zone: Africa/Lagos" in w.brain.profile()          # a memory: visible, editable, AI sees it


def test_browser_reports_never_overwrite_but_switch_does(site):
    sign_up(site, "America/New_York")
    w = site.workspaces.get(site.store.user_id("lagos@example.com"))
    info = site.get("/api/timezone?browser=America/Chicago").json()
    assert info["differs"] and info["city"] == "New York" and info["browser_city"] == "Chicago"
    assert str(w.timezone()) == "America/New_York"                    # just asking doesn't change anything
    assert not site.get("/api/timezone?browser=America/Detroit").json()["differs"]   # same clock: no nagging
    assert site.post("/api/timezone", json={"timezone": "America/Chicago"}, headers=H).status_code == 200  # Switch
    assert str(w.timezone()) == "America/Chicago"


def test_editing_the_profile_changes_the_times_the_ai_reads(site):
    sign_up(site, "America/New_York")
    uid = site.store.user_id("lagos@example.com")
    profile = site.get("/api/profile").json()["profile"]
    profile["about"]["timezone"] = "Asia/Tokyo"
    assert site.put("/api/profile", json=profile, headers=H).status_code == 200
    assert str(site.workspaces.get(uid).timezone()) == "Asia/Tokyo"
    profile["about"]["timezone"] = "Nowhere/Real"
    assert site.put("/api/profile", json=profile, headers=H).status_code == 400
    # a save that doesn't mention the zone (older page, a template) keeps it
    del profile["about"]["timezone"]
    site.put("/api/profile", json=profile, headers=H)
    assert str(site.workspaces.get(uid).timezone()) == "Asia/Tokyo"


def test_whats_due_follows_the_profile_zone(tmp_path):
    from thread.server import build_server
    items = [{"plannable_type": "assignment", "context_name": "Math", "course_id": 1,
              "plannable": {"title": "HW 5", "due_at": "2026-10-22T03:59:00Z"},
              "submissions": {"submitted": False}, "html_url": "/courses/1/assignments/5"}]
    def handler(request):
        if request.url.path.endswith("/courses"):
            return httpx.Response(200, json=[{"id": 1, "name": "Math", "course_code": "21-127"}])
        return httpx.Response(200, json=items)
    canvas = CanvasClient("t", "https://canvas.test", transport=httpx.MockTransport(handler))
    w = Workspace(Brain(tmp_path / "brain"), canvas=canvas, settings=tmp_path / "settings.json")
    w.set_timezone("Asia/Tokyo")

    import asyncio
    from unittest.mock import patch
    with patch("thread.server.Workspace.local", return_value=w):
        out = str(asyncio.run(build_server(w.brain, canvas).call_tool("whats_due", {"days": 30})))
    assert "Thu Oct 22, 12:59 PM JST" in out


def test_old_browser_reported_zone_moves_into_the_profile_once(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"timezone": "America/Chicago", "setup": {"seen": True, "skipped": []}}))
    w = Workspace(Brain(tmp_path / "brain"), settings=settings)
    assert str(w.timezone()) == "America/Chicago"
    assert "- Time zone: America/Chicago" in w.brain.profile()
    left = json.loads(settings.read_text())
    assert "timezone" not in left and left["setup"]["seen"] is True        # other settings untouched


def test_laptop_without_a_zone_uses_the_computer_and_can_choose_one(tmp_path):
    w = Workspace(Brain(tmp_path / "brain"))
    assert w.timezone() is None
    w.set_timezone("Europe/Berlin")
    assert str(w.timezone()) == "Europe/Berlin"
