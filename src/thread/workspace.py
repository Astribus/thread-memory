"""Per-user state: the notes folder plus Google and Canvas connections.

Local mode has a single workspace (~/thread-brain, Keychain token, ~/.config/thread). Online, each
account lives under THREAD_DATA_DIR/users/<id>/:

    brain/              Markdown notes
    google-token.json   Google OAuth token
    calendars.json      selected calendars
    canvas-token        Canvas access token (optional)
    canvas-feed         Canvas calendar feed URL
    canvas-cache/       cached Canvas responses
    chats/              chat backups (10-day retention)
    settings.json       setup progress

The time zone is stored in brain/me.md.
"""

import json
import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from thread import gcal
from thread.brain import Brain, default_root
from thread.canvas import CanvasClient, load_token
from thread.canvas_cache import CachedCanvas, source_key
from thread.sealed import Sealer, read_secret, write_secret
from thread.chats import ChatBackup
from thread.fingerprint import Fingerprint


# Fallback for accounts without a zone in their profile.
FALLBACK_TIMEZONE = "America/New_York"


def data_dir() -> Path:
    return Path(os.environ.get("THREAD_DATA_DIR", Path.home() / ".config" / "thread" / "data"))


class Workspace:
    def __init__(self, brain: Brain, google_token: Path | None = None, calendars: Path | None = None,
                 canvas_token: Path | None = None, canvas: CanvasClient | None = None, calendar=None,
                 chats: Path | None = None, settings: Path | None = None, canvas_feed: Path | None = None,
                 canvas_cache: Path | None = None,
                 sealer: Sealer | None = None):
        """`canvas_token`: token file path (None = Keychain). `canvas`/`calendar` are injectable for tests."""
        self.brain = brain
        self.fingerprint = Fingerprint(brain)
        self.google_token = google_token
        self.calendars = calendars
        self.canvas_token_file = canvas_token
        self.canvas_feed_file = canvas_feed   # online only
        self.canvas_cache_dir = canvas_cache   # None disables caching
        self.sealer = sealer   # encrypts Canvas and Google credentials
        self._canvas, self._calendar = canvas, calendar
        self.chats = ChatBackup(chats) if chats else None   # online only
        self.settings_file = settings   # online only

    @classmethod
    def local(cls, brain: Brain | None = None, canvas: CanvasClient | None = None, calendar=None) -> "Workspace":
        """The laptop setup: one person, the brain at ~/thread-brain."""
        brain = brain or Brain(default_root())
        if canvas is None and (token := load_token()):
            canvas = CachedCanvas(CanvasClient(token), Path.home() / ".config" / "thread" / "canvas-cache",
                                  source_key("token:" + token))
        return cls(brain, canvas=canvas, calendar=calendar)

    @classmethod
    def for_account(cls, user_id: str, root: Path | None = None, sealer: Sealer | None = None) -> "Workspace":
        if not re.fullmatch(r"[0-9a-f]{16}", user_id):   # ids come from AuthStore; never treat input as a path
            raise ValueError("Bad account id")
        home = (root or data_dir()) / "users" / user_id
        brain = Brain(home / "brain")
        return cls(brain, google_token=home / "google-token.json", calendars=home / "calendars.json",
                   canvas_token=home / "canvas-token", chats=home / "chats", settings=home / "settings.json",
                   canvas_feed=home / "canvas-feed", canvas_cache=home / "canvas-cache", sealer=sealer)

    # ---- Time zone ----
    # The zone is user-chosen and stored in the profile (me.md) so the AI can read it; the browser's zone
    # is never written silently. Without one, local mode uses the system zone.
    def timezone_name(self) -> str | None:
        """The zone written in the profile (migrating an old browser-reported one on first read), or None."""
        from thread.profile import read_profile
        try:
            name = read_profile(self.brain)["about"].get("timezone", "")
        except FileNotFoundError:
            name = ""
        if name:
            return name
        legacy = self._settings().get("timezone") if self.settings_file else None
        if legacy:   # one-time migration from settings.json
            try:
                self.set_timezone(legacy)
            except ValueError:
                return None
            data = self._settings()
            data.pop("timezone", None)
            self.settings_file.write_text(json.dumps(data))
            return self.timezone_name()
        return None

    def timezone(self) -> ZoneInfo | None:
        """The person's zone; None means "this computer's zone" (laptop without one in the profile)."""
        name = self.timezone_name()
        if name:
            try:
                return ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError):
                pass
        return None if self.settings_file is None else ZoneInfo(FALLBACK_TIMEZONE)

    def set_timezone(self, name: str) -> str:
        """Write the zone into the profile. Raises ValueError for anything that isn't a real zone."""
        from thread import timezones
        from thread.profile import read_profile, write_profile
        zone = timezones.canonical(name)
        if not zone:
            raise ValueError("That time zone isn't one Thread knows. Pick one from the list.")
        profile = read_profile(self.brain)
        if profile["about"].get("timezone") != zone:
            profile["about"]["timezone"] = zone
            write_profile(self.brain, profile)
        return zone

    # ---- device zones the user chose to keep ----
    def kept_over(self) -> list[str]:
        return list(self._settings().get("tz_kept_over", [])) if self.settings_file else []

    def keep_zone_over(self, device: str) -> None:
        if self.settings_file is None or not device:
            return
        data = self._settings()
        kept = data.setdefault("tz_kept_over", [])
        if device not in kept:
            kept.append(device)
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        self.settings_file.write_text(json.dumps(data))

    # ---- class colours ----
    def class_colors(self) -> dict[str, int]:
        """{class folder slug: palette index} the person picked. Classes not listed get a colour by order."""
        if self.settings_file is None:
            return {}
        return {k: int(v) for k, v in self._settings().get("class_colors", {}).items()}

    def set_class_color(self, key: str, index: int) -> dict[str, int]:
        if self.settings_file is None:
            raise ValueError("Class colours are saved on the online version of Thread.")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,80}", key) or not 0 <= int(index) < 12:
            raise ValueError("Unknown class or colour")
        data = self._settings()
        data.setdefault("class_colors", {})[key] = int(index)
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        self.settings_file.write_text(json.dumps(data))
        return self.class_colors()

    # ---- setup progress (online) ----
    def setup_state(self) -> dict:
        """{"seen": bool, "skipped": [steps]}: whether the first-run guide was shown, and what was put off."""
        state = self._settings().get("setup", {}) if self.settings_file else {}
        return {"seen": bool(state.get("seen")), "skipped": list(state.get("skipped", []))}

    def update_setup(self, seen: bool | None = None, skipped: str | None = None, unskip: str | None = None) -> dict:
        if self.settings_file is None:
            return self.setup_state()
        data = self._settings()
        state = data.setdefault("setup", {"seen": False, "skipped": []})
        if seen is not None:
            state["seen"] = bool(seen)
        if skipped and skipped not in state.setdefault("skipped", []):
            state["skipped"].append(skipped)
        if unskip and unskip in state.get("skipped", []):
            state["skipped"].remove(unskip)
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        self.settings_file.write_text(json.dumps(data))
        return self.setup_state()

    def _settings(self) -> dict:
        try:
            return json.loads(self.settings_file.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    # ---- Canvas ----
    def canvas_token(self) -> str | None:
        if self.canvas_token_file is None:
            return load_token()
        return read_secret(self.canvas_token_file, self.sealer)

    def canvas(self):
        """Access-token client if configured, else the calendar feed, else None."""
        if self._canvas is not None:
            return self._canvas
        token = self.canvas_token()
        if token:
            return self._cached(CanvasClient(token), "token:" + token)
        feed = self.canvas_feed()
        if feed:
            from thread.canvas_feed import FeedCanvas
            return self._cached(FeedCanvas(feed), "feed:" + feed)
        return None

    def _cached(self, client, secret: str):
        if self.canvas_cache_dir is None:
            return client
        return CachedCanvas(client, self.canvas_cache_dir, source_key(secret))

    def clear_canvas_cache(self) -> None:
        """Delete saved Canvas answers (on connect or disconnect, so nothing lingers from an old connection)."""
        if self.canvas_cache_dir is not None:
            CachedCanvas(None, self.canvas_cache_dir, "").clear()

    def canvas_feed(self) -> str | None:
        return read_secret(self.canvas_feed_file, self.sealer) if self.canvas_feed_file else None

    def set_canvas_feed(self, url: str) -> None:
        self.clear_canvas_cache()
        if self.canvas_feed_file is None:
            raise ValueError("The Canvas calendar feed works on the online version of Thread.")
        write_secret(self.canvas_feed_file, url.strip(), self.sealer)   # grants calendar read access; treat as a secret

    def clear_canvas_feed(self) -> None:
        self.clear_canvas_cache()
        if self.canvas_feed_file is not None:
            self.canvas_feed_file.unlink(missing_ok=True)

    def set_canvas_token(self, token: str) -> None:
        self.clear_canvas_cache()
        if self.canvas_token_file is None:
            raise ValueError("On a laptop, Canvas uses the Keychain (see the README).")
        write_secret(self.canvas_token_file, token.strip(), self.sealer)

    def clear_canvas_token(self) -> None:
        self.clear_canvas_cache()
        if self.canvas_token_file is not None:
            self.canvas_token_file.unlink(missing_ok=True)

    # ---- Google Calendar ----
    def calendar(self):
        if self._calendar is not None:
            return self._calendar
        return gcal.Calendar(settings=gcal.calendar_settings(self.calendars), token_file=self.google_token,
                             sealer=self.sealer)


class Workspaces:
    """Online: hands out (and keeps) each account's workspace."""

    def __init__(self, root: Path | None = None, sealer: Sealer | None = None):
        self.root = root or data_dir()
        self.sealer = sealer
        self._open: dict[str, Workspace] = {}

    def get(self, user_id: str) -> Workspace:
        if user_id not in self._open:
            self._open[user_id] = Workspace.for_account(user_id, self.root, self.sealer)
        return self._open[user_id]
