import pytest

from thread.brain import Brain


@pytest.fixture
def brain(tmp_path):
    return Brain(tmp_path)


def test_new_brain_gets_a_blank_profile_with_no_one_else_s_rules(brain):
    assert "# Who I am" in brain.profile() and "privacy: strict" in brain.profile()
    assert "Save anything important" not in brain.profile()   # no built-in personal rules


def test_remember_then_search(brain):
    brain.remember("15-122 exam", "Midterm 1 is Oct 14 in DH 2210", folder="classes/15-122")
    hits = brain.search("when is my 15-122 midterm")
    assert hits[0]["path"] == "classes/15-122/15-122-exam.md"
    assert "Oct 14" in hits[0]["snippet"]


def test_remember_same_title_appends(brain):
    brain.remember("Goals", "Ship Thread V1")
    brain.remember("Goals", "Talk to 10 students")
    text = brain.read("inbox/goals.md")
    assert "Ship Thread V1" in text and "Talk to 10 students" in text


def test_folder_cannot_escape_brain(brain, tmp_path):
    path = brain.remember("x", "y", folder="../../etc")
    assert (tmp_path / path).resolve().is_relative_to(tmp_path)


def test_read_rejects_outside_paths(brain):
    with pytest.raises(ValueError):
        brain.read("../../../etc/passwd")


def test_saving_mode_defaults_to_unasked_and_can_be_set(tmp_path):
    import pytest
    brain = Brain(tmp_path)
    assert brain.saving() is None                 # never asked yet
    assert brain.set_saving("Important") == "important" and brain.saving() == "important"
    assert brain.privacy() == "strict"            # the privacy line is untouched
    brain.set_privacy("light")
    assert brain.saving() == "important" and brain.privacy() == "light"
    with pytest.raises(ValueError):
        brain.set_saving("sometimes")


def test_new_chats_get_the_saving_rule_that_matches_the_users_choice(tmp_path):
    from thread.server import build_server
    brain = Brain(tmp_path)
    live = lambda: build_server(brain)._lowlevel_server.instructions
    first = live()
    assert "FIRST, before anything else" in first and "Save as you go" in first   # ask once, save everything meanwhile
    brain.set_saving("everything")
    assert "FIRST, before anything else" not in live() and "Save as you go" in live()
    brain.set_saving("ask")
    assert "Save only when the user asks" in live() and "Save as you go" not in live()


def test_a_class_never_splits_into_two_folders_whichever_name_comes_first(tmp_path):
    brain = Brain(tmp_path)
    brain.remember("Office hours", "Thu 3pm", folder="classes/36-200")   # short name first
    brain.write("Upcoming deadlines", "- HW 7", folder="classes/36-200 Reasoning with Data")   # full Canvas name later
    classes = sorted(p.name for p in (tmp_path / "classes").iterdir() if p.is_dir())
    assert classes == ["36-200"]
    brain2 = Brain(tmp_path / "b2")
    brain2.write("Upcoming deadlines", "- Exam 2", folder="classes/21241 Matrices and Linear Transformations")
    brain2.remember("Exam 2 plan", "ch 3-4", folder="classes/21-241")
    assert sorted(p.name for p in (tmp_path / "b2" / "classes").iterdir() if p.is_dir()) == ["21241-matrices-and-linear-transformations"]
    assert brain2.class_slug("21-241 Matrices and Linear Transformations") == "21241-matrices-and-linear-transformations"
