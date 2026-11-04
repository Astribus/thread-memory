"""Chat backup: kept 10 days from first save, merged by message id, private per account, searchable by the AI."""

import time

import pytest
from starlette.testclient import TestClient

from thread.auth import AuthStore
from thread.chats import ChatBackup, as_markdown
from thread.server import build_online_app
from thread.workspace import Workspaces

PUBLIC = "https://thread-test.fly.dev"
SAM, ALEX, PASSWORD = "sam@andrew.cmu.edu", "alex@andrew.cmu.edu", "correct horse battery staple"
DAY = 86400


def chat(messages, chat_id="6ac70220-98fc-83ea-bf19-ab3efe69ec42", source="chatgpt", title="Pointers help"):
    return {"source": source, "chat_id": chat_id, "title": title, "url": f"https://chatgpt.com/c/{chat_id}",
            "messages": [{"id": i, "role": r, "text": t} for i, r, t in messages]}


def test_resending_a_chat_adds_only_new_messages_and_keeps_scrolled_away_ones(tmp_path):
    box = ChatBackup(tmp_path)
    box.save(chat([("m1", "user", "what is a pointer"), ("m2", "assistant", "an address in memory")]))
    box.save(chat([("m2", "assistant", "an address in memory"), ("m3", "user", "ok thanks")]))   # m1 scrolled away
    saved = box.get("chatgpt-6ac70220-98fc-83ea-bf19-ab3efe69ec42")
    assert [m["id"] for m in saved["messages"]] == ["m1", "m2", "m3"]


def test_a_chat_is_deleted_10_days_after_it_was_first_saved(tmp_path):
    box = ChatBackup(tmp_path)
    start = time.time() - 9.5 * DAY
    box.save(chat([("m1", "user", "hi there")]), now=start)
    box.save(chat([("m2", "user", "still going")]), now=time.time())     # still being used: clock doesn't reset
    assert len(box.overview()) == 1 and box.overview()[0]["deletes_in_days"] == 0.5
    assert box.overview(now=start + 10 * DAY + 1) == []
    assert not list(tmp_path.glob("*.json"))                              # the file itself is gone


def test_bad_chats_are_refused(tmp_path):
    box = ChatBackup(tmp_path)
    for bad in (chat([("m", "user", "x")], chat_id="../../etc"), chat([("m", "user", "x")], source="gemini")):
        with pytest.raises(ValueError):
            box.save(bad)
    saved = box.save(chat([("m1", "system", "ignored"), ("m2", "user", "  "), ("m3", "user", "real")]))
    assert saved["messages"] == 1


def test_search_finds_the_chat_and_the_lines(tmp_path):
    box = ChatBackup(tmp_path)
    box.save(chat([("m1", "user", "how do linked lists work"), ("m2", "assistant", "each node points to the next")]))
    box.save(chat([("x1", "user", "write me a poem")], chat_id="5ad5d0c1-1a40-4894-9dca-ab4b717e03f5", source="claude",
                  title="Poem"))
    hits = box.search("linked lists")
    assert len(hits) == 1 and hits[0]["app"] == "ChatGPT" and "linked lists" in hits[0]["matches"][0]
    text = as_markdown(box.get(hits[0]["chat"]))
    assert "**You:**" in text and "each node points to the next" in text


@pytest.fixture
def online(tmp_path):
    store = AuthStore(tmp_path / "accounts.json")
    workspaces = Workspaces(tmp_path / "data")
    store.create_user(SAM, PASSWORD)
    store.create_user(ALEX, PASSWORD)
    app = build_online_app(store, PUBLIC, workspaces)
    with TestClient(app, base_url=PUBLIC) as client:
        client.store, client.workspaces, client.app_ = store, workspaces, app
        yield client


def test_extension_upload_needs_the_right_key_and_lands_in_that_person_s_folder(online):
    online.post("/signin", data={"email": SAM, "password": PASSWORD})
    key = online.post("/api/backup-key", json={}, headers={"origin": PUBLIC}).json()["key"]
    assert key not in online.store.path.read_text()                       # stored only as a hash
    extension = TestClient(online.app_, base_url=PUBLIC)                  # no cookies: just the key
    body = chat([("m1", "user", "my secret plan is jazz")])
    assert extension.post("/backup/chat", json=body).status_code == 401
    assert extension.post("/backup/chat", json=body, headers={"authorization": "Bearer thr_wrong"}).status_code == 401
    r = extension.post("/backup/chat", json=body, headers={"authorization": f"Bearer {key}"})
    assert r.status_code == 200 and r.json()["messages"] == 1
    assert online.get("/api/chats").json()["chats"][0]["title"] == "Pointers help"
    alex = TestClient(online.app_, base_url=PUBLIC)
    alex.post("/signin", data={"email": ALEX, "password": PASSWORD})
    assert alex.get("/api/chats").json()["chats"] == []                   # Alex never sees Sam's chats
    sam_key = online.get("/api/chats").json()["chats"][0]["key"]
    assert alex.get(f"/api/chats/{sam_key}").status_code == 404
    md = online.get(f"/api/chats/{sam_key}", params={"format": "md"})
    assert "my secret plan is jazz" in md.text and "attachment" in md.headers["content-disposition"]


def test_a_new_key_replaces_the_old_one_and_a_new_password_turns_it_off(online):
    online.post("/signin", data={"email": SAM, "password": PASSWORD})
    first = online.post("/api/backup-key", json={}, headers={"origin": PUBLIC}).json()["key"]
    second = online.post("/api/backup-key", json={}, headers={"origin": PUBLIC}).json()["key"]
    assert online.store.backup_user(first) is None and online.store.backup_user(second)
    online.store.set_password(online.store.user_id(SAM), "a brand new long password")
    assert online.store.backup_user(second) is None


def test_upload_size_is_capped(online):
    online.post("/signin", data={"email": SAM, "password": PASSWORD})
    key = online.post("/api/backup-key", json={}, headers={"origin": PUBLIC}).json()["key"]
    huge = chat([("m1", "user", "x" * (6 * 1024 * 1024))])
    r = TestClient(online.app_, base_url=PUBLIC).post("/backup/chat", json=huge, headers={"authorization": f"Bearer {key}"})
    assert r.status_code == 413


def test_search_and_list_read_one_chat_at_a_time(tmp_path):
    # 300 long chats (~45 MB) must never be loaded together.
    import tracemalloc
    from thread.chats import ChatBackup
    backup = ChatBackup(tmp_path)
    filler = "lorem ipsum " * 1250                       # ~15KB per message
    for i in range(300):
        backup.save({"source": "claude", "chat_id": f"chat-{i:04d}", "title": f"Chat {i}",
                     "messages": [{"id": f"m{j}", "role": "user" if j % 2 else "assistant",
                                   "text": (f"needle-{i} " if j == 0 else "") + filler} for j in range(10)]})
    tracemalloc.start()
    hits = backup.search("needle-123")
    listed = backup.overview()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert hits and hits[0]["chat"] == "claude-chat-0123"
    assert len(listed) == 300
    assert peak < 8 * 1024 * 1024, f"peak {peak / 1e6:.1f}MB"   # streamed, one chat at a time
