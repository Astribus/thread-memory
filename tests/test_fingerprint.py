import asyncio
from datetime import date

import pytest

from thread.brain import Brain
from thread.fingerprint import Fingerprint


@pytest.fixture
def brain(tmp_path):
    return Brain(tmp_path)


def fp(brain, day="2026-10-02"):
    return Fingerprint(brain, today=date.fromisoformat(day))


def test_learn_writes_a_visible_line_in_the_right_note(brain):
    msg = fp(brain).learn("preferences", "Wants the blunt answer first, no sugar-coating", "prefers", "chat (correction)")
    assert msg.startswith("Learned")
    note = brain.read("fingerprint/what-i-like.md")
    assert "- Wants the blunt answer first, no sugar-coating · prefers · seen: 2026-10-02 · source: chat (correction)" in note
    assert "Part of [[fingerprint/fingerprint|" in note          # on the map, linked to its folder


def test_same_thing_again_counts_instead_of_duplicating(brain):
    fp(brain, "2026-09-20").learn("preferences", "Wants the blunt answer first, no sugar-coating", "prefers")
    msg = fp(brain).learn("preferences", "wants blunt answers first - no sugarcoating", "prefers", "chat (correction)")
    assert "noted again (2 times)" in msg
    obs = fp(brain).observations("preferences")
    assert len(obs) == 1 and obs[0]["seen"] == ["2026-09-20", "2026-10-02"]


def test_repeated_and_recent_things_rank_first_and_old_ones_fade(brain):
    f = fp(brain, "2026-06-01")
    f.learn("patterns", "Skips checking small examples before writing a proof", "struggles")
    f2 = fp(brain, "2026-09-25")
    f2.learn("patterns", "Explains best after a concrete worked example", "works")
    f3 = fp(brain)
    f3.learn("patterns", "Explains best after a concrete worked example", "works")
    summary = f3.summary()
    assert summary.index("worked example") < summary.index("small examples")
    assert "strong" in summary.split("worked example")[1].split("\n")[0]
    assert "fading" in summary.split("small examples")[1].split("\n")[0]


def test_summary_note_is_written_and_linked(brain):
    fp(brain).learn("thinking", "Thinks in big visions first, then narrows down", "pattern")
    note = brain.read("fingerprint/how-i-work.md")
    assert "Thinks in big visions" in note and "[[fingerprint/how-i-think|How I think]]" in note


def test_forget_removes_it_from_the_note_and_the_summary(brain):
    f = fp(brain)
    f.learn("preferences", "Likes dark mode", "prefers")
    f.learn("preferences", "Wants the blunt answer first", "prefers")
    assert "Forgot: Likes dark mode" == f.forget("likes dark mode")
    assert "dark mode" not in brain.read("fingerprint/what-i-like.md")
    assert "dark mode" not in brain.read("fingerprint/how-i-work.md")
    assert "Nothing matched" in f.forget("something never said")


def test_user_edits_in_the_note_are_respected(brain):
    f = fp(brain)
    f.learn("writing", "Writes in short lowercase messages", "style")
    path = brain.root / "fingerprint" / "how-i-write.md"
    path.write_text(path.read_text().replace("short lowercase messages", "short casual messages, often lowercase"))
    assert fp(brain).observations("writing")[0]["text"] == "Writes in short casual messages, often lowercase"


def test_bad_input_is_rejected_clearly(brain):
    f = fp(brain)
    with pytest.raises(ValueError):
        f.learn("hobbies", "x", "prefers")
    with pytest.raises(ValueError):
        f.learn("preferences", "x", "loves")


def test_ai_tools_and_instructions(brain):
    from thread.server import build_server
    fp(brain).learn("preferences", "Wants the blunt answer first", "prefers")
    server = build_server(brain)
    assert "Wants the blunt answer first" in server.instructions        # every AI sees it at the start
    result = str(asyncio.run(server.call_tool("learn_about_user", {
        "category": "life", "observation": "Building Thread, a memory you own", "kind": "goal"})))
    assert "Learned" in result
    assert "Building Thread" in str(asyncio.run(server.call_tool("how_user_works", {})))


def test_each_category_fades_at_its_own_speed(brain):
    f = fp(brain, "2026-09-02")                       # 30 days before "today"
    f.learn("now", "Midterm on Friday", "fact")
    f.learn("voice", "Writes fast, casual lowercase messages", "style")
    today = fp(brain)
    now = today.observations("now")[0]["score"]
    voice = today.observations("voice")[0]["score"]
    assert round(now, 2) == round(0.5 ** (30 / 14), 2)      # deadlines go stale fast
    assert round(voice, 2) == round(0.5 ** (30 / 120), 2)   # voice decays slowly
    assert voice > 0.8 > 0.35 > now


def test_old_category_names_still_work(brain):
    f = fp(brain)
    assert f.learn("preferences", "Likes dark mode", "prefers").startswith("Learned (What I like)")
    assert f.learn("writing", "Short messages", "style").startswith("Learned (How I write)")
    assert f.learn("life", "Building Thread", "goal").startswith("Learned (Right now)")
    assert f.observations("taste") == f.observations("preferences")


def test_old_life_note_moves_into_right_now(brain):
    old = brain.root / "fingerprint" / "my-life-right-now.md"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text("# My life right now\n\n- Building Thread · goal · seen: 2026-09-24, 2026-10-02 · source: chat\n")
    f = fp(brain)
    assert not old.exists()                                   # trashed, so Undo can bring it back
    obs = f.observations("now")
    assert obs[0]["text"] == "Building Thread" and obs[0]["seen"] == ["2026-09-24", "2026-10-02"]
    assert "Building Thread" in brain.read("fingerprint/how-i-work.md")


def test_forget_removes_only_the_single_best_match(tmp_path):
    from thread.brain import Brain
    from thread.fingerprint import Fingerprint
    fp = Fingerprint(Brain(tmp_path))
    fp.learn("taste", "Likes dark mode", "prefers")
    fp.learn("taste", "Likes dark mode in the editor but not on the map", "prefers")
    fp.learn("working", "Likes dark mode late at night when coding", "pattern")
    assert fp.forget("Likes dark mode") == "Forgot: Likes dark mode"
    left = {o["text"] for o in fp.observations()}
    assert left == {"Likes dark mode in the editor but not on the map", "Likes dark mode late at night when coding"}


def test_forgotten_observation_goes_to_trash_and_undo_restores_it(tmp_path):
    from thread.brain import TRASH, Brain
    from thread.fingerprint import Fingerprint
    brain = Brain(tmp_path)
    fp = Fingerprint(brain)
    fp.learn("taste", "Wants the blunt answer first", "prefers")
    fp.learn("taste", "Wants the blunt answer first", "prefers")          # seen twice
    before = fp.observations()[0]
    gone = fp.forget_one("Wants the blunt answer first")
    assert fp.observations() == [] and (tmp_path / TRASH / gone["trash"]).is_dir()
    assert "blunt" not in fp.summary()
    assert fp.restore(gone["trash"]) == "Wants the blunt answer first"
    after = fp.observations()[0]
    assert (after["text"], after["seen"], after["source"]) == (before["text"], before["seen"], before["source"])
    assert not (tmp_path / TRASH / gone["trash"]).exists()
    assert fp.forget_one("something never learned") is None
