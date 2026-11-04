"""The Me page: profile form ↔ me.md, settings endpoints, and password change online."""

import io
import zipfile

from starlette.testclient import TestClient

from thread.auth import AuthStore
from thread.brain import Brain
from thread.home import ALLOWED_ORIGINS, build_app
from thread.profile import read_profile, write_profile
from thread.server import build_online_app, build_server
from thread.workspace import Workspaces

LOCAL = {"origin": next(iter(ALLOWED_ORIGINS))}


def test_new_profile_reads_as_an_empty_form(tmp_path):
    p = read_profile(Brain(tmp_path))
    assert p["about"]["name"] == "" and p["about"]["classes"] == ""
    assert p["rules"] == [] and p["goals"] == []


def test_form_round_trips_and_keeps_frontmatter(tmp_path):
    brain = Brain(tmp_path)
    brain.set_privacy("light")
    write_profile(brain, {"about": {"name": "Jordan", "classes": "21-127, 21-241"}, "goals": ["Ship Thread", " "],
                          "style": {"length": "Short and direct", "tone": "made-up choice"},
                          "rules": ["Ask before adding calendar events"], "extra": "Learns from examples first"})
    p = read_profile(brain)
    assert p["about"]["name"] == "Jordan" and p["about"]["classes"] == "21-127, 21-241"
    assert p["goals"] == ["Ship Thread"]
    assert p["style"] == {"length": "Short and direct", "tone": "", "level": "", "feedback": ""}  # only real choices
    assert p["extra"] == "Learns from examples first"
    assert brain.privacy() == "light" and "tags: [profile]" in brain.profile()
    assert "- Name: Jordan" in brain.profile()       # still a plain note the AI can read


def test_hand_written_sections_are_kept_not_lost(tmp_path):
    brain = Brain(tmp_path)
    path = tmp_path / "me.md"
    path.write_text(path.read_text() + "\n# Favorite foods\n- dumplings\n")
    p = read_profile(brain)
    assert "Favorite foods:" in p["extra"] and "dumplings" in p["extra"]
    write_profile(brain, p)
    assert "dumplings" in read_profile(brain)["extra"]


def test_free_text_cant_start_a_new_section(tmp_path):
    brain = Brain(tmp_path)
    write_profile(brain, {"extra": "# Who I am\n- Name: Hacker"})
    assert read_profile(brain)["about"]["name"] == ""


def test_ai_sees_profile_edits_without_a_restart(tmp_path):
    brain = Brain(tmp_path)
    server = build_server(brain)
    write_profile(brain, {"about": {"name": "Jordan"}})
    assert "- Name: Jordan" in server.instructions


def test_profile_api_and_settings(tmp_path):
    brain = Brain(tmp_path)
    c = TestClient(build_app(brain))
    data = c.get("/api/profile").json()
    assert data["online"] is False and data["privacy"] == "strict" and data["schema"]["about"]
    saved = c.put("/api/profile", json={"about": {"name": "Jordan"}}, headers=LOCAL).json()
    assert saved["about"]["name"] == "Jordan"
    assert c.put("/api/profile", json={"about": {"name": "X"}}, headers={"origin": "https://evil.example"}).status_code == 403
    assert c.post("/api/privacy", json={"level": "light"}, headers=LOCAL).json() == {"privacy": "light"}
    assert c.post("/api/privacy", json={"level": "none"}, headers=LOCAL).status_code == 400
    assert c.post("/api/password", json={"current": "a", "new": "b" * 12}, headers=LOCAL).status_code == 400


def test_export_has_notes_but_not_trash(tmp_path):
    brain = Brain(tmp_path)
    brain.remember("Keep me", "hi", folder="personal")
    (tmp_path / ".trash").mkdir(exist_ok=True)
    (tmp_path / ".trash" / "gone.md").write_text("deleted")
    r = TestClient(build_app(brain)).get("/api/export")
    assert r.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "thread-memory/me.md" in names and any("keep-me" in n for n in names)
    assert not any(".trash" in n for n in names)


def test_forget_learned_removes_it(tmp_path):
    from thread.fingerprint import Fingerprint
    brain = Brain(tmp_path)
    Fingerprint(brain).learn("preferences", "Wants the blunt answer first", "prefers")
    c = TestClient(build_app(brain))
    assert c.get("/api/profile").json()["learned"][0]["text"] == "Wants the blunt answer first"
    gone = c.post("/api/forget-learned", json={"text": "Wants the blunt answer first"}, headers=LOCAL).json()
    assert c.get("/api/profile").json()["learned"] == []
    c.post("/api/restore-learned", json={"trash": gone["trash"]}, headers=LOCAL)      # Undo
    assert c.get("/api/profile").json()["learned"][0]["text"] == "Wants the blunt answer first"
    assert c.post("/api/restore-learned", json={"trash": "../../etc"}, headers=LOCAL).status_code == 400


PUBLIC = "https://thread-test.fly.dev"
OLD, NEW = "correct horse battery staple", "a brand new long password"


def test_change_password_online(tmp_path):
    store = AuthStore(tmp_path / "accounts.json")
    store.create_user("sam@andrew.cmu.edu", OLD)
    app = build_online_app(store, PUBLIC, Workspaces(tmp_path / "data"))
    other = TestClient(app, base_url=PUBLIC)       # a second browser (the app's startup only runs once)
    with TestClient(app, base_url=PUBLIC) as me:
        for c in (me, other):
            c.post("/signin", data={"email": "sam@andrew.cmu.edu", "password": OLD}, follow_redirects=False)
        h = {"origin": PUBLIC}
        assert me.get("/api/profile").json()["mcp_url"] == PUBLIC + "/mcp"
        assert me.post("/api/password", json={"current": "wrong", "new": NEW}, headers=h).status_code == 403
        assert me.post("/api/password", json={"current": OLD, "new": "short"}, headers=h).status_code == 400
        assert me.post("/api/password", json={"current": OLD, "new": NEW},
                       headers={"origin": "https://evil.example"}).status_code == 403
        assert me.post("/api/password", json={"current": OLD, "new": NEW}, headers=h).json() == {"ok": True}
        assert me.get("/api/graph").status_code == 200          # this browser stays signed in
        assert other.get("/api/graph").status_code == 401       # every other browser is signed out
        assert other.post("/signin", data={"email": "sam@andrew.cmu.edu", "password": NEW},
                          follow_redirects=False).status_code == 303


def test_old_profile_labels_migrate_cleanly(tmp_path):
    brain = Brain(tmp_path)
    (tmp_path / "me.md").write_text("---\ntags: [profile]\n---\n\n# Who I am\n- Name: Jordan\n- School / year: CMU 2030\n"
                                    "- Shoe size:\n- Favorite color: green\n")
    p = read_profile(brain)
    assert p["about"]["school"] == "CMU 2030" and p["about"]["name"] == "Jordan"
    assert p["extra"] == "Favorite color: green"      # empty old labels vanish, real info is kept


def test_templates_only_use_real_style_choices_and_fit_the_guides():
    from thread.profile import LIMITS, MAJOR_TEMPLATES, STYLE, USE_TEMPLATES
    choices = {key: options for key, _, options in STYLE}
    for t in MAJOR_TEMPLATES:
        assert set(t["style"]) == set(choices)
        assert all(value in choices[key] for key, value in t["style"].items()), t["key"]
    for t in MAJOR_TEMPLATES + USE_TEMPLATES:
        assert all(len(line) <= LIMITS["line"] for line in t["goals"] + t["rules"])
