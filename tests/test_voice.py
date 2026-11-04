import asyncio

import pytest

from thread.brain import Brain
from thread.voice import Voice, clean


@pytest.fixture
def brain(tmp_path):
    return Brain(tmp_path)


SAMPLES = [
    ("2026-09-24", "ok wait so does it make sense to call the folder where everything lives the notebook?"),
    ("2026-09-25", "bro im not trying to start small i already have the big idea in my head"),
    ("2026-10-01", "ok lets continue"),
    ("2026-10-02", "nah we gotta move faster and actually lock in this week"),
    ("2026-10-02", "yea sure"),
    ("2026-10-02", "ok i gave the other agent access so pass it whatever you cant finish"),
]


def test_private_or_code_messages_are_never_kept():
    assert clean("my password is hunter2 dont tell anyone") is None
    assert clean("i got 61/100 on the exam") is None
    assert clean("check https://example.com for it") is None
    assert clean("run `uv sync` then test") is None
    assert clean("ok") is None                       # one word says too little
    assert clean("yea sure") == "yea sure"
    assert clean("also my advisor is Dana Whitfield so email her") is None      # someone else's name
    assert clean("is an A not possible for 127 anymore") is None                    # grade worries
    assert clean("ok lets continue on thread") == "ok lets continue on thread"


def test_style_is_learned_from_real_messages(brain):
    v = Voice(brain)
    v.add_many(SAMPLES)
    s = v.style()
    assert s["count"] == 6
    assert s["lowercase_start"] >= 80
    assert s["skips_apostrophes"]
    assert "ok" in s["slang"]
    guide = v.guide()
    assert "Mostly lowercase" in guide and "Skips apostrophes" in guide
    assert '"yea sure"' in guide                     # real examples are handed to the AI


def test_voice_note_is_visible_and_capped(brain):
    v = Voice(brain)
    v.add_many([(f"2026-09-{d:02d}", f"message number {d} from me ok") for d in range(1, 30)] * 4)
    note = brain.read("fingerprint/my-voice.md")
    assert note.count('- "') <= 80
    assert "Part of [[fingerprint/fingerprint|" in note


def test_forget_removes_examples(brain):
    v = Voice(brain)
    v.add_many(SAMPLES)
    assert v.forget("notebook") == "Removed 1 example(s)."
    assert all("notebook" not in s["text"] for s in v.samples())


def test_every_ai_gets_the_voice_and_can_add_to_it(brain):
    from thread.fingerprint import Fingerprint
    from thread.server import build_server
    Voice(brain).add_many(SAMPLES)
    Fingerprint(brain).refresh_summary()
    server = build_server(brain)
    heard = len(Voice(brain).samples())
    assert heard < 10
    # with only a few samples the AI talks naturally and doesn't copy slang yet
    assert "Talk like them" not in server.instructions and f"only\n  seen {heard}" in server.instructions
    for i in range(10 - heard):
        Voice(brain).add(f"yo quick question number {i} about the hw")
    assert "How I talk" in server.instructions and "Talk like them" in server.instructions
    result = str(asyncio.run(server.call_tool("note_user_voice", {"message": "aight bet lets build it"})))
    assert "Saved to your voice" in result
    assert "aight bet lets build it" in brain.read("fingerprint/my-voice.md")


def test_light_privacy_keeps_personal_things_but_never_secrets(brain):
    brain.set_privacy("light")
    assert brain.privacy() == "light"
    v = Voice(brain)
    assert "Saved" in v.add("also my advisor is Dana Whitfield so email her")
    assert "Saved" in v.add("is an A not possible for 127 anymore")
    assert "Skipped" in v.add("my password is hunter2 dont tell anyone")
    assert "Skipped" in v.add("card is 4111 1111 1111 1111 use that")
    brain.set_privacy("strict")
    assert "Skipped" in v.add("my mom said hi to everyone today")


def test_forgotten_stays_forgotten_even_if_imported_again(brain):
    v = Voice(brain)
    v.add_many(SAMPLES)
    v.forget("notebook")
    v.add_many(SAMPLES)                       # the same history imported again
    assert all("notebook" not in s["text"] for s in v.samples())
    assert "notebook" not in (brain.root / ".thread" / "forgotten-voice.json").read_text()   # only hashes


def test_privacy_tool_and_instructions(brain):
    from thread.server import build_server
    assert "Privacy is STRICT" in build_server(brain).instructions
    result = str(asyncio.run(build_server(brain).call_tool("set_privacy", {"level": "light"})))
    assert "Privacy set to light" in result
    assert "Privacy is LIGHT" in build_server(brain).instructions
    assert "must be one of" in str(asyncio.run(build_server(brain).call_tool("set_privacy", {"level": "medium"})))


def test_everyday_cs_talk_is_kept_on_strict():
    # block private phrases, not CS words that happen to look like them.
    for msg in ("ok wait so pointers are just addresses?? lol", "bro i need to drop the table and redo it",
                "my token expired again lol", "this test case keeps failing lmao", "store it in an array lol"):
        assert clean(msg) == msg
    for msg in ("i failed the exam bro", "thinking about dropping the class ngl", "i got a C on it", "my mom called"):
        assert clean(msg) is None
