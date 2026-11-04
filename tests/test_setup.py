"""First-run setup: server-derived completion per account and saved progress."""

import time

import pytest
from starlette.testclient import TestClient

from thread.auth import AuthStore
from thread.server import build_online_app
from thread.workspace import Workspaces

PUBLIC = "https://thread-test.fly.dev"
SAM, ALEX = "sam@andrew.cmu.edu", "alex@andrew.cmu.edu"
PASSWORD = "correct horse battery staple"
H = {"origin": PUBLIC}


@pytest.fixture
def online(tmp_path):
    store = AuthStore(tmp_path / "accounts.json")
    workspaces = Workspaces(tmp_path / "data")
    store.create_user(SAM, PASSWORD)
    app = build_online_app(store, PUBLIC, workspaces)
    with TestClient(app, base_url=PUBLIC) as client:
        client.store, client.app_ = store, app
        yield client


def sign_in(client, email=SAM):
    return client.post("/signin", data={"email": email, "password": PASSWORD}, follow_redirects=False)


def connect_ai(store, user, name, client_id, expires_in=3600):
    """What the OAuth flow leaves behind when an AI app signs in: a registered client and a refresh token."""
    store.data["clients"][client_id] = {"client_id": client_id, "client_name": name}
    store.data["refresh"][f"hash-{client_id}"] = {"user": user, "client_id": client_id, "scopes": [],
                                                  "expires_at": int(time.time()) + expires_in}
    store.save()


def test_new_account_starts_with_nothing_done(online):
    sign_in(online)
    s = online.get("/api/setup").json()
    assert s["online"] and not s["seen"] and s["skipped"] == [] and s["ai_apps"] == []
    assert s["done"] == {"about": False, "ai": False, "canvas": False, "backup": False}


def test_connected_ai_and_profile_count_as_done_and_stay_per_account(online):
    sign_in(online)
    sam, alex = online.store.user_id(SAM), online.store.create_user(ALEX, PASSWORD)
    connect_ai(online.store, sam, "Claude", "claude-web")
    connect_ai(online.store, sam, "claude.ai", "claude-phone")              # same app twice → listed once
    connect_ai(online.store, alex, "ChatGPT", "gpt", expires_in=-10)         # Alex's sign-in has expired
    profile = online.get("/api/profile").json()["profile"]
    profile["about"]["name"] = "Sam"
    assert online.put("/api/profile", json=profile, headers=H).status_code == 200
    s = online.get("/api/setup").json()
    assert s["ai_apps"] == ["Claude"] and s["done"]["ai"] and s["done"]["about"]
    other = TestClient(online.app_, base_url=PUBLIC)
    sign_in(other, ALEX)
    a = other.get("/api/setup").json()
    assert a["ai_apps"] == [] and not a["done"]["ai"] and not a["done"]["about"]   # never sees Sam's


def test_progress_is_saved_and_checked(online):
    sign_in(online)
    assert online.post("/api/setup", json={"seen": True, "skipped": "canvas"}, headers=H).json()["skipped"] == ["canvas"]
    assert online.post("/api/setup", json={"skipped": "ai"}, headers=H).json()["skipped"] == ["canvas", "ai"]
    s = online.post("/api/setup", json={"unskip": "canvas"}, headers=H).json()
    assert s["seen"] and s["skipped"] == ["ai"]
    assert online.get("/api/setup").json()["skipped"] == ["ai"]              # survives a reload
    assert online.post("/api/setup", json={"skipped": "wifi"}, headers=H).status_code == 400
    assert online.post("/api/setup", json={"seen": True}, headers={"origin": "https://evil.example"}).status_code == 403


def test_signed_out_cant_read_setup(online):
    assert online.get("/api/setup").status_code == 401
