import pytest

from thread.brain import Brain


@pytest.fixture
def brain(tmp_path):
    return Brain(tmp_path)


def edges(brain):
    return {(e["source"], e["target"]) for e in brain.graph()["edges"]}


def test_every_note_connects_up_to_me_through_folder_hubs(brain):
    brain.remember("Exam 1 weak spots", "contingency tables", folder="classes/36200 Reasoning with Data")
    hub = "classes/36200-reasoning-with-data/36200-reasoning-with-data"
    assert edges(brain) == {
        ("classes/36200-reasoning-with-data/exam-1-weak-spots", hub),
        (hub, "classes/classes"),
        ("classes/classes", "me"),
    }
    assert "# 36200 Reasoning with Data" in brain.read(hub + ".md")


def test_related_links_resolve_by_title_and_unknowns_stay_faded(brain):
    brain.remember("Contingency tables", "joint / marginal / conditional", folder="classes/36200")
    brain.remember("Exam 1 weak spots", "got these wrong", folder="classes/36200",
                   related=["Contingency tables", "Statistic vs parameter"])
    graph = brain.graph()
    assert ("classes/36200/exam-1-weak-spots", "classes/36200/contingency-tables") in edges(brain)
    faded = [n for n in graph["nodes"] if n["unresolved"]]
    assert [n["id"] for n in faded] == ["Statistic vs parameter"]


def test_degree_counts_both_directions_once_per_pair(brain):
    brain.remember("A", "x", related=["B"])
    brain.remember("B", "y", related=["A"])
    brain.remember("A", "again", related=["B"])  # repeat link, still one edge each way
    degree = {n["id"]: n["degree"] for n in brain.graph()["nodes"]}
    assert degree["inbox/a"] == 3  # -> B, <- B, -> inbox hub


def test_link_all_backfills_old_notes_and_is_idempotent(brain, tmp_path):
    old = tmp_path / "classes" / "21127-concepts" / "quiz-notes.md"
    old.parent.mkdir(parents=True)
    old.write_text("# Quiz notes\n\nno links yet\n")
    assert brain.link_all() == 1
    assert brain.link_all() == 0
    assert ("classes/21127-concepts/quiz-notes", "classes/21127-concepts/21127-concepts") in edges(brain)


def test_add_link_between_existing_notes(brain):
    brain.remember("Goals", "ship Thread")
    brain.remember("Thread V1", "MCP server", folder="projects")
    brain.add_link("Goals", "Thread V1")
    assert ("inbox/goals", "projects/thread-v1") in edges(brain)


def test_course_number_reuses_existing_class_folder(brain):
    brain.write("Upcoming deadlines", "quiz", folder="classes/21241 Matrices and Linear Transformations")
    path = brain.remember("Exam 2", "Oct 28", folder="classes/21-241")
    assert str(path) == "classes/21241-matrices-and-linear-transformations/exam-2.md"
    assert not (brain.root / "classes" / "21-241").exists()
