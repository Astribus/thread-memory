import pytest

from thread.brain import Brain


@pytest.fixture
def brain(tmp_path):
    return Brain(tmp_path)


def test_tree_lists_folders_first_hides_hub_notes_and_starts_with_me(brain):
    brain.remember("Exam 1 weak spots", "x", folder="classes/36200 Reasoning with Data")
    tree = brain.tree_data()
    assert tree["children"][0] == {"type": "note", "id": "me", "name": "Me"}
    classes = tree["children"][1]
    assert classes["type"] == "folder" and classes["path"] == "classes"
    course = classes["children"][0]
    assert course["name"] == "36200 Reasoning with Data"
    assert [c["name"] for c in course["children"]] == ["Exam 1 weak spots"]  # hub not listed


def test_create_nested_folders_and_a_note_inside(brain):
    projects = brain.create_folder("", "Projects")
    thread = brain.create_folder(projects, "Thread App")
    note = brain.create_note(thread, "Launch plan", "Talk to 10 students")
    assert note == "projects/thread-app/launch-plan"
    edges = {(e["source"], e["target"]) for e in brain.graph()["edges"]}
    assert (note, "projects/thread-app/thread-app") in edges
    assert ("projects/thread-app/thread-app", "projects/projects") in edges
    assert ("projects/projects", "me") in edges


def test_create_refuses_duplicates_and_outside_paths(brain):
    brain.create_folder("", "Projects")
    with pytest.raises(FileExistsError):
        brain.create_folder("", "projects")
    brain.create_note("projects", "Plan")
    with pytest.raises(FileExistsError):
        brain.create_note("projects", "Plan")
    with pytest.raises((ValueError, FileNotFoundError)):
        brain.create_folder("../..", "evil")


def test_save_replaces_text_and_keeps_note_connected(brain):
    note = brain.create_note("", "Ideas", "first")
    brain.save(note, "# Ideas\n\nrewritten without any links")
    text = brain.read(note + ".md")
    assert "rewritten" in text and "Part of [[inbox/inbox|" in text
