"""Google Calendar: see what's coming up and add events (e.g. homework due dates)."""

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow, InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from thread.sealed import read_secret, write_secret

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
CONFIG_DIR = Path(os.environ.get("THREAD_CONFIG_DIR", Path.home() / ".config" / "thread"))
CLIENT_FILE = CONFIG_DIR / "google-client.json"
TOKEN_FILE = CONFIG_DIR / "google-token.json"
# Calendars to read and the default write target. Calendars shared into the account also work:
# {"read": ["primary", "someone@example.edu"], "write": "primary"}
CALENDARS_FILE = CONFIG_DIR / "calendars.json"


def calendar_settings(path: Path | None = None) -> dict:
    try:
        settings = json.loads((path or CALENDARS_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        settings = {}
    return {"read": settings.get("read") or ["primary"], "write": settings.get("write") or "primary"}


def calendar_label(cal_id: str) -> str:
    """Short name for a calendar: 'personal', a school name from its email ('CMU'), or the id."""
    if cal_id == "primary":
        return "personal"
    if cal_id.endswith("@andrew.cmu.edu") or cal_id.endswith("@cmu.edu"):
        return "CMU"
    return cal_id


class CalendarAuthError(Exception):
    """Not signed in yet, or the sign-in expired (Testing-mode apps expire after ~7 days)."""


def login() -> None:
    """One-time browser sign-in. Run with: uv run thread-google-login"""
    if not CLIENT_FILE.exists():
        raise SystemExit(f"Missing {CLIENT_FILE}. Move the downloaded client_secret JSON there first.")
    creds = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES).run_local_server(port=0)
    TOKEN_FILE.write_text(creds.to_json())
    TOKEN_FILE.chmod(0o600)
    print(f"Signed in. Saved to {TOKEN_FILE}")


# ---------- OAuth from the web UI ----------
# Redirect URI is /google/callback (loopback for a Desktop client; a Web client online, see docs/DEPLOY.md).
# Pending flows: state -> (PKCE verifier, account). Online, the callback must match the starting account.
_pending: dict[str, tuple[str, str | None]] = {}


def connect_url(redirect_uri: str, owner: str | None = None) -> str:
    """Where to send the browser to pick a Google account (always shows the account chooser)."""
    if not CLIENT_FILE.exists():
        raise CalendarAuthError("Google isn't set up on this Thread yet (missing google-client.json)")
    flow = Flow.from_client_secrets_file(str(CLIENT_FILE), SCOPES, redirect_uri=redirect_uri)
    url, state = flow.authorization_url(access_type="offline", prompt="select_account consent")
    if len(_pending) > 20:   # drop abandoned flows
        _pending.clear()
    _pending[state] = (flow.code_verifier, owner)
    return url


def finish_connect(redirect_uri: str, state: str, code: str, token_file: Path | None = None,
                   owner: str | None = None, sealer=None) -> None:
    token_file = token_file or TOKEN_FILE
    verifier, started_by = _pending.pop(state, (None, None))
    if verifier is None or started_by != owner:
        raise CalendarAuthError("This sign-in link expired or wasn't started here. Try again.")
    flow = Flow.from_client_secrets_file(str(CLIENT_FILE), SCOPES, redirect_uri=redirect_uri,
                                         state=state, code_verifier=verifier)
    flow.fetch_token(code=code)
    write_secret(token_file, flow.credentials.to_json(), sealer)


def disconnect(token_file: Path | None = None) -> None:
    """Forget the Google sign-in on this Thread (the calendar itself is untouched)."""
    (token_file or TOKEN_FILE).unlink(missing_ok=True)


def status(token_file: Path | None = None, sealer=None) -> dict:
    """For the settings page: is Google connected, and as which account?"""
    token_file = token_file or TOKEN_FILE
    if not CLIENT_FILE.exists():
        return {"state": "unavailable"}
    if not token_file.exists():
        return {"state": "disconnected"}
    try:
        service = build("calendar", "v3", credentials=load_credentials(token_file, sealer), cache_discovery=False)
        # the primary calendar's id is the account email
        email = service.events().list(calendarId="primary", maxResults=1).execute().get("summary", "")
    except CalendarAuthError:
        return {"state": "expired"}
    except Exception:
        return {"state": "connected", "account": ""}
    return {"state": "connected", "account": email}


def load_credentials(token_file: Path | None = None, sealer=None) -> Credentials:
    token_file = token_file or TOKEN_FILE
    saved = read_secret(token_file, sealer)   # sealed at rest online
    if not saved:
        raise CalendarAuthError("not signed in yet")
    creds = Credentials.from_authorized_user_info(json.loads(saved), SCOPES)
    if not creds.valid:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            raise CalendarAuthError("sign-in expired") from e
        write_secret(token_file, creds.to_json(), sealer)
    return creds


class Calendar:
    def __init__(self, service=None, settings: dict | None = None, token_file: Path | None = None, sealer=None):
        # `service` and `settings` are injectable for tests; `token_file` selects the account's token.
        self._service = service
        self.token_file = token_file
        self.sealer = sealer
        self.settings = settings or calendar_settings()

    @property
    def service(self):
        if self._service is None:
            self._service = build("calendar", "v3", credentials=load_credentials(self.token_file, self.sealer),
                                  cache_discovery=False)
        return self._service

    def upcoming(self, days: int = 14) -> list[dict]:
        """Merged events from settings["read"], soonest first; inaccessible calendars are reported, not fatal."""
        now = datetime.now(timezone.utc)
        events, seen, missing = [], set(), []
        for cal_id in self.settings["read"]:
            try:
                result = self.service.events().list(
                    calendarId=cal_id,
                    timeMin=now.isoformat(),
                    timeMax=(now + timedelta(days=days)).isoformat(),
                    singleEvents=True,   # expand recurring events
                    orderBy="startTime",
                ).execute()
            except HttpError as e:
                if e.resp.status in (403, 404):
                    missing.append(cal_id)
                    continue
                raise
            for e in result.get("items", []):
                start = e["start"].get("dateTime") or e["start"].get("date")
                key = (e.get("summary"), start)   # same event on two calendars
                if key in seen:
                    continue
                seen.add(key)
                events.append({
                    "title": e.get("summary", "(no title)"),
                    "start": start,
                    "location": e.get("location", ""),
                    "calendar": calendar_label(cal_id),
                })
        events.sort(key=lambda ev: ev["start"])
        self.missing = missing
        return events

    def add(self, title: str, start: str, end: str | None = None, description: str = "",
            calendar: str | None = None, tz=None) -> str:
        """`start` is a date ("2026-10-07") for an all-day event, or a datetime for a timed one.
        Timed events default to 30 minutes. `calendar` picks a calendar id (default: settings["write"])."""
        if "T" in start:
            begin = datetime.fromisoformat(start)
            # Naive times are in the user's zone (`tz` online, where the server runs in UTC).
            local = (lambda t: t.replace(tzinfo=tz)) if tz else (lambda t: t.astimezone())
            if begin.tzinfo is None:
                begin = local(begin)
            finish = datetime.fromisoformat(end) if end else begin + timedelta(minutes=30)
            if finish.tzinfo is None:
                finish = local(finish)
            when = {"start": {"dateTime": begin.isoformat()}, "end": {"dateTime": finish.isoformat()}}
        else:
            day = date.fromisoformat(start)
            last = date.fromisoformat(end) if end else day
            when = {"start": {"date": day.isoformat()},
                    "end": {"date": (last + timedelta(days=1)).isoformat()}}   # end date is exclusive
        event = self.service.events().insert(
            calendarId=calendar or self.settings["write"],
            body={"summary": title, "description": description, **when},
        ).execute()
        return event.get("htmlLink", "")
