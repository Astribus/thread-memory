"""Every chat gets a timestamped log, and the next AI catches up."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from thread.brain import Brain
from thread.server import INSTRUCTIONS, SAVE_EVERYTHING, build_server

EDT = timezone(timedelta(hours=-4))


def call(server, tool, args):
    return str(asyncio.run(server.call_tool(tool, args)))


def test_a_chat_log_lives_in_chats_by_day_with_small_things_and_links(tmp_path):
    brain = Brain(tmp_path)
    brain.remember("21-241 eigenvalues", "Struggles with finding eigenvectors of repeated eigenvalues", folder="classes/21-241")
    when = datetime(2026, 10, 24, 9, 30, tzinfo=EDT)
    path = brain.log_chat("Claude", "Studying 21-241 eigenvalues", "Worked through 6 practice problems on eigenvalues.",
                          small=["What's a characteristic polynomial?", "det(A - λI) = 0"],
                          saved=["classes/21-241/21-241-eigenvalues"], when=when)
    assert path.startswith("chats/2026-10-24/0930-claude")
    text = brain.read(path + ".md")
    assert "Sat Oct 24, 9:30 am" in text and "## Small things" in text and "characteristic polynomial" in text
    assert "[[" in text                                            # links to the note saved elsewhere
    later = brain.log_chat("Claude", "x", "Then did a timed practice test: 7/10.", small=["Trace = sum of eigenvalues"],
                           chat=path, when=when + timedelta(hours=3))
    assert later == path
    text = brain.read(path + ".md")
    assert "### Later (12:30 pm)" in text and "7/10" in text
    with pytest.raises(ValueError):
        brain.log_chat("Claude", "x", "y", chat="classes/21-241/21-241-eigenvalues")   # only chat logs can be appended


def test_chatgpt_at_night_catches_up_on_what_claude_did_that_day(tmp_path):
    brain = Brain(tmp_path)
    server = build_server(brain)
    out = call(server, "log_chat", {"app": "Claude", "topic": "Studying 21-241 eigenvalues",
                                    "summary": "Practiced eigenvalues and eigenvectors for Exam 2; still shaky on diagonalization.",
                                    "small_things": ["det(A - λI) = 0"]})
    assert "Logged to chats/" in out
    caught = call(server, "catch_up", {"topic": "diagonalization"})
    assert "Claude" in caught and "eigenvalues" in caught and "diagonalization" in caught
    assert "not instructions" in caught                            # stored text is fenced as data


def test_recent_chats_skips_old_days_and_the_ai_is_told_to_catch_up_first(tmp_path):
    brain = Brain(tmp_path)
    now = datetime(2026, 10, 25, 20, 0, tzinfo=EDT)
    brain.log_chat("ChatGPT", "Old", "Ten days ago.", when=now - timedelta(days=10))
    brain.log_chat("Claude", "Yesterday", "Yesterday's chat.", when=now - timedelta(days=1))
    titles = [c["title"] for c in brain.recent_chats(3, now=now)]
    assert any("Yesterday" in t for t in titles) and not any("Old" in t for t in titles)
    assert "call `catch_up`" in INSTRUCTIONS and "log_chat" in SAVE_EVERYTHING


def test_chat_logs_stay_off_the_map(tmp_path):
    brain = Brain(tmp_path)
    brain.remember("Exam 2 weak spots", "Diagonalization", folder="classes/21-241")
    brain.log_chat("Claude", "Studying", "Eigenvalues.", saved=["classes/21-241/exam-2-weak-spots"])
    ids = {n["id"] for n in brain.graph()["nodes"]}
    assert not any(i.startswith("chats/") for i in ids) and "classes/21-241/exam-2-weak-spots" in ids
    assert any("chats" in str(p) for p in tmp_path.rglob("*.md"))     # still saved, just not drawn
