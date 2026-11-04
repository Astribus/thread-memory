"""Saved Canvas answers (canvas_cache): reused while fresh, never across connections, never after an error."""

import asyncio
import json
import tempfile
from pathlib import Path

import httpx
import pytest

from thread.brain import Brain
from thread.canvas import CanvasAuthError, CanvasClient
from thread.canvas_cache import COURSES_SECONDS, DUE_SECONDS, SYLLABUS_SECONDS, CachedCanvas, source_key
from thread.server import build_server
from thread.workspace import Workspace

from test_syllabus import SYLLABUS_PDF


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class Fake:
    """Counts how often each Canvas question really reaches 'Canvas'."""
    source = "api"

    def __init__(self):
        self.calls = {"labels": 0, "upcoming": 0}
        self.fail = None

    def course_labels(self):
        self.calls["labels"] += 1
        if self.fail:
            raise self.fail
        return {1: "21-127 Concepts of Mathematics"}

    def upcoming(self, days=14):
        self.calls["upcoming"] += 1
        return [{"course_id": 1, "course": "21-127", "title": f"Quiz 18 ({days}d)", "type": "assignment",
                 "due": "2026-11-02T16:00:00Z", "submitted": False, "url": "u"}]


def cached(fake=None, folder=None, secret="token:a", clock=None):
    fake = fake or Fake()
    return fake, CachedCanvas(fake, Path(folder or tempfile.mkdtemp()), source_key(secret), clock or Clock())


def test_class_list_is_reused_for_a_day_then_asked_again():
    clock = Clock()
    fake, canvas = cached(clock=clock)
    assert canvas.course_labels() == {1: "21-127 Concepts of Mathematics"}   # int keys survive the JSON round trip
    canvas.course_labels()
    assert fake.calls["labels"] == 1
    clock.now += COURSES_SECONDS + 1
    canvas.course_labels()
    assert fake.calls["labels"] == 2


def test_deadlines_expire_after_five_minutes_and_each_window_is_separate():
    clock = Clock()
    fake, canvas = cached(clock=clock)
    canvas.upcoming(14)
    canvas.upcoming(14)
    assert fake.calls["upcoming"] == 1
    assert canvas.upcoming(7)[0]["title"] == "Quiz 18 (7d)"        # a different window isn't confused with 14 days
    assert fake.calls["upcoming"] == 2
    clock.now += DUE_SECONDS
    canvas.upcoming(14)
    assert fake.calls["upcoming"] == 3


def test_refresh_skips_the_saved_answer_and_saves_the_new_one():
    fake, canvas = cached()
    canvas.upcoming()
    canvas.refreshed().upcoming()
    assert fake.calls["upcoming"] == 2
    canvas.upcoming()                                               # the original isn't stuck refreshing
    assert fake.calls["upcoming"] == 2


def test_a_different_connection_never_sees_another_ones_answers():
    folder = tempfile.mkdtemp()
    fake, canvas = cached(folder=folder, secret="token:old")
    canvas.upcoming()
    fake2, other = cached(folder=folder, secret="token:new")
    other.upcoming()
    assert fake2.calls["upcoming"] == 1
    assert "token" not in "".join(p.read_text() for p in Path(folder).glob("*.json"))   # only a hash is stored


def test_errors_are_not_saved():
    fake, canvas = cached()
    fake.fail = CanvasAuthError("Invalid access token.")
    with pytest.raises(CanvasAuthError):
        canvas.course_labels()
    fake.fail = None
    assert canvas.course_labels()
    assert fake.calls["labels"] == 2


def test_a_broken_cache_file_is_just_a_miss():
    folder = tempfile.mkdtemp()
    fake, canvas = cached(folder=folder)
    canvas.upcoming()
    (Path(folder) / "due-14.json").write_text("{not json")
    canvas.upcoming()
    assert fake.calls["upcoming"] == 2


def test_cache_files_are_private():
    folder = tempfile.mkdtemp()
    _, canvas = cached(folder=folder)
    canvas.upcoming()
    assert (Path(folder) / "due-14.json").stat().st_mode & 0o777 == 0o600


def fake_canvas(counts, modified="2026-08-20T00:00:00Z"):
    """A Canvas whose 21-127 syllabus PDF (file 7) is linked from the front page, counting PDF downloads."""
    def handler(request):
        path = request.url.path
        if request.url.host == "files.test":
            counts["pdf"] += 1
            return httpx.Response(200, content=SYLLABUS_PDF, headers={"content-type": "application/pdf"})
        if path == "/api/v1/courses":
            return httpx.Response(200, json=[{"id": 1, "name": "Concepts of Mathematics", "course_code": "21-127"}])
        if path.endswith("/front_page"):
            return httpx.Response(200, json={"body": '<a href="https://canvas.test/courses/1/files/7">Syllabus</a>'})
        if path.startswith("/api/v1/files/"):
            return httpx.Response(200, json={"id": 7, "display_name": "Syllabus.pdf", "size": len(SYLLABUS_PDF),
                                             "modified_at": modified, "content-type": "application/pdf",
                                             "url": "https://files.test/dl/7"})
        if path.startswith("/api/v1/courses/1") and path.count("/") == 4:
            return httpx.Response(200, json={"id": 1, "syllabus_body": ""})
        return httpx.Response(404, json={})
    return CanvasClient("t", "https://canvas.test", transport=httpx.MockTransport(handler))


def test_syllabus_is_saved_for_a_week_and_unchanged_pdfs_are_not_reread():
    clock, counts, folder = Clock(), {"pdf": 0}, tempfile.mkdtemp()
    canvas = CachedCanvas(fake_canvas(counts), Path(folder), source_key("token:t"), clock)
    first = canvas.syllabus(1)
    assert "Oct 28th Test 4" in first["files"][0]["text"]
    canvas.syllabus(1)
    assert counts["pdf"] == 1
    clock.now += SYLLABUS_SECONDS + 1                     # a week later: look again, but the PDF didn't change
    assert canvas.syllabus(1)["files"][0]["text"] == first["files"][0]["text"]
    assert counts["pdf"] == 1
    changed = CachedCanvas(fake_canvas(counts, modified="2026-10-01T00:00:00Z"), Path(folder),
                           source_key("token:t"), clock)
    changed.refreshed().syllabus(1)                       # the professor uploaded a new version
    assert counts["pdf"] == 2


def test_workspace_uses_the_cache_and_clears_it_on_disconnect():
    root = Path(tempfile.mkdtemp())
    w = Workspace.for_account("0123456789abcdef", root)
    w.set_canvas_token("secret-token")
    canvas = w.canvas()
    assert isinstance(canvas, CachedCanvas) and getattr(canvas, "source", "api") == "api"
    folder = root / "users" / "0123456789abcdef" / "canvas-cache"
    canvas._write("due-14", [])
    assert list(folder.glob("*.json"))
    w.clear_canvas_token()
    assert not list(folder.glob("*.json"))


def test_whats_due_refresh_asks_canvas_again():
    fake, canvas = cached()
    server = build_server(Brain(tempfile.mkdtemp()), canvas)
    run = lambda args: asyncio.run(server.call_tool("whats_due", args)).content[0].text
    assert "Quiz 18" in run({})
    run({})
    assert fake.calls["upcoming"] == 1
    run({"refresh": True})
    assert fake.calls["upcoming"] == 2
