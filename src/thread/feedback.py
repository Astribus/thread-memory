"""Bug reports and feature requests, appended as JSON lines to feedback/reports.jsonl.

Sources: the Settings form and the `report_bug` tool. Reports contain the user's text and minimal
context (screen, browser), never note contents. Read them with `thread-admin feedback`.
"""

import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

MAX_CHARS = 4000   # rejected, not truncated
MAX_PER_HOUR = 10   # per account
MAX_CONTEXT_CHARS = 300   # per context value
CONTEXT_KEYS = ("tab", "screen", "viewport", "browser")   # other keys are dropped
SOURCES = {"page", "ai"}


def feedback_file(online: bool) -> Path:
    if online:
        from thread.workspace import data_dir
        return data_dir() / "feedback" / "reports.jsonl"
    config = Path(os.environ.get("THREAD_CONFIG_DIR", Path.home() / ".config" / "thread"))
    return config / "feedback" / "reports.jsonl"


class Feedback:
    def __init__(self, path: Path):
        self.path = Path(path)

    def add(self, message: str, account: str, source: str, email: str = "", context: dict | None = None,
            now: float | None = None) -> dict:
        """Append a report. Raises ValueError (user-facing message) if empty, too long or rate-limited."""
        now = now or time.time()
        message = str(message or "").strip()
        if not message:
            raise ValueError("Write what happened first.")
        if len(message) > MAX_CHARS:
            raise ValueError(f"That's {len(message):,} characters; please keep a report under {MAX_CHARS:,}.")
        if source not in SOURCES:
            raise ValueError("Unknown source")
        recent = sum(1 for r in self._all() if r.get("account") == account and now - r.get("ts", 0) < 3600)
        if recent >= MAX_PER_HOUR:
            raise ValueError("You've sent a lot of reports in the last hour. Thank you! Please try again a bit later.")
        extras = {k: str(v)[:MAX_CONTEXT_CHARS] for k, v in (context or {}).items() if k in CONTEXT_KEYS and v}
        report = {"id": secrets.token_hex(4), "ts": now,
                  "time": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
                  "account": account, "email": email, "source": source, "message": message, "context": extras}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(report) + "\n")
        return report

    def _all(self) -> list[dict]:
        try:
            lines = self.path.read_text().splitlines()
        except FileNotFoundError:
            return []
        reports = []
        for line in lines:
            try:
                reports.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return reports

    def latest(self, n: int = 20) -> list[dict]:
        return self._all()[-n:][::-1]


def describe(report: dict) -> str:
    """One report as readable lines, for `thread-admin feedback`."""
    who = report.get("email") or report.get("account", "?")
    ctx = ", ".join(f"{k} {v}" for k, v in (report.get("context") or {}).items())
    head = f"[{report.get('time', '?')}] {who} via {report.get('source', '?')} (#{report.get('id', '?')})"
    return head + (f"\n  {ctx}" if ctx else "") + "\n  " + report.get("message", "").replace("\n", "\n  ")
