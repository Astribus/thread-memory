"""TTL cache for Canvas responses, stored as JSON in the user's folder:

    canvas-cache/courses.json         class names                 COURSES_SECONDS
    canvas-cache/due-<days>.json      upcoming work               DUE_SECONDS
    canvas-cache/syllabus-<id>.json   one class's syllabus text   SYLLABUS_SECONDS

Expired syllabi only re-download PDFs whose modified date or size changed. Entries are keyed by a hash
of the Canvas credential, so switching tokens invalidates them. `refreshed()` bypasses the cache once.
"""

import hashlib
import json
import os
import re
import time
from pathlib import Path

COURSES_SECONDS = 24 * 60 * 60   # course list: daily
DUE_SECONDS = 5 * 60   # deadlines: 5 minutes
SYLLABUS_SECONDS = 7 * 24 * 60 * 60   # syllabus: weekly; PDFs re-read only if changed


def source_key(secret: str) -> str:
    """Which Canvas connection an answer came from, without storing the token or link."""
    return hashlib.sha256(secret.encode()).hexdigest()[:16]


class CachedCanvas:
    """Same shape as CanvasClient / FeedCanvas (course_labels, upcoming, syllabus), with saved answers."""

    def __init__(self, inner, folder: Path, source: str, clock=time.time):
        self.inner, self.folder, self.source_id, self.clock = inner, Path(folder), source, clock
        self.fresh = False

    def __getattr__(self, name):   # delegate everything else to the wrapped client
        return getattr(self.inner, name)

    def refreshed(self) -> "CachedCanvas":
        """Copy that bypasses (but still writes) the cache."""
        copy = CachedCanvas(self.inner, self.folder, self.source_id, self.clock)
        copy.fresh = True
        return copy

    def course_labels(self) -> dict[int, str]:
        labels = self._remember("courses", COURSES_SECONDS, self.inner.course_labels)
        return {int(k): v for k, v in labels.items()}   # JSON keys are strings

    def upcoming(self, days: int = 14) -> list[dict]:
        return self._remember(f"due-{int(days)}", DUE_SECONDS, lambda: self.inner.upcoming(days))

    def syllabus(self, course_id: int) -> dict:
        name = f"syllabus-{int(course_id)}"
        saved = self._read(name, SYLLABUS_SECONDS)
        if saved is not None and not self.fresh:
            return saved
        # Stale or forced refresh: refetch, passing known PDFs so unchanged ones are reused.
        old = self._read(name, None) or {}
        known = {f["key"]: f for f in old.get("files", []) if f.get("key") and "text" in f}
        data = self.inner.syllabus(course_id, known=known)
        self._write(name, data)
        return data

    def clear(self) -> None:
        for path in self.folder.glob("*.json"):
            path.unlink(missing_ok=True)

    # ---- files ----
    def _remember(self, name: str, seconds: int, fetch):
        if not self.fresh:
            saved = self._read(name, seconds)
            if saved is not None:
                return saved
        data = fetch()   # errors propagate and nothing is cached
        self._write(name, data)
        return data

    def _read(self, name: str, seconds: int | None):
        """The saved answer, or None if missing, broken, from another connection, or older than `seconds`."""
        try:
            entry = json.loads((self.folder / f"{name}.json").read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        if not isinstance(entry, dict) or entry.get("source") != self.source_id:
            return None
        age = self.clock() - float(entry.get("saved", 0))
        if seconds is not None and not 0 <= age < seconds:
            return None
        return entry.get("data")

    def _write(self, name: str, data) -> None:
        if not re.fullmatch(r"[a-z0-9-]+", name):
            raise ValueError("bad cache name")
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            path, tmp = self.folder / f"{name}.json", self.folder / f".{name}.{os.getpid()}.tmp"
            tmp.write_text(json.dumps({"source": self.source_id, "saved": self.clock(), "data": data}))
            tmp.chmod(0o600)
            tmp.replace(path)   # atomic write
        except OSError:
            pass   # caching is best-effort
