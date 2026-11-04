from starlette.testclient import TestClient

from thread.brain import Brain
from thread.home import build_app


def client(tmp_path):
    brain = Brain(tmp_path)
    brain.remember("Exam 1 weak spots", "see [[Contingency tables]] and [[Not written]]",
                   folder="classes/36200 Reasoning with Data")
    brain.remember("Contingency tables", "joint / marginal", folder="classes/36200 Reasoning with Data")
    return TestClient(build_app(brain))


def test_page_and_d3_are_served(tmp_path):
    c = client(tmp_path)
    assert "<canvas id=\"map\"" in c.get("/").text
    assert c.get("/static/d3.min.js").status_code == 200


def test_static_files_carry_a_content_fingerprint_so_updates_are_never_stale(tmp_path):
    r = client(tmp_path).get("/")
    assert r.headers["cache-control"] == "no-cache"
    import re
    versions = re.findall(r'/static/(style\.css|app\.js|d3\.min\.js)\?v=([0-9a-f]{10})"', r.text)
    assert {name for name, _ in versions} == {"style.css", "app.js", "d3.min.js"}
    assert client(tmp_path).get(f"/static/style.css?v={versions[0][1]}").status_code == 200


def test_graph_api_has_nodes_edges_and_dates(tmp_path):
    data = client(tmp_path).get("/api/graph").json()
    ids = {n["id"] for n in data["nodes"]}
    assert "me" in ids and "classes/36200-reasoning-with-data/exam-1-weak-spots" in ids
    assert all("date" in n and "degree" in n for n in data["nodes"])
    assert {"source": "classes/classes", "target": "me"} in data["edges"]


def test_note_api_resolves_links_for_clicking(tmp_path):
    note = client(tmp_path).get("/api/note", params={"id": "classes/36200-reasoning-with-data/exam-1-weak-spots"}).json()
    assert note["links"]["Contingency tables"] == "classes/36200-reasoning-with-data/contingency-tables"
    assert note["links"]["Not written"] is None


def test_note_api_refuses_paths_outside_the_brain(tmp_path):
    assert client(tmp_path).get("/api/note", params={"id": "../../etc/passwd"}).status_code == 404


ORIGIN = {"origin": "http://127.0.0.1:8765"}


def test_create_folder_and_note_then_edit(tmp_path):
    c = client(tmp_path)
    folder = c.post("/api/folder", json={"parent": "", "name": "Projects"}, headers=ORIGIN).json()["path"]
    note = c.post("/api/note", json={"folder": folder, "title": "Launch plan"}, headers=ORIGIN).json()["id"]
    assert note == "projects/launch-plan"
    assert c.put("/api/note", json={"id": note, "text": "# Launch plan\n\nTalk to 10 students"}, headers=ORIGIN).status_code == 200
    assert "Talk to 10 students" in c.get("/api/note", params={"id": note}).json()["text"]
    names = [n["name"] for n in c.get("/api/tree").json()["children"]]
    assert "Projects" in names


def test_changes_from_other_websites_are_refused(tmp_path):
    c = client(tmp_path)
    evil = {"origin": "https://evil.example"}
    assert c.post("/api/folder", json={"name": "x"}, headers=evil).status_code == 403
    assert c.post("/api/folder", json={"name": "x"}).status_code == 403  # no origin at all
    form = c.post("/api/folder", data={"name": "x"}, headers=ORIGIN)  # form posts skip the browser's JSON check
    assert form.status_code == 403


def test_duplicate_folder_is_a_clear_error(tmp_path):
    c = client(tmp_path)
    c.post("/api/folder", json={"name": "Projects"}, headers=ORIGIN)
    r = c.post("/api/folder", json={"name": "Projects"}, headers=ORIGIN)
    assert r.status_code == 409 and "already exists" in r.json()["error"]


def test_rename_delete_and_undo_over_the_api(tmp_path):
    c = client(tmp_path)
    old = "classes/36200-reasoning-with-data/contingency-tables"
    new = c.post("/api/rename", json={"kind": "note", "id": old, "name": "Two-way tables"}, headers=ORIGIN).json()["id"]
    assert new.endswith("two-way-tables")
    gone = c.post("/api/delete", json={"target": new}, headers=ORIGIN).json()
    assert c.get("/api/note", params={"id": new}).status_code == 404
    c.post("/api/restore", json={"trash": gone["trash"]}, headers=ORIGIN)
    assert c.get("/api/note", params={"id": new}).status_code == 200


def test_delete_from_another_website_is_refused(tmp_path):
    c = client(tmp_path)
    r = c.post("/api/delete", json={"target": "classes/36200-reasoning-with-data/contingency-tables"},
               headers={"origin": "https://evil.example"})
    assert r.status_code == 403
