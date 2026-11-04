"""Read-only Canvas LMS client: courses, upcoming work, syllabi, pages and announcements."""

import html
import io
import logging
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import httpx

DEFAULT_BASE_URL = "https://canvas.cmu.edu"
KEYCHAIN_SERVICE = "thread-canvas"

# httpx logs every request at INFO.
logging.getLogger("httpx").setLevel(logging.WARNING)

# Limits for read_syllabus.
MAX_SYLLABUS_FILES = 3   # syllabus PDFs per course
MAX_PDF_PAGES = 30   # pages per PDF
MAX_PDF_BYTES = 15_000_000   # skip larger files
MAX_SYLLABUS_CHARS = 30_000   # total text returned

# Limits for the read-only Canvas tools.
SEARCH_RESULTS = 10
SNIPPET_CHARS = 240
SEARCH_ANNOUNCEMENT_DAYS = 120   # announcement lookback for canvas_search
MAX_TOOL_CHARS = 12_000   # max characters per tool response

# /courses/1/files/7, /files/7, /api/v1/courses/1/files/7
FILE_LINK = re.compile(r"/(?:api/v1/)?(?:courses/\d+/)?files/(\d+)")

# Planner types that represent assignable work.
WORK_TYPES = {"assignment", "quiz", "discussion_topic", "wiki_page", "sub_assignment"}


def load_token() -> str | None:
    """CANVAS_TOKEN env var wins (handy for tests); otherwise read the macOS Keychain."""
    if token := os.environ.get("CANVAS_TOKEN"):
        return token
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True,
        )
    except FileNotFoundError:   # no `security` binary (not macOS)
        return None
    return result.stdout.strip() or None


class CanvasAuthError(Exception):
    """The token was rejected (expired, revoked, or wrong)."""


class _Text(HTMLParser):
    """Canvas syllabus HTML -> plain text, keeping line breaks where paragraphs, rows and list items end."""
    BREAKS = {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol"}

    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BREAKS:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in self.BREAKS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _Text()
    parser.feed(markup or "")
    text = html.unescape("".join(parser.parts)).replace("\xa0", " ")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


ANCHOR = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)


def syllabus_links(markup: str) -> list[str]:
    """File ids of links whose text or address says "syllabus" (the front page links to lots of other files)."""
    ids = []
    for href, label in ANCHOR.findall(markup or ""):
        match = FILE_LINK.search(href)
        if match and "syllabus" in (html_to_text(label) + " " + href).lower():
            ids.append(match.group(1))
    return ids


def pdf_text(data: bytes, max_pages: int = MAX_PDF_PAGES) -> tuple[str, int, int]:
    """(text of the first `max_pages` pages, pages read, total pages)."""
    from pypdf import PdfReader   # lazy import: pypdf costs ~20 MB
    reader = PdfReader(io.BytesIO(data))
    total = len(reader.pages)
    pages = [(page.extract_text() or "").strip() for page in reader.pages[:max_pages]]
    return "\n\n".join(p for p in pages if p), min(total, max_pages), total


class CanvasClient:
    def __init__(self, token: str, base_url: str = DEFAULT_BASE_URL,
                 transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        # File downloads follow signed links to other hosts, so they use a client without the token.
        self.files = httpx.Client(transport=transport, timeout=30, follow_redirects=True)
        self.http = httpx.Client(
            base_url=f"{self.base_url}/api/v1",
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
            timeout=20,
        )

    def _get_all(self, path: str, params: dict) -> list[dict]:
        """Canvas splits long lists into pages linked by the Link header; follow them all."""
        items, url = [], path
        params = {**params, "per_page": 100}
        while url:
            response = self.http.get(url, params=params)
            if response.status_code == 401:
                errors = response.json().get("errors") or [{}]
                raise CanvasAuthError(errors[0].get("message", "Canvas rejected the access token."))
            response.raise_for_status()
            items.extend(response.json())
            url = response.links.get("next", {}).get("url")
            params = None   # the next URL already has the query
        return items

    def _get_json(self, path: str, params: dict | None = None):
        """GET one resource. None on 403/404; CanvasAuthError only for an invalid token."""
        response = self.http.get(path, params=params)
        if response.status_code == 401:
            errors = (response.json() if response.content else {}).get("errors") or [{}]
            message = errors[0].get("message", "")
            if "not authorized" in message.lower():
                return None
            raise CanvasAuthError(message or "Canvas rejected the access token.")
        if response.status_code in (403, 404):
            return None
        response.raise_for_status()
        return response.json()

    def _list(self, path: str, params: dict | None = None) -> list[dict]:
        """Like _get_all, but a list the course hides (403/404, "not authorized") is just empty."""
        items, url, params = [], path, {**(params or {}), "per_page": 100}
        while url:
            response = self.http.get(url, params=params)
            if response.status_code == 401:
                errors = (response.json() if response.content else {}).get("errors") or [{}]
                message = errors[0].get("message", "")
                if "not authorized" in message.lower():
                    return items
                raise CanvasAuthError(message or "Canvas rejected the access token.")
            if response.status_code in (403, 404):
                return items
            response.raise_for_status()
            data = response.json()
            items.extend(data if isinstance(data, list) else [])
            url = response.links.get("next", {}).get("url")
            params = None
        return items

    # ---- read-only tools ----
    def announcements(self, course_ids: list[int], days: int = 14) -> list[dict]:
        """Announcements posted in the last `days` days, newest first."""
        if not course_ids:
            return []
        now = datetime.now(timezone.utc)
        params = [("context_codes[]", f"course_{cid}") for cid in course_ids]
        params += [("start_date", (now - timedelta(days=days)).date().isoformat()), ("end_date", now.date().isoformat()),
                   ("per_page", 100)]
        response = self.http.get("/announcements", params=params)
        if response.status_code == 401:
            raise CanvasAuthError("Canvas rejected the access token.")
        if response.status_code in (403, 404):
            return []
        response.raise_for_status()
        out = []
        for a in response.json():
            code = a.get("context_code", "")
            out.append({"course_id": int(code.split("_")[-1]) if code.startswith("course_") else None,
                        "title": a.get("title", "Untitled"), "posted": a.get("posted_at") or a.get("created_at"),
                        "text": html_to_text(a.get("message") or ""), "url": a.get("html_url") or ""})
        return sorted(out, key=lambda a: a["posted"] or "", reverse=True)

    def assignments(self, course_id: int, search: str = "") -> list[dict]:
        params = {"include[]": "submission", "order_by": "due_at"}
        if search:
            params["search_term"] = search
        return self._list(f"/courses/{course_id}/assignments", params)

    def assignment(self, course_id: int, assignment_id: int) -> dict | None:
        return self._get_json(f"/courses/{course_id}/assignments/{assignment_id}", {"include[]": "submission"})

    def pages(self, course_id: int, with_body: bool = False) -> list[dict]:
        params = {"sort": "title"}
        if with_body:
            params["include[]"] = "body"
        return self._list(f"/courses/{course_id}/pages", params)

    def page(self, course_id: int, url: str) -> dict | None:
        return self._get_json(f"/courses/{course_id}/pages/{url}")

    def modules(self, course_id: int) -> list[dict]:
        return self._list(f"/courses/{course_id}/modules", {"include[]": "items"})

    def syllabus_body(self, course_id: int) -> str:
        course = self._get_json(f"/courses/{course_id}", {"include[]": "syllabus_body"}) or {}
        return html_to_text(course.get("syllabus_body") or "")

    def syllabus(self, course_id: int, known: dict | None = None) -> dict:
        """Collect syllabus text from the Syllabus tab, wiki pages and linked or named PDFs.

        Returns {"page": str, "pages": [(title, text)], "files": [{"name", "text", "pages_read",
        "pages_total"} | {"name", "skipped"}]}. `known` maps (id, modified, size) to earlier results so
        unchanged PDFs aren't re-downloaded.
        """
        course = self._get_json(f"/courses/{course_id}", {"include[]": "syllabus_body"}) or {}
        body = course.get("syllabus_body") or ""
        ids = list(dict.fromkeys(FILE_LINK.findall(body)))   # files linked from the Syllabus tab
        front = (self._get_json(f"/courses/{course_id}/front_page") or {}).get("body") or ""
        ids += syllabus_links(front)   # front-page links mentioning "syllabus"
        pages = []
        for page in self._get_json(f"/courses/{course_id}/pages", {"search_term": "syllabus", "per_page": 10}) or []:
            full = self._get_json(f"/courses/{course_id}/pages/{page.get('url')}") or {}
            if full.get("body"):
                pages.append((full.get("title") or page.get("title") or "Syllabus", html_to_text(full["body"])))
                ids += FILE_LINK.findall(full["body"])
        found = self._get_json(f"/courses/{course_id}/files", {"search_term": "syllabus", "per_page": 20}) or []
        ids += [str(f["id"]) for f in found if isinstance(f, dict)]
        ids = list(dict.fromkeys(ids))
        files = []
        for fid in ids:
            if sum("text" in f for f in files) >= MAX_SYLLABUS_FILES:
                break
            info = self._get_json(f"/files/{fid}")
            if not info:
                continue
            name = info.get("display_name") or info.get("filename") or f"file {fid}"
            kind = (info.get("content-type") or info.get("mime_class") or "").lower()
            if "pdf" not in kind and not name.lower().endswith(".pdf"):
                continue
            key = f'{fid}:{info.get("modified_at") or info.get("updated_at") or ""}:{info.get("size") or 0}'
            if key in (known or {}):
                files.append(known[key])
                continue
            if (info.get("size") or 0) > MAX_PDF_BYTES:
                files.append({"name": name, "skipped": "too big to read"})
                continue
            try:
                response = self.files.get(info["url"])
                response.raise_for_status()
                text, read, total = pdf_text(response.content)
            except Exception:   # one unreadable PDF shouldn't fail the whole call
                files.append({"name": name, "skipped": "couldn't be read"})
                continue
            files.append({"name": name, "text": text, "pages_read": read, "pages_total": total, "key": key})
        return {"page": html_to_text(body), "pages": pages, "files": files}

    def courses(self) -> list[dict]:
        return self._get_all("/courses", {"enrollment_state": "active"})

    def course_labels(self) -> dict[int, str]:
        """Course id -> "21-127 Concepts of Mathematics", so folders keep the course number."""
        labels = {}
        for course in self.courses():
            name, code = course.get("name", ""), course.get("course_code", "")
            # Drop the course code when its number already appears in the name.
            number = re.match(r"\w+", code)
            labels[course["id"]] = name if not number or number.group() in name else f"{code} {name}"
        return labels

    def upcoming(self, days: int = 14) -> list[dict]:
        """Work due in the next `days` days, soonest first."""
        now = datetime.now(timezone.utc)
        raw = self._get_all("/planner/items", {
            "start_date": now.isoformat(),
            "end_date": (now + timedelta(days=days)).isoformat(),
        })
        items = []
        for item in raw:
            if item.get("plannable_type") not in WORK_TYPES:
                continue
            plannable = item.get("plannable") or {}
            submissions = item.get("submissions") or {}
            items.append({
                "course_id": item.get("course_id"),
                "course": item.get("context_name") or "Unknown course",
                "title": plannable.get("title", "Untitled"),
                "type": item["plannable_type"],
                "due": plannable.get("due_at") or item.get("plannable_date"),
                "submitted": bool(submissions.get("submitted")),
                "url": self.base_url + item.get("html_url", ""),
            })
        items.sort(key=lambda i: i["due"] or "")
        return items
