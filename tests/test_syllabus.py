"""read_syllabus: exam dates that only live in a syllabus."""

import asyncio
import tempfile

import httpx

from thread import canvas as canvas_mod
from thread.brain import Brain
from thread.canvas import CanvasClient, html_to_text, pdf_text
from thread.server import build_server, format_syllabus


def make_pdf(pages: list[str]) -> bytes:
    """A tiny real PDF with one line of text per page (no PDF library needed to write one)."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream.decode()}\nendstream")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 3 0 R >> >> /Contents {len(objs)} 0 R >>")
        kids.append(len(objs))
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>"
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


SYLLABUS_PDF = make_pdf(["21-127 SYLLABUS", "Sep 9th Test 1   Sep 23rd Test 2   Oct 28th Test 4"])
COURSES = [{"id": 1, "name": "Concepts of Mathematics", "course_code": "21-127"},
           {"id": 2, "name": "Reasoning with Data", "course_code": "36-200"},
           {"id": 3, "name": "Matrices and Linear Transformations", "course_code": "21-241"},
           {"id": 4, "name": "Matrix Methods", "course_code": "21-242"}]


def fake(syllabus_body="", front="", pages=None, files_status=403, pdf=SYLLABUS_PDF, seen=None):
    """A fake Canvas. Course 1 links its syllabus PDF (file 7) the way 21-127 does, unless told otherwise."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if seen is not None:
            seen.append((request.url.host, request.headers.get("authorization")))
        if request.url.host == "files.test":
            return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})
        if path == "/api/v1/courses":
            return httpx.Response(200, json=COURSES)
        if path.endswith("/front_page"):
            return httpx.Response(200, json={"body": front}) if front else httpx.Response(404, json={})
        if path.endswith("/files"):
            if files_status != 200:
                return httpx.Response(files_status, json={"errors": [{"message": "user not authorized to perform that action"}]})
            return httpx.Response(200, json=[{"id": 8, "display_name": "Syllabus F26.pdf"}])
        if "/pages/" in path:
            return httpx.Response(200, json=(pages or {}).get(path.rsplit("/", 1)[1], {}))
        if path.endswith("/pages"):
            return httpx.Response(200, json=[{"url": u, "title": p["title"]} for u, p in (pages or {}).items()])
        if path.startswith("/api/v1/files/"):
            fid = path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"id": fid, "display_name": f"Syllabus-{fid}.pdf", "size": len(pdf),
                                             "content-type": "application/pdf", "url": f"https://files.test/dl/{fid}?sig=x"})
        if path.startswith("/api/v1/courses/"):
            return httpx.Response(200, json={"id": 1, "syllabus_body": syllabus_body})
        return httpx.Response(404, json={})
    return CanvasClient("test-token", "https://canvas.test", transport=httpx.MockTransport(handler))


def ask(canvas, course):
    result = asyncio.run(build_server(Brain(tempfile.mkdtemp()), canvas).call_tool("read_syllabus", {"course": course}))
    return result.content[0].text


FRONT = ('<ul><li><a href="https://canvas.test/courses/1/files/7?verifier=abc&amp;wrap=1">Syllabus</a></li>'
         '<li><a href="https://canvas.test/courses/1/files/9?wrap=1">Textbook</a></li></ul>')


def test_html_syllabus_becomes_readable_text():
    text = html_to_text("<h2>Exams</h2><p>Exam 1:&nbsp;<b>Sep 30</b></p><table><tr><td>Exam 2</td><td>Oct 28</td></tr></table>")
    assert "Exams" in text and "Exam 1: Sep 30" in text and "Exam 2 | Oct 28" in text


def test_html_only_syllabus():
    out = ask(fake(syllabus_body="<p>Midterm: Wednesday, October 14</p>"), "21-127")
    assert "Syllabus tab" in out and "Midterm: Wednesday, October 14" in out


def test_pdf_linked_from_the_front_page_like_21_127_and_token_stays_on_canvas():
    seen = []
    out = ask(fake(front=FRONT, seen=seen), "21127")             # dashes and spaces don't matter
    assert "Syllabus-7.pdf (PDF, 2 pages)" in out and "Oct 28th Test 4" in out
    assert "Syllabus-9" not in out                               # the textbook link isn't read
    assert all(auth is None for host, auth in seen if host == "files.test")   # the token never leaves Canvas


def test_files_hidden_still_works_through_links_and_named_pages():
    pages = {"syllabus": {"title": "Syllabus", "body": '<p>Test 1 is Sep 9.</p><a href="/courses/1/files/7">pdf</a>'}}
    out = ask(fake(pages=pages, files_status=403), "concepts")
    assert 'course page "Syllabus"' in out and "Test 1 is Sep 9." in out and "Sep 23rd Test 2" in out


def test_files_search_used_when_files_are_visible():
    assert "Syllabus-8.pdf" in ask(fake(files_status=200), "21-127")


def test_no_syllabus_anywhere():
    out = ask(fake(), "21-127")
    assert out.count("No syllabus found on Canvas for 21-127 Concepts of Mathematics") == 1


def test_unknown_or_ambiguous_course():
    assert 'No class matches "15-122"' in ask(fake(), "15-122") and "21-241" in ask(fake(), "15-122")
    assert "more than one class" in ask(fake(), "matri")


def test_bad_token_gives_instructions():
    def handler(request):
        return httpx.Response(401, json={"errors": [{"message": "Invalid access token."}]})
    out = ask(CanvasClient("x", "https://canvas.test", transport=httpx.MockTransport(handler)), "21-127")
    assert "rejected the access token" in out


def test_caps_pages_files_size_and_characters(monkeypatch):
    long_pdf = make_pdf([f"Page {i}" for i in range(1, 41)])
    text, read, total = pdf_text(long_pdf, max_pages=canvas_mod.MAX_PDF_PAGES)
    assert (read, total) == (30, 40) and "Page 30" in text and "Page 31" not in text
    out = ask(fake(front=FRONT, pdf=long_pdf), "21-127")
    assert "pages 1-30 of 40" in out
    many = "".join(f'<a href="/courses/1/files/{i}">Syllabus part {i}</a>' for i in range(10, 16))
    assert ask(fake(front=many), "21-127").count("## From Syllabus-") == canvas_mod.MAX_SYLLABUS_FILES
    monkeypatch.setattr(canvas_mod, "MAX_PDF_BYTES", 10)
    assert "too big to read" in ask(fake(front=FRONT), "21-127")
    big = {"page": "x" * 50_000, "pages": [], "files": []}
    cut = format_syllabus("21-127", big)
    assert len(cut) < 31_000 and "Cut at 30,000 characters" in cut


def test_broken_pdf_is_skipped_not_fatal():
    assert "couldn't be read" in ask(fake(front=FRONT, pdf=b"not a pdf"), "21-127")
