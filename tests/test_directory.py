"""App-directory requirements: tool annotations, public privacy/terms/support pages, and OAuth redirect rules."""

import asyncio
import hashlib
import base64
import secrets

import pytest
from starlette.testclient import TestClient

from thread.auth import AuthStore
from thread.brain import Brain
from thread.server import build_online_app, build_server
from thread.workspace import Workspaces

PUBLIC = "https://thread-test.fly.dev"


def tools(tmp_path):
    return asyncio.run(build_server(Brain(tmp_path)).list_tools())


def test_every_tool_has_a_title_and_all_three_hints(tmp_path):
    listed = tools(tmp_path)
    assert len(listed) >= 19
    for t in listed:
        a = t.annotations
        assert t.title and a and a.title == t.title, t.name
        assert isinstance(a.read_only_hint, bool) and isinstance(a.destructive_hint, bool), t.name
        assert a.open_world_hint is False, t.name            # only the user's own memory / own accounts
        assert len(t.name) <= 64 and t.description
        assert not (a.read_only_hint and a.destructive_hint), t.name


def test_hints_match_what_each_tool_does(tmp_path):
    by = {t.name: t.annotations for t in tools(tmp_path)}
    for read in ("search_memory", "read_note", "list_memory", "whats_due", "read_syllabus", "calendar_upcoming",
                 "search_chats", "read_chat", "how_user_works"):
        assert by[read].read_only_hint is True, read
    for destructive in ("forget_about_user", "sync_canvas", "report_bug"):   # deletes, overwrites, or sends out
        assert by[destructive].destructive_hint is True and by[destructive].read_only_hint is False, destructive
    for write in ("remember", "link_notes", "learn_about_user", "note_user_voice", "add_to_calendar", "set_saving"):
        assert by[write].read_only_hint is False and by[write].destructive_hint is False, write


def test_tool_descriptions_describe_not_direct(tmp_path):
    # directory rule: descriptions say what a tool does, not how the model should behave
    for t in tools(tmp_path):
        d = t.description.lower()
        assert "thank the user" not in d and "only after they say yes" not in d, t.name


@pytest.fixture
def online(tmp_path):
    app = build_online_app(AuthStore(tmp_path / "accounts.json"), PUBLIC, Workspaces(tmp_path / "data"))
    with TestClient(app, base_url=PUBLIC) as client:
        yield client


def test_privacy_terms_support_are_public_and_honest(online):
    for path, must in [("/privacy", ["never sells", "10 days", "Fly.io", "scrypt", "DRAFT"]),
                       ("/terms", ["invite", "DRAFT", "as is"]),
                       ("/support", ["Report a bug", "Security"])]:
        r = online.get(path, follow_redirects=False)
        assert r.status_code == 200, path
        for word in must:
            assert word.lower() in r.text.lower(), (path, word)
    txt = online.get("/.well-known/security.txt")
    assert txt.status_code == 200 and f"Contact: {PUBLIC}/support" in txt.text
    assert 'href="/privacy"' in online.get("/signin").text


def _pkce():
    v = secrets.token_urlsafe(40)
    return base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()


def test_claude_redirects_are_accepted_and_others_are_not(online):
    reg = lambda uris: online.post("/register", json={"client_name": "x", "redirect_uris": uris,
        "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"]}).json()["client_id"]
    web = reg(["https://claude.ai/api/mcp/auth_callback"])
    code = reg(["http://localhost/callback", "http://127.0.0.1/callback"])
    auth = lambda cid, uri: online.get("/authorize", params={"response_type": "code", "client_id": cid,
        "redirect_uri": uri, "code_challenge": _pkce(), "code_challenge_method": "S256", "state": "s",
        "resource": PUBLIC + "/mcp"}, follow_redirects=False).status_code
    assert auth(web, "https://claude.ai/api/mcp/auth_callback") < 400
    assert auth(code, "http://localhost:53122/callback") < 400   # native clients: any loopback port
    assert auth(code, "http://127.0.0.1:61000/callback") < 400
    assert auth(code, "http://localhost:53122/other") >= 400            # path must still match
    assert auth(code, "http://evil.example:53122/callback") >= 400      # only loopback hosts get the port pass
    assert auth(web, "https://claude.ai/api/mcp/auth_callback/x") >= 400
