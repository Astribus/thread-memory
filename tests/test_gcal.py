import asyncio

from thread.brain import Brain
from thread.gcal import Calendar, CalendarAuthError
from thread.server import build_server


class FakeRequest:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class FakeEvents:
    def __init__(self):
        self.inserted = []

    def list(self, **kwargs):
        assert kwargs["singleEvents"] is True
        if kwargs["calendarId"] == "not-shared@andrew.cmu.edu":
            raise not_shared()
        return FakeRequest({"items": [
            {"summary": "36-200 Lecture", "start": {"dateTime": "2026-09-30T10:00:00-04:00"}, "location": "Porter 100"},
            {"summary": "Mom's birthday", "start": {"date": "2026-10-02"}},
        ]})

    def insert(self, calendarId, body):
        self.inserted.append(body)
        return FakeRequest({"htmlLink": "https://calendar.google.com/event?eid=x"})


def not_shared():
    from googleapiclient.errors import HttpError

    class Resp(dict):
        status = 404
        reason = "Not Found"
    return HttpError(Resp(), b"not found")


class FakeService:
    def __init__(self):
        self._events = FakeEvents()

    def events(self):
        return self._events


def test_upcoming_reads_timed_and_all_day_events():
    events = Calendar(FakeService(), {"read": ["primary"], "write": "primary"}).upcoming()
    assert events[0] == {"title": "36-200 Lecture", "start": "2026-09-30T10:00:00-04:00",
                         "location": "Porter 100", "calendar": "personal"}
    assert events[1]["start"] == "2026-10-02"


def test_all_day_event_end_is_next_day():
    service = FakeService()
    Calendar(service, {"read": ["primary"], "write": "primary"}).add("21-127 Test 3", "2026-10-07")
    body = service.events().inserted[0]
    assert body["start"] == {"date": "2026-10-07"} and body["end"] == {"date": "2026-10-08"}


def test_timed_event_defaults_to_30_minutes():
    service = FakeService()
    Calendar(service, {"read": ["primary"], "write": "primary"}).add("36-200 HW4 due", "2026-10-07T23:30")
    body = service.events().inserted[0]
    assert body["start"]["dateTime"].startswith("2026-10-07T23:30")
    assert body["end"]["dateTime"].startswith("2026-10-08T00:00")


def test_not_signed_in_gives_login_instructions(tmp_path):
    class SignedOut(Calendar):
        def upcoming(self, days=14):
            raise CalendarAuthError("not signed in yet")

    server = build_server(Brain(tmp_path), calendar=SignedOut())
    result = str(asyncio.run(server.call_tool("calendar_upcoming", {})))
    assert "thread-google-login" in result


def test_reads_several_calendars_dedupes_and_reports_unshared_ones():
    cal = Calendar(FakeService(), {"read": ["primary", "jdoe@andrew.cmu.edu", "not-shared@andrew.cmu.edu"],
                                   "write": "primary"})
    events = cal.upcoming()
    assert len(events) == 2                                  # same events on two calendars counted once
    assert cal.missing == ["not-shared@andrew.cmu.edu"]


def test_calendar_labels_and_friendly_times():
    from thread.gcal import calendar_label
    from thread.server import friendly_time
    assert calendar_label("primary") == "personal"
    assert calendar_label("jdoe@andrew.cmu.edu") == "CMU"
    assert friendly_time("2026-10-02") == "Fri Oct 2 (all day)"
    assert friendly_time("2026-10-02T09:00:00-04:00").startswith("Fri Oct 2")
