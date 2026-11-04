"""Secrets at rest (thread.sealed): Canvas tokens, Canvas feed links and Google sign-ins are encrypted online."""

import json

import pytest
from cryptography.fernet import Fernet

from thread.sealed import PREFIX, KEY_ENV, SecretKeyError, Sealer, read_secret, write_secret
from thread.workspace import Workspaces

TOKEN = "7752~abcdefghijklmnopqrstuvwxyz0123456789"   # fake token in Canvas's format


def key():
    return Fernet.generate_key().decode()


def test_roundtrip_and_file_never_holds_the_plain_secret(tmp_path):
    s = Sealer(key())
    path = tmp_path / "canvas-token"
    write_secret(path, TOKEN, s)
    raw = path.read_text()
    assert raw.startswith(PREFIX) and TOKEN not in raw
    assert oct(path.stat().st_mode)[-3:] == "600"
    assert read_secret(path, s) == TOKEN


def test_old_plain_file_is_upgraded_on_first_read(tmp_path):
    s = Sealer(key())
    path = tmp_path / "canvas-token"
    path.write_text(TOKEN + "\n")   # legacy plaintext file
    assert read_secret(path, s) == TOKEN
    assert path.read_text().startswith(PREFIX) and TOKEN not in path.read_text()
    assert read_secret(path, s) == TOKEN   # still readable


def test_wrong_or_missing_key_fails_loudly_instead_of_returning_junk(tmp_path):
    path = tmp_path / "canvas-token"
    write_secret(path, TOKEN, Sealer(key()))
    with pytest.raises(SecretKeyError):
        read_secret(path, Sealer(key()))              # a different key
    with pytest.raises(SecretKeyError):
        read_secret(path, None)                       # no key at all
    with pytest.raises(SecretKeyError):
        Sealer("not-a-real-key")


def test_laptop_mode_is_unchanged_plain_text(tmp_path):
    path = tmp_path / "canvas-token"
    write_secret(path, TOKEN, None)
    assert path.read_text() == TOKEN and read_secret(path, None) == TOKEN
    assert read_secret(tmp_path / "missing", None) is None


def test_each_account_seals_its_own_secrets(tmp_path):
    ws = Workspaces(tmp_path, sealer=Sealer(key()))
    a, b = ws.get("a" * 16), ws.get("b" * 16)
    a.set_canvas_token(TOKEN)
    b.set_canvas_feed("https://canvas.test/feeds/calendars/user_secret.ics")
    assert a.canvas_token() == TOKEN and b.canvas_token() is None
    assert b.canvas_feed().endswith("user_secret.ics") and a.canvas_feed() is None
    for f in (tmp_path / "users").rglob("canvas-*"):
        assert f.read_text().startswith(PREFIX) and "user_secret" not in f.read_text() and TOKEN not in f.read_text()


def test_google_sign_in_is_sealed_and_still_loads(tmp_path):
    from thread import gcal
    s = Sealer(key())
    token_file = tmp_path / "google-token.json"
    creds = {"token": "ya29.fake", "refresh_token": "1//fake", "client_id": "x.apps.googleusercontent.com",
             "client_secret": "fake", "token_uri": "https://oauth2.googleapis.com/token",
             "scopes": gcal.SCOPES, "expiry": "2099-01-01T00:00:00Z"}
    write_secret(token_file, json.dumps(creds), s)
    assert "ya29.fake" not in token_file.read_text()
    loaded = gcal.load_credentials(token_file, s)
    assert loaded.token == "ya29.fake" and loaded.refresh_token == "1//fake"


def test_online_server_refuses_to_start_without_a_key(monkeypatch):
    from thread import server
    monkeypatch.delenv(KEY_ENV, raising=False)
    with pytest.raises(SystemExit) as e:
        server.main_online()
    assert KEY_ENV in str(e.value)
