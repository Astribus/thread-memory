"""Canvas deadlines from the per-user iCalendar feed (Calendar → Calendar Feed), no access token needed.

The feed covers assignments and events across courses but not submission status or files. The URL is a
bearer secret and is stored like a token. `FeedCanvas` matches the `CanvasClient` interface used by
whats_due and sync_canvas.
"""

import ipaddress
import re
import socket
import zlib
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlparse

import httpx

FEED_PATH = re.compile(r"^/feeds/calendars/[A-Za-z0-9_\-]+\.ics$")
MAX_FEED_BYTES = 5 * 1024 * 1024   # real feeds are a few hundred KB


class FeedError(Exception):
    """The link isn't a Canvas calendar feed, or it couldn't be read."""


def check_feed_url(url: str) -> str:
    """Normalise and validate a feed URL (https, feed path, public host) to prevent SSRF."""
    url = url.strip()
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    parts = urlparse(url)
    if parts.scheme != "https" or not parts.hostname or not FEED_PATH.match(parts.path):
        raise FeedError("That isn't a Canvas calendar feed link. In Canvas, open Calendar and copy the "
                        '"Calendar Feed" link (it ends in .ics).')
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(parts.hostname, 443)}
    except socket.gaierror:
        raise FeedError(f"Couldn't find {parts.hostname}. Check the link.")
    for address in addresses:
        if not _public(address):
            raise FeedError("That link points somewhere Thread can't reach.")
    return url


def _public(address: str) -> bool:
    """Globally routable addresses only (`is_global` also excludes CGNAT and fdaa::/16)."""
    ip = ipaddress.ip_address(address.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def fetch_feed(url: str, transport: httpx.BaseTransport | None = None) -> str:
    url = check_feed_url(url) if transport is None else url
    try:
        with httpx.Client(timeout=20, follow_redirects=False, transport=transport) as http:
            with http.stream("GET", url) as response:
                # Re-check the connected address to defeat DNS rebinding.
                if transport is None:
                    stream = response.extensions.get("network_stream")
                    peer = stream.get_extra_info("server_addr") if stream else None
                    if not peer or not _public(str(peer[0])):
                        raise FeedError("That link points somewhere Thread can't reach.")
                if response.status_code != 200:
                    raise FeedError("Canvas didn't accept that feed link. Copy it again from Canvas → Calendar → Calendar Feed.")
                data = bytearray()
                for chunk in response.iter_bytes():   # enforce the size cap while streaming
                    data += chunk
                    if len(data) > MAX_FEED_BYTES:
                        raise FeedError("That calendar feed is too big to read.")
    except httpx.HTTPError:
        raise FeedError("Couldn't reach Canvas. Try again in a minute.")
    text = bytes(data).decode("utf-8", errors="replace")
    if "BEGIN:VCALENDAR" not in text[:2000]:
        raise FeedError("That link didn't return a calendar. Copy the Calendar Feed link from Canvas again.")
    return text


def _unfold(text: str) -> list[str]:
    """iCalendar wraps long lines with a leading space on the next line (RFC 5545 §3.1)."""
    lines = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _unescape(value: str) -> str:
    return (value.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",")
            .replace("\\;", ";").replace("\\\\", "\\"))


def _when(params: str, value: str):
    """DTSTART → an aware datetime (UTC) or a date for all-day items."""
    if "VALUE=DATE" in params.upper() and "T" not in value:
        return date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    stamp = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        return stamp.replace(tzinfo=timezone.utc)
    tz = re.search(r"TZID=([^;:]+)", params)
    if tz:
        from zoneinfo import ZoneInfo
        try:
            return stamp.replace(tzinfo=ZoneInfo(tz.group(1))).astimezone(timezone.utc)
        except Exception:
            pass
    return stamp.replace(tzinfo=timezone.utc)


def parse_events(text: str) -> list[dict]:
    """The VEVENTs in a feed as {uid, summary, start, url, description}."""
    events, current = [], None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            if current is not None and current.get("start") is not None:
                events.append(current)
            current = None
        elif current is not None and ":" in line:
            name, _, value = line.partition(":")
            key, _, params = name.partition(";")
            key = key.upper()
            if key == "DTSTART":
                try:
                    current["start"] = _when(params, value.strip())
                except ValueError:
                    pass
            elif key in ("SUMMARY", "URL", "UID", "DESCRIPTION"):
                current[key.lower()] = _unescape(value.strip())
    return events


COURSE_IN_SUMMARY = re.compile(r"^(.*?)\s*\[([^\]]+)\]\s*$")   # "Homework 3 [21127 Concepts of Mathematics]"


def _course_id(label: str) -> int:
    return zlib.crc32(label.encode())   # the feed has no course ids


class FeedCanvas:
    """Reads deadlines from a Calendar Feed link. Same shape as CanvasClient for whats_due / sync_canvas."""
    source = "feed"
    MISSING = ("From your Canvas calendar feed, which doesn't say what you've already submitted or include "
               "syllabus files. For those, add a Canvas access token under Settings → Canvas → Advanced.")

    def __init__(self, url: str, transport: httpx.BaseTransport | None = None):
        self.url, self.transport = url, transport
        self._events = None

    def events(self) -> list[dict]:
        if self._events is None:
            self._events = parse_events(fetch_feed(self.url, self.transport))
        return self._events

    def _items(self) -> list[dict]:
        items = []
        for e in self.events():
            match = COURSE_IN_SUMMARY.match(e.get("summary", ""))
            title, course = (match.group(1), match.group(2)) if match else (e.get("summary", "Untitled"), "Canvas")
            uid = e.get("uid", "")
            kind = "assignment" if "assignment" in uid else ("quiz" if "quiz" in uid else "event")
            start = e["start"]
            due = start.isoformat() if isinstance(start, datetime) else start.isoformat()
            items.append({"course_id": _course_id(course), "course": course, "title": title or "Untitled",
                          "type": kind, "due": due, "submitted": False, "url": e.get("url", ""), "_start": start})
        return items

    def course_labels(self) -> dict[int, str]:
        return {i["course_id"]: i["course"] for i in self._items()}

    def upcoming(self, days: int = 14) -> list[dict]:
        now = datetime.now(timezone.utc)
        end = now + timedelta(days=days)
        out = []
        for item in self._items():
            start = item.pop("_start")
            moment = start if isinstance(start, datetime) else datetime.combine(start, time(23, 59), timezone.utc)
            if now <= moment <= end:
                out.append(item)
        out.sort(key=lambda i: i["due"])
        return out
