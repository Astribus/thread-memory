import pytest

from thread.brain import Brain


@pytest.fixture
def brain(tmp_path):
    b = Brain(tmp_path)
    b.remember("Contingency tables", "joint / marginal", folder="classes/36200 Reasoning with Data")
    b.remember("Exam 1 weak spots", "see below", folder="classes/36200 Reasoning with Data",
               related=["Contingency tables"])
    b.remember("Study plan", "start with [[Contingency tables]] then practice")   # plain title link
    return b


def edges(brain):
    return {(e["source"], e["target"]) for e in brain.graph()["edges"]}


def unresolved(brain):
    return [n["id"] for n in brain.graph()["nodes"] if n["unresolved"]]


def test_rename_note_updates_file_title_and_every_link(brain):
    new_id = brain.rename_note("classes/36200-reasoning-with-data/contingency-tables", "Two-way tables")
    assert new_id == "classes/36200-reasoning-with-data/two-way-tables"
    assert "# Two-way tables" in brain.read(new_id + ".md")
    weak = brain.read("classes/36200-reasoning-with-data/exam-1-weak-spots.md")
    assert "[[classes/36200-reasoning-with-data/two-way-tables|Two-way tables]]" in weak
    plan = brain.read("inbox/study-plan.md")
    assert "[[classes/36200-reasoning-with-data/two-way-tables|Two-way tables]]" in plan
    assert unresolved(brain) == []


def test_rename_folder_moves_everything_and_keeps_links(brain):
    new_path = brain.rename_folder("classes/36200-reasoning-with-data", "36-200 Stats")
    assert new_path == "classes/36-200-stats"
    hub = "classes/36-200-stats/36-200-stats"
    assert "# 36-200 Stats" in brain.read(hub + ".md")
    assert ("classes/36-200-stats/exam-1-weak-spots", "classes/36-200-stats/contingency-tables") in edges(brain)
    assert ("classes/36-200-stats/exam-1-weak-spots", hub) in edges(brain)
    assert "|36-200 Stats]]" in brain.read("classes/36-200-stats/exam-1-weak-spots.md")
    assert unresolved(brain) == []


def test_rename_refuses_collisions_and_protected_notes(brain):
    with pytest.raises(FileExistsError):
        brain.rename_note("classes/36200-reasoning-with-data/contingency-tables", "Exam 1 weak spots")
    with pytest.raises(ValueError):
        brain.rename_note("me", "Someone else")
    with pytest.raises(ValueError):
        brain.rename_note("classes/36200-reasoning-with-data/36200-reasoning-with-data", "x")


def test_delete_note_goes_to_trash_and_undo_restores_it(brain):
    note = "classes/36200-reasoning-with-data/contingency-tables"
    result = brain.delete(note)
    assert result["notes"] == 1
    ids = {n["id"] for n in brain.graph()["nodes"] if not n["unresolved"]}
    assert note not in ids                                   # gone from the map
    assert note not in [r["path"] for r in brain.search("contingency")]  # and from search
    brain.restore(result["trash"])
    assert "joint / marginal" in brain.read(note + ".md")
    assert unresolved(brain) == []


def test_delete_folder_and_undo(brain):
    result = brain.delete("classes/36200-reasoning-with-data/")
    assert result["notes"] == 3                               # 2 notes + the folder note
    assert not (brain.root / "classes" / "36200-reasoning-with-data").exists()
    brain.restore(result["trash"])
    assert (brain.root / "classes" / "36200-reasoning-with-data" / "exam-1-weak-spots.md").exists()


def test_delete_refuses_me_hub_notes_and_escapes(brain):
    for target in ["me", "classes/36200-reasoning-with-data/36200-reasoning-with-data", "../../etc"]:
        with pytest.raises(ValueError):
            brain.delete(target)


def test_trash_is_invisible_to_link_backfill(brain):
    brain.delete("classes/36200-reasoning-with-data/contingency-tables")
    assert brain.link_all() == 0
