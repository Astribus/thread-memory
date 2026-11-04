"""End-to-end OAuth sign-in as an MCP client performs it, plus the rejection paths."""

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from starlette.testclient import TestClient

from thread.auth import AuthStore, ThreadAuthProvider, check_password, hash_password
from thread.workspace import Workspaces
from thread.server import build_server

BASE = "http://127.0.0.1:8770"
PASSWORD = "correct horse battery staple"
REDIRECT = "http://localhost:9999/callback"


SAM, ALEX = "sam@andrew.cmu.edu", "alex@andrew.cmu.edu"


@pytest.fixture
def app(tmp_path):
    store = AuthStore(tmp_path / "accounts.json")
    store.create_user(SAM, PASSWORD)
    store.create_user(ALEX, PASSWORD)
    provider = ThreadAuthProvider(store, BASE)
    settings = AuthSettings(issuer_url=BASE, resource_server_url=f"{BASE}/mcp",
                            client_registration_options=ClientRegistrationOptions(enabled=True),
                            revocation_options=RevocationOptions(enabled=True),
                            validate_token_resource=True)
    workspaces = Workspaces(tmp_path / "data")
    server = build_server(auth=(provider, settings), workspaces=workspaces)
    with TestClient(server.streamable_http_app(), base_url=BASE) as client:
        client.provider, client.store, client.workspaces = provider, store, workspaces
        yield client


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def sign_in(client, password=PASSWORD, email=SAM):
    """Register, authorize, log in, and exchange the code. Returns (client_id, tokens or the login response)."""
    reg = client.post("/register", json={"redirect_uris": [REDIRECT], "client_name": "Claude",
                                         "token_endpoint_auth_method": "none",
                                         "grant_types": ["authorization_code", "refresh_token"],
                                         "response_types": ["code"]})
    assert reg.status_code == 201, reg.text
    client_id = reg.json()["client_id"]
    verifier, challenge = pkce()
    auth = client.get("/authorize", params={"response_type": "code", "client_id": client_id,
                                            "redirect_uri": REDIRECT, "code_challenge": challenge,
                                            "code_challenge_method": "S256", "state": "xyz"},
                      follow_redirects=False)
    assert auth.status_code == 302, auth.text
    login_url = urlparse(auth.headers["location"])
    assert login_url.path == "/login"
    request_id = parse_qs(login_url.query)["request"][0]
    assert "Claude" in client.get("/login", params={"request": request_id}).text
    done = client.post("/login", data={"request": request_id, "email": email, "password": password},
                       follow_redirects=False)
    if done.status_code != 302:
        return client_id, done
    back = urlparse(done.headers["location"])
    assert back.geturl().startswith(REDIRECT)
    query = parse_qs(back.query)
    assert query["state"] == ["xyz"]
    code = query["code"][0]
    tokens = client.post("/token", data={"grant_type": "authorization_code", "code": code, "client_id": client_id,
                                         "redirect_uri": REDIRECT, "code_verifier": verifier})
    assert tokens.status_code == 200, tokens.text
    return client_id, {**tokens.json(), "_code": code, "_verifier": verifier}


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def test_password_is_stored_only_as_a_hash(app):
    stored = app.store.data["users"][app.store.user_id(SAM)]["password"]
    assert PASSWORD not in stored and stored.startswith("scrypt$")
    assert check_password(PASSWORD, stored) and not check_password("wrong", stored)
    assert hash_password(PASSWORD) != hash_password(PASSWORD)   # salted


def test_no_token_is_refused(app):
    r = app.post("/mcp", json=INIT, headers=HEADERS)
    assert r.status_code == 401
    assert "resource_metadata" in r.headers.get("www-authenticate", "")   # tells apps where to sign in


def test_full_sign_in_then_use_thread(app):
    _, tokens = sign_in(app)
    r = app.post("/mcp", json=INIT, headers={**HEADERS, "authorization": f"Bearer {tokens['access_token']}"})
    assert r.status_code == 200, r.text
    assert "thread" in r.text.lower()
    # tokens are saved hashed, never in plain text
    raw = app.store.path.read_text()
    assert tokens["access_token"] not in raw and tokens["refresh_token"] not in raw


def test_wrong_password_gets_no_code(app):
    _, response = sign_in(app, password="nope")
    assert response.status_code == 401 and "Wrong email or password" in response.text


def test_a_code_works_only_once(app):
    client_id, tokens = sign_in(app)
    again = app.post("/token", data={"grant_type": "authorization_code", "code": tokens["_code"],
                                     "client_id": client_id, "redirect_uri": REDIRECT,
                                     "code_verifier": tokens["_verifier"]})
    assert again.status_code == 400


def test_refresh_rotates_and_old_tokens_stop_working(app):
    client_id, tokens = sign_in(app)
    new = app.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                                   "client_id": client_id})
    assert new.status_code == 200, new.text
    old_access = app.post("/mcp", json=INIT, headers={**HEADERS, "authorization": f"Bearer {tokens['access_token']}"})
    assert old_access.status_code == 401
    reuse = app.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                                     "client_id": client_id})
    assert reuse.status_code == 400


def test_lockout_after_five_wrong_passwords(app):
    for _ in range(5):
        assert sign_in(app, password="nope")[1].status_code == 401
    _, response = sign_in(app, password=PASSWORD)    # even the right password is refused while locked
    assert response.status_code == 429
    _, tokens = sign_in(app, email=ALEX)              # other accounts aren't locked
    assert "access_token" in tokens


def test_new_password_signs_that_person_s_apps_out(app):
    _, sam = sign_in(app)
    _, alex = sign_in(app, email=ALEX)
    app.store.set_password(app.store.user_id(SAM), "a brand new long password")
    r = app.post("/mcp", json=INIT, headers={**HEADERS, "authorization": f"Bearer {sam['access_token']}"})
    assert r.status_code == 401
    r = app.post("/mcp", json=INIT, headers={**HEADERS, "authorization": f"Bearer {alex['access_token']}"})
    assert r.status_code == 200


def test_tokens_last_one_hour_and_refresh_lasts_14_days(app):
    import time
    _, tokens = sign_in(app)
    assert tokens["expires_in"] == 3600
    refresh = next(iter(app.store.data["refresh"].values()))
    assert abs(refresh["expires_at"] - (time.time() + 14 * 24 * 3600)) < 60


def open_chat(app, tokens):
    """Start an MCP chat the way the Claude app does. Returns (headers for later calls, initialize reply)."""
    auth = {**HEADERS, "authorization": f"Bearer {tokens['access_token']}"}
    r = app.post("/mcp", json=INIT, headers=auth)
    assert r.status_code == 200, r.text
    auth["mcp-session-id"] = r.headers["mcp-session-id"]
    app.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=auth)
    return auth, r.text


def call(app, auth, tool, args):
    r = app.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                               "params": {"name": tool, "arguments": args}}, headers=auth)
    assert r.status_code == 200, r.text
    return r.text


def test_each_ai_connection_uses_only_its_own_person_s_memory(app):
    sam_ws = app.workspaces.get(app.store.user_id(SAM))
    alex_ws = app.workspaces.get(app.store.user_id(ALEX))
    (sam_ws.brain.root / "me.md").write_text("---\ntags: [profile]\n---\n\n# Who I am\n- Name: Sam Rivera\n")
    (alex_ws.brain.root / "me.md").write_text("---\ntags: [profile]\n---\n\n# Who I am\n- Name: Alex Kim\n")
    _, sam = sign_in(app)
    _, alex = sign_in(app, email=ALEX)
    sam_chat, sam_init = open_chat(app, sam)
    alex_chat, alex_init = open_chat(app, alex)
    # each chat starts with its own person's profile
    assert "Sam Rivera" in sam_init and "Alex Kim" not in sam_init
    assert "Alex Kim" in alex_init and "Sam Rivera" not in alex_init
    # what Sam's AI saves goes to Sam's folder; Alex's AI can't find it
    call(app, sam_chat, "remember", {"title": "Sam secret", "content": "likes jazz", "folder": "personal"})
    assert sam_ws.brain.search("jazz") and not alex_ws.brain.search("jazz")
    assert "sam-secret" not in call(app, alex_chat, "search_memory", {"query": "jazz"})
    assert "sam-secret" in call(app, sam_chat, "search_memory", {"query": "jazz"})


def test_a_chat_can_t_be_borrowed_with_someone_else_s_token(app):
    sam_ws = app.workspaces.get(app.store.user_id(SAM))
    _, sam = sign_in(app)
    _, alex = sign_in(app, email=ALEX)
    sam_chat, _ = open_chat(app, sam)
    stolen = {**sam_chat, "authorization": f"Bearer {alex['access_token']}"}   # Alex reuses Sam's chat id
    r = app.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                               "params": {"name": "remember", "arguments": {"title": "x", "content": "planted"}}},
                 headers=stolen)
    assert not sam_ws.brain.search("planted")


def test_token_for_another_service_is_refused(app):
    _, tokens = sign_in(app)
    raw = app.store.data["access"]
    for entry in raw.values():
        entry["resource"] = "https://some-other-service.example/mcp"
    r = app.post("/mcp", json=INIT, headers={**HEADERS, "authorization": f"Bearer {tokens['access_token']}"})
    assert r.status_code == 401


def test_config_dir_setting_is_used_by_calendar_and_canvas_is_safe_off_mac(monkeypatch, tmp_path):
    import importlib
    import subprocess

    monkeypatch.setenv("THREAD_CONFIG_DIR", str(tmp_path / "cfg"))
    import thread.gcal as gcal
    importlib.reload(gcal)
    assert gcal.CONFIG_DIR == tmp_path / "cfg" and gcal.TOKEN_FILE.parent == tmp_path / "cfg"

    import thread.canvas as canvas
    monkeypatch.delenv("CANVAS_TOKEN", raising=False)

    def no_security(*args, **kwargs):
        raise FileNotFoundError("security")
    monkeypatch.setattr(subprocess, "run", no_security)
    assert canvas.load_token() is None




def test_the_ai_can_search_only_its_own_person_s_chat_backups(app):
    sam_ws = app.workspaces.get(app.store.user_id(SAM))
    sam_ws.chats.save({"source": "claude", "chat_id": "5ad5d0c1-1a40-4894-9dca-ab4b717e03f5", "title": "Deleted chat",
                       "messages": [{"id": "1", "role": "user", "text": "my startup idea is a memory you own"}]})
    _, sam = sign_in(app)
    _, alex = sign_in(app, email=ALEX)
    sam_chat, _ = open_chat(app, sam)
    alex_chat, _ = open_chat(app, alex)
    assert "Deleted chat" in call(app, sam_chat, "search_chats", {"query": "startup idea"})
    assert "Deleted chat" not in call(app, alex_chat, "search_chats", {"query": "startup idea"})
    assert "memory you own" in call(app, sam_chat, "read_chat", {"chat": "claude-5ad5d0c1-1a40-4894-9dca-ab4b717e03f5"})
