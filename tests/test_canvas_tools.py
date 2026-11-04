"""The read-only Canvas tools (canvas_search, canvas_announcements, canvas_assignment, canvas_pages/page,
canvas_modules), against a fake Canvas."""

import asyncio
import json

import httpx

from thread.brain import Brain
from thread.canvas import CanvasClient
from thread.server import build_server

COURSES = [{"id": 5, "name": "Matrices and Linear Transformations", "course_code": "21241"},
           {"id": 7, "name": "Concepts of Mathematics", "course_code": "21127"}]
ASSIGNMENTS = {5: [{"id": 51, "name": "Exam 2", "due_at": "2026-10-28T13:00:00Z", "points_possible": 100,
                    "submission_types": ["on_paper"], "description": "<p>Closed book. One <b>cheat sheet</b> allowed.</p>",
                    "rubric": [{"description": "Proofs", "points": 40, "long_description": "Full justification"}],
                    "submission": {"workflow_state": "unsubmitted"}, "html_url": "https://canvas.test/courses/5/assignments/51"},
                   {"id": 52, "name": "HW 8", "due_at": "2026-10-22T03:59:00Z", "points_possible": 10,
                    "submission_types": ["online_upload"], "description": "Problems 1-5",
                    "submission": {"workflow_state": "submitted", "submitted_at": "2026-10-20T10:00:00Z"},
                    "html_url": "https://canvas.test/courses/5/assignments/52"}],
               7: []}
PAGES = {5: [{"url": "exam-policy", "title": "Exam policy", "body": "<p>Bring your CMU ID. A cheat sheet is fine.</p>",
              "updated_at": "2026-10-01T12:00:00Z", "html_url": "https://canvas.test/courses/5/pages/exam-policy"}]}
ANNOUNCEMENTS = [{"context_code": "course_7", "title": "Test 4 room change", "message": "<p>Test 4 is in DH 2210.</p>",
                  "posted_at": "2026-10-20T15:00:00Z", "html_url": "https://canvas.test/courses/7/discussion_topics/9"}]
MODULES = {5: [{"name": "Week 9", "items": [{"title": "Lecture 18 slides", "type": "File", "html_url": "https://canvas.test/f/1"},
                                            {"title": "Exam 2", "type": "Assignment"}]}]}


def fake_canvas(hide_pages: bool = False, bad_token: bool = False):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("/api/v1", "")
        if bad_token:
            return httpx.Response(401, json={"errors": [{"message": "Invalid access token."}]})
        if request.method != "GET":
            raise AssertionError(f"read-only tools must never {request.method} {path}")
        if path == "/courses":
            return httpx.Response(200, json=COURSES)
        if path.startswith("/courses/") and path.count("/") == 2:
            return httpx.Response(200, json={"syllabus_body": "<p>Exams are closed book.</p>" if path.endswith("/5") else ""})
        cid = int(path.split("/")[2]) if path.startswith("/courses/") else None
        if path.endswith("/assignments"):
            term = request.url.params.get("search_term", "").lower()
            return httpx.Response(200, json=[a for a in ASSIGNMENTS[cid] if term in a["name"].lower()])
        if "/assignments/" in path:
            aid = int(path.rsplit("/", 1)[1])
            hit = [a for a in ASSIGNMENTS[cid] if a["id"] == aid]
            return httpx.Response(200, json=hit[0]) if hit else httpx.Response(404, json={})
        if path.endswith("/pages"):
            return httpx.Response(403, json={}) if hide_pages else httpx.Response(200, json=PAGES.get(cid, []))
        if "/pages/" in path:
            url = path.rsplit("/", 1)[1]
            hit = [p for p in PAGES.get(cid, []) if p["url"] == url]
            return httpx.Response(200, json=hit[0]) if hit else httpx.Response(404, json={})
        if path.endswith("/modules"):
            return httpx.Response(200, json=MODULES.get(cid, []))
        if path == "/announcements":
            wanted = set(request.url.params.get_list("context_codes[]"))
            return httpx.Response(200, json=[a for a in ANNOUNCEMENTS if a["context_code"] in wanted])
        return httpx.Response(404, json={})
    return CanvasClient("t", "https://canvas.test", transport=httpx.MockTransport(handler))


def call(server, tool, **args):
    return str(asyncio.run(server.call_tool(tool, args)))


def test_search_finds_matches_across_syllabus_pages_assignments_and_announcements(tmp_path):
    server = build_server(Brain(tmp_path), fake_canvas())
    out = call(server, "canvas_search", query="cheat sheet")
    assert "Exam 2" in out and "Exam policy" in out and "assignment" in out and "page" in out
    assert out.index("Exam") < out.index("Nothing") if "Nothing" in out else True
    room = call(server, "canvas_search", query="room change")
    assert "Test 4 room change" in room and "announcement" in room and "Concepts of Mathematics" in room
    assert "Nothing on Canvas" in call(server, "canvas_search", query="zebra")


def test_assignment_details_with_rubric_due_date_and_submitted_state(tmp_path):
    server = build_server(Brain(tmp_path), fake_canvas())
    out = call(server, "canvas_assignment", course="21-241", assignment="exam 2")
    assert "Closed book" in out and "cheat sheet" in out and "Proofs (40 pts): Full justification" in out
    assert "Not submitted yet" in out and "Points: 100" in out and "on_paper" in out
    assert "Already submitted" in call(server, "canvas_assignment", course="21241", assignment="52")
    assert "No assignment" in call(server, "canvas_assignment", course="21241", assignment="lab 3")
    assert "No class matches" in call(server, "canvas_assignment", course="15-122", assignment="x")


def test_pages_page_modules_and_announcements(tmp_path):
    server = build_server(Brain(tmp_path), fake_canvas())
    assert "Exam policy (page: exam-policy)" in call(server, "canvas_pages", course="21-241")
    page = call(server, "canvas_page", course="21-241", page="policy")
    assert "Bring your CMU ID" in page and "exam-policy" in page
    assert "Lecture 18 slides [File]" in call(server, "canvas_modules", course="21-241")
    assert "DH 2210" in call(server, "canvas_announcements", course="")
    assert "No announcements" in call(server, "canvas_announcements", course="21-241")


def test_hidden_pages_and_bad_token_are_explained_not_crashes(tmp_path):
    hidden = build_server(Brain(tmp_path), fake_canvas(hide_pages=True))
    assert "no pages you can see" in call(hidden, "canvas_pages", course="21-241")
    bad = build_server(Brain(tmp_path), fake_canvas(bad_token=True))
    assert "rejected" in call(bad, "canvas_search", query="exam").lower()


def test_feed_only_users_are_told_they_need_a_token(tmp_path):
    from thread.canvas_feed import FeedCanvas
    server = build_server(Brain(tmp_path), FeedCanvas("https://canvas.test/feeds/calendars/user_abc.ics"))
    for tool, args in [("canvas_search", {"query": "exam"}), ("canvas_modules", {"course": "21241"})]:
        out = call(server, tool, **args)
        assert "needs a Canvas access token" in out and "Advanced" in out


def test_no_canvas_tool_can_write(tmp_path):
    # every tool runs against a fake Canvas that fails the test on any non-GET request
    server = build_server(Brain(tmp_path), fake_canvas())
    for tool, args in [("canvas_search", {"query": "exam"}), ("canvas_announcements", {}),
                       ("canvas_assignment", {"course": "21241", "assignment": "Exam 2"}),
                       ("canvas_pages", {"course": "21241"}), ("canvas_page", {"course": "21241", "page": "exam-policy"}),
                       ("canvas_modules", {"course": "21241"})]:
        assert "must never" not in call(server, tool, **args)
