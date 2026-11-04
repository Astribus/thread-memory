"""Scale regressions: a busy user's memory must stay fast. Small fixtures, generous budgets, so these run
in seconds anywhere; run scale-lab/ for the full 300/1,000/3,000-note numbers."""

import random
import re
import time
from pathlib import Path

from starlette.testclient import TestClient

from thread.brain import LINK_RE, TREE_FULL_LIMIT, Brain, loose
from thread.home import build_app


def make_brain(root: Path, n: int, seed: int = 7) -> Brain:
    """n notes in a few folders, linked by path, by title (Obsidian style), and to notes that don't exist."""
    rng = random.Random(seed)
    brain = Brain(root)
    made = []                                          # (path without .md, title)
    for i in range(n):
        folder = rng.choice(["classes/21-127", "classes/21-241", "work", "people", "ideas"])
        title = f"Note {i} {rng.choice(['exam', 'meeting', 'idea', 'plan'])}"
        links = [f"[[{t}]]" if rng.random() < 0.4 else f"[[{p}|{t}]]"            # by title (Obsidian) or by path
                 for p, t in rng.sample(made, min(len(made), rng.randint(0, 4)))]
        if rng.random() < 0.1:
            links.append(f"[[Never written {i}]]")
        rel = brain.write(title, "Some text. " + " ".join(links), folder=folder)
        made.append((Path(rel).with_suffix("").as_posix(), title))
    return brain


def reference_graph(brain: Brain) -> dict:
    """The original algorithm (scan every note to resolve every link), kept here as the oracle."""
    nodes, edges = {}, set()
    for path in brain._notes():
        rel = path.relative_to(brain.root)
        created = re.search(r"^date:\s*(\d{4}-\d{2}-\d{2})", path.read_text(), re.MULTILINE)
        nodes[rel.with_suffix("").as_posix()] = {"id": rel.with_suffix("").as_posix(), "title": brain.display_title(rel),
                                                 "folder": rel.parent.as_posix(), "unresolved": False,
                                                 "date": created.group(1) if created else None}

    def resolve(ref):
        ref = ref.strip().removesuffix(".md")
        path = brain.root / (ref + ".md")
        if path.exists():
            return path.relative_to(brain.root)
        want = loose(ref)
        for p in brain._notes():
            rel = p.relative_to(brain.root)
            if want and want in (p.stem, loose(brain.title(rel))):
                return rel
        return None
    for path in brain._notes():
        source = path.relative_to(brain.root).with_suffix("").as_posix()
        for m in LINK_RE.finditer(path.read_text()):
            t = resolve(m.group(1))
            target = t.with_suffix("").as_posix() if t else m.group(1).strip()
            if target not in nodes:
                nodes[target] = {"id": target, "title": target, "folder": "", "unresolved": True, "date": None}
            if target != source:
                edges.add((source, target))
    degree = {k: 0 for k in nodes}
    for a, b in edges:
        degree[a] += 1; degree[b] += 1
    for v in nodes.values():
        v["degree"] = degree[v["id"]]
    return {"nodes": list(nodes.values()), "edges": [{"source": a, "target": b} for a, b in sorted(edges)]}


def test_fast_graph_gives_exactly_the_same_map_as_the_old_scan(tmp_path):
    brain = make_brain(tmp_path, 120)
    key = lambda g: (sorted((n["id"], n["title"], n["folder"], n["unresolved"], n["date"], n["degree"]) for n in g["nodes"]),
                     sorted((e["source"], e["target"]) for e in g["edges"]))
    assert key(brain.graph()) == key(reference_graph(brain))


def test_graph_is_cached_until_a_note_changes(tmp_path):
    brain = make_brain(tmp_path, 400)
    t = time.perf_counter(); first = brain.graph(); cold = time.perf_counter() - t
    assert cold < 2.0, f"cold graph {cold:.2f}s for 400 notes (was ~2 s for 300 before the fix)"
    t = time.perf_counter(); again = brain.graph(); warm = time.perf_counter() - t
    assert again is first and warm < 0.25            # nothing changed: no rebuild
    brain.remember("Fresh note", "links to [[Note 3 exam]]", folder="ideas")
    after = brain.graph()
    assert after is not first and any(n["title"] == "Fresh note" for n in after["nodes"])


def test_graph_endpoint_answers_304_when_nothing_changed(tmp_path):
    brain = make_brain(tmp_path, 30)
    client = TestClient(build_app(brain))
    r = client.get("/api/graph")
    assert r.status_code == 200 and r.headers["etag"]
    assert client.get("/api/graph", headers={"If-None-Match": r.headers["etag"]}).status_code == 304
    brain.remember("Another", "text", folder="work")
    assert client.get("/api/graph", headers={"If-None-Match": r.headers["etag"]}).status_code == 200


def test_resolve_by_title_still_works_and_is_quick(tmp_path):
    brain = make_brain(tmp_path, 300)
    rel = brain.resolve("Note 42 " + re.search(r"Note 42 (\w+)", "\n".join(n["title"] for n in brain.graph()["nodes"])).group(1))
    assert rel and rel.name.startswith("note-42")
    t = time.perf_counter()
    for _ in range(20):
        brain.resolve("no such note anywhere")
    assert (time.perf_counter() - t) / 20 < 0.05   # 50 ms budget per lookup


def test_list_memory_summarises_a_big_memory_and_lists_one_folder_in_full(tmp_path):
    small = make_brain(tmp_path / "s", 20)
    assert "Too many to list" not in small.tree()
    big = make_brain(tmp_path / "b", TREE_FULL_LIMIT + 40)
    out = big.tree()
    assert "Too many to list" in out and len(out) < 5000 and "classes/21-127/" in out
    folder = big.tree("classes/21-127")
    assert "Too many" not in folder and ".md" in folder


def test_sidebar_tree_uses_the_index_and_matches_titles(tmp_path):
    brain = make_brain(tmp_path, 80)
    names = []
    def walk(items):
        for it in items:
            names.append(it["name"]); walk(it.get("children", []))
    walk(brain.tree_data()["children"])
    assert any(n.startswith("Note 7 ") for n in names)
