import json

import httpx

from thread.brain import Brain
from thread.canvas import CanvasClient

PLANNER_PAGE_1 = [
    {"plannable_type": "assignment", "context_name": "Concepts of Mathematics", "course_id": 1,
     "plannable": {"title": "Quiz 12", "due_at": "2026-10-01T16:00:00Z"},
     "submissions": {"submitted": False}, "html_url": "/courses/1/assignments/9"},
    {"plannable_type": "announcement", "context_name": "Concepts of Mathematics", "course_id": 1,
     "plannable": {"title": "Office hours moved"}, "html_url": "/courses/1/announcements/3"},
]
PLANNER_PAGE_2 = [
    {"plannable_type": "quiz", "context_name": "Reasoning with Data", "course_id": 2,
     "plannable": {"title": "Exam 1", "due_at": "2026-09-28T13:00:00Z"},
     "submissions": False, "html_url": "/courses/2/quizzes/4"},
]
COURSES = [{"id": 1, "name": "Concepts of Mathematics", "course_code": "21-127"},
           {"id": 2, "name": "Reasoning with Data", "course_code": "36-200"},
           {"id": 3, "name": "Safer Scotties Anti-Hazing Training 2026", "course_code": "SAFER"},
           {"id": 4, "name": "99101-DC Core@CMU F26", "course_code": "99101-DC F26"}]


def test_course_code_not_repeated_when_already_in_name():
    assert fake_canvas().course_labels()[4] == "99101-DC Core@CMU F26"


def fake_canvas(status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        if status == 401:
            return httpx.Response(401, json={"errors": [{"message": "Expired access token."}]})
        assert request.headers["Authorization"] == "Bearer test-token"
        if request.url.path.endswith("/courses"):
            return httpx.Response(200, json=COURSES)
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=PLANNER_PAGE_2)
        next_link = '<https://canvas.test/api/v1/planner/items?page=2>; rel="next"'
        return httpx.Response(200, json=PLANNER_PAGE_1, headers={"Link": next_link})
    return CanvasClient("test-token", "https://canvas.test", transport=httpx.MockTransport(handler))


def test_upcoming_follows_pages_skips_announcements_and_sorts():
    items = fake_canvas().upcoming()
    assert [i["title"] for i in items] == ["Exam 1", "Quiz 12"]
    assert items[1]["url"] == "https://canvas.test/courses/1/assignments/9"
    assert items[0]["submitted"] is False


def call(server, tool):
    import asyncio
    return str(asyncio.run(server.call_tool(tool, {})))


def test_sync_uses_course_numbers_and_skips_classes_with_nothing_due(tmp_path):
    from thread.server import build_server

    brain = Brain(tmp_path)
    call(build_server(brain, fake_canvas()), "sync_canvas")
    note = brain.read("classes/21-127-concepts-of-mathematics/upcoming-deadlines.md")
    assert "Quiz 12" in note and "canvas" in note
    assert not (tmp_path / "classes/safer-anti-hazing-training-2026").exists()
    assert sorted(p.name for p in (tmp_path / "classes").iterdir() if p.is_dir()) == [
        "21-127-concepts-of-mathematics", "36-200-reasoning-with-data"]


def test_class_that_had_a_note_gets_cleared_when_work_is_done(tmp_path):
    from thread.server import build_server

    brain = Brain(tmp_path)
    brain.write("Upcoming deadlines", "- old quiz", folder="classes/SAFER Safer Scotties Anti-Hazing Training 2026")
    call(build_server(brain, fake_canvas()), "sync_canvas")
    note = brain.read("classes/safer-safer-scotties-anti-hazing-training-2026/upcoming-deadlines.md")
    assert "old quiz" not in note and "Nothing due" in note


def test_expired_token_gives_instructions_not_a_crash(tmp_path):
    from thread.server import build_server

    result = call(build_server(Brain(tmp_path), fake_canvas(status=401)), "whats_due")
    assert "Expired access token" in result and "security add-generic-password -U" in result


# ---- times in the student's zone, an exam filter, submitted work hidden ----
SEMESTER = [
    {"plannable_type": "assignment", "context_name": "Core@CMU", "course_id": 4,
     "plannable": {"title": "Reflection 3", "due_at": "2026-10-22T03:59:00Z"},   # 11:59 PM Oct 21 in Pittsburgh
     "submissions": {"submitted": True}, "html_url": "/courses/4/assignments/1"},
    {"plannable_type": "assignment", "context_name": "Matrices", "course_id": 5,
     "plannable": {"title": "Exam 2", "due_at": "2026-10-28T19:00:00Z"},
     "submissions": {"submitted": False}, "html_url": "/courses/5/assignments/2"},
    {"plannable_type": "quiz", "context_name": "Matrices", "course_id": 5,
     "plannable": {"title": "Recitation check-in", "due_at": "2026-11-05T16:00:00Z"},   # after DST ends: EST
     "submissions": {"submitted": False}, "html_url": "/courses/5/quizzes/3"},
]


def semester_canvas():
    def handler(request):
        if request.url.path.endswith("/courses"):
            return httpx.Response(200, json=[{"id": 4, "name": "99101-DC Core@CMU F26", "course_code": "99101"},
                                             {"id": 5, "name": "Matrices and Linear Transformations", "course_code": "21-241"}])
        return httpx.Response(200, json=SEMESTER)
    return CanvasClient("t", "https://canvas.test", transport=httpx.MockTransport(handler))


def call_with(server, tool, args):
    import asyncio
    return str(asyncio.run(server.call_tool(tool, args)))


def test_times_are_in_the_persons_zone_with_the_zone_named(tmp_path):
    from thread import server as srv
    from thread.workspace import Workspace
    w = Workspace(Brain(tmp_path / "brain"), settings=tmp_path / "s.json")
    w.set_timezone("America/New_York")
    assert srv.friendly_time("2026-10-22T03:59:00Z", w.timezone()) == "Wed Oct 21, 11:59 PM EDT"
    assert srv.friendly_time("2026-11-05T16:00:00Z", w.timezone()) == "Thu Nov 5, 11:00 AM EST"   # DST handled
    w.set_timezone("America/Los_Angeles")
    assert srv.friendly_time("2026-10-22T03:59:00Z", w.timezone()) == "Wed Oct 21, 8:59 PM PDT"


def test_unknown_zone_is_rejected_and_unset_falls_back(tmp_path):
    import pytest
    from thread.workspace import FALLBACK_TIMEZONE, Workspace
    w = Workspace(Brain(tmp_path / "brain"), settings=tmp_path / "s.json")
    assert str(w.timezone()) == FALLBACK_TIMEZONE
    with pytest.raises(Exception):
        w.set_timezone("Mars/Olympus_Mons")
    assert Workspace(Brain(tmp_path / "b2")).timezone() is None      # laptop: the computer's own zone


def test_whats_due_hides_submitted_and_filters_exams(tmp_path):
    from thread.server import build_server
    canvas = semester_canvas()
    server = build_server(Brain(tmp_path), canvas)
    everything = call_with(server, "whats_due", {"days": 60})
    assert "Reflection 3" not in everything and "1 already submitted, not shown" in everything
    assert "Reflection 3" in call_with(server, "whats_due", {"days": 60, "include_submitted": True})
    exams = call_with(server, "whats_due", {"days": 60, "only": "exams"})
    assert "Exam 2" in exams and "Recitation check-in" not in exams
    quizzes = call_with(server, "whats_due", {"days": 60, "only": "quizzes"})
    assert "Recitation check-in" in quizzes and "Exam 2" not in quizzes
    assert "must be" in call_with(server, "whats_due", {"only": "labs"})
