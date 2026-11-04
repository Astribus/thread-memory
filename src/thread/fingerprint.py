"""Learned observations about how the user thinks, writes and works.

Stored as editable notes under `fingerprint/`, one line per observation:

    - Wants the blunt answer first, no sugar-coating · prefers · seen: 2026-09-20, 2026-10-02 · source: chat (correction)

Observations are scored by recency-weighted sightings with a per-category half-life (14–120 days).
The ranked summary is written to `fingerprint/how-i-work.md` and included in the server instructions.
"""

import json
import re
import shutil
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path

from thread.brain import TRASH, Brain, loose, slugify
from thread.voice import Voice

FOLDER = "fingerprint"
CATEGORIES = {
    "thinking": ("How I think", "How you reason, decide, and approach problems."),
    "voice": ("How I write", "Your tone, phrasing, and length, so drafts sound like you."),
    "learning": ("How I learn", "What you already know, what's new to you, and the explanations that click."),
    "working": ("Working with me", "How you want help: when to ask vs. act, reply length, corrections, pet peeves."),
    "taste": ("What I like", "Tools, aesthetics, and taste in general."),
    "patterns": ("My patterns", "Where you get stuck, what you forget, what reliably works."),
    "now": ("Right now", "Current goals, deadlines, projects, and people. Fades fast."),
}
HALF_LIVES = {"voice": 120, "thinking": 90, "learning": 60, "working": 90,
              "taste": 60, "patterns": 45, "now": 14}  # days
# Legacy category names, still accepted.
ALIASES = {"writing": "voice", "preferences": "taste", "life": "now"}
# Renamed notes: old filename -> current category.
RENAMED = {"my-life-right-now": "now"}
KINDS = {"prefers", "dislikes", "pattern", "struggles", "works", "style", "goal", "fact"}
LINE_RE = re.compile(r"^- (?P<text>.+?) · (?P<kind>[a-z]+) · seen: (?P<seen>[0-9, -]+?) · source: (?P<source>.+)$")


class Fingerprint:
    def __init__(self, brain: Brain, today: date | None = None):
        self.brain = brain
        self._today = today
        self._migrate()

    @property
    def today(self) -> date:
        return self._today or date.today()

    def _migrate(self) -> None:
        """Move lines from notes whose title changed into the new note, then trash the old one (undoable)."""
        for old, cat in RENAMED.items():
            old_path = self.brain._folder(FOLDER) / f"{old}.md"
            if not old_path.exists():
                continue
            lines = [line.strip() for line in old_path.read_text().splitlines() if LINE_RE.match(line.strip())]
            new_path = self._ensure_note(cat)
            current = [self._line(o) for o in self.observations(cat)]
            self._rewrite(new_path, current + [line for line in lines if line not in current])
            self.brain.delete(old_path.relative_to(self.brain.root).with_suffix("").as_posix())
            self.refresh_summary()

    @staticmethod
    def category(name: str) -> str:
        """The current category key for a name, accepting the old names too."""
        key = ALIASES.get(name, name)
        if key not in CATEGORIES:
            raise ValueError(f"category must be one of: {', '.join(CATEGORIES)}")
        return key

    # ---------- reading ----------
    def _path(self, category: str) -> Path:
        return self.brain._folder(FOLDER) / f"{slugify(CATEGORIES[category][0])}.md"

    def observations(self, category: str | None = None) -> list[dict]:
        found = []
        for cat in ([self.category(category)] if category else CATEGORIES):
            path = self._path(cat)
            if not path.exists():
                continue
            for line in path.read_text().splitlines():
                m = LINE_RE.match(line.strip())
                if not m:
                    continue
                seen = sorted(d.strip() for d in m["seen"].split(",") if d.strip())
                found.append({"category": cat, "text": m["text"], "kind": m["kind"], "seen": seen,
                              "source": m["source"], "score": self.score(seen, cat)})
        return found

    def score(self, seen: list[str], category: str) -> float:
        """Each sighting counts 1 today and half as much after one half-life (e.g. 14 days for "now")."""
        half_life = HALF_LIVES[category]
        total = 0.0
        for d in seen:
            age = (self.today - date.fromisoformat(d)).days
            total += 0.5 ** (max(age, 0) / half_life)
        return total

    # ---------- writing ----------
    def _ensure_note(self, category: str) -> Path:
        path = self._path(category)
        if not path.exists():
            title, about = CATEGORIES[category]
            self.brain.write(title, f"{about}\n\nThread adds a line whenever it notices something new. "
                             "Edit or delete any line; Thread will respect it.",
                             folder=FOLDER, tags=["fingerprint", category])
        return path

    def _rewrite(self, path: Path, new_lines: list[str]) -> None:
        """Keep the note's header and 'Part of' link; replace the observation lines."""
        kept = [line for line in path.read_text().splitlines() if not LINE_RE.match(line.strip())]
        try:
            at = next(i for i, line in enumerate(kept) if line.startswith("Part of "))
        except StopIteration:
            at = len(kept)
        while at > 0 and kept[at - 1] == "":
            at -= 1
        body = kept[:at] + [""] + new_lines + ([""] if new_lines else []) + kept[at:]
        path.write_text("\n".join(body).rstrip("\n") + "\n")

    @staticmethod
    def _line(obs: dict) -> str:
        return f"- {obs['text']} · {obs['kind']} · seen: {', '.join(obs['seen'])} · source: {obs['source']}"

    def learn(self, category: str, observation: str, kind: str, source: str = "chat") -> str:
        """Record an observation. If it's (nearly) already there, add today's sighting instead of a duplicate."""
        category = self.category(category)
        if kind not in KINDS:
            raise ValueError(f"kind must be one of: {', '.join(sorted(KINDS))}")
        text = " ".join(observation.split()).replace(" · ", " - ")
        if not text:
            raise ValueError("observation is empty")
        path = self._ensure_note(category)
        current = self.observations(category)
        today = self.today.isoformat()
        for obs in current:
            if SequenceMatcher(None, loose(obs["text"]), loose(text)).ratio() >= 0.8:
                if today not in obs["seen"]:
                    obs["seen"].append(today)
                if source not in obs["source"]:
                    obs["source"] = f"{obs['source']}; {source}"
                self._rewrite(path, [self._line(o) for o in current])
                self.refresh_summary()
                return f"Already knew this; noted again ({len(obs['seen'])} times): {obs['text']}"
        current.append({"text": text, "kind": kind, "seen": [today], "source": source})
        self._rewrite(path, [self._line(o) for o in current])
        self.refresh_summary()
        return f"Learned ({CATEGORIES[category][0]}): {text}"

    def forget(self, observation: str) -> str:
        """Delete the ONE observation that best matches the given text (it goes to the trash)."""
        gone = self.forget_one(observation)
        return f"Forgot: {gone['text']}" if gone else "Nothing matched, so nothing was forgotten."

    def forget_one(self, observation: str) -> dict | None:
        """Trash the single closest match (substring or ≥0.8 similarity). Returns undo info, or None."""
        target = loose(observation)
        best, best_score = None, 0.0
        for o in self.observations():
            text = loose(o["text"])
            ratio = SequenceMatcher(None, text, target).ratio()
            if text == target:
                ratio = 2.0   # exact match wins
            elif not (ratio >= 0.8 or (len(target) >= 12 and target in text)):
                continue
            if ratio > best_score:
                best, best_score = o, ratio
        if best is None:
            return None
        path = self._path(best["category"])
        siblings = self.observations(best["category"])
        position = siblings.index(best)
        self._rewrite(path, [self._line(o) for o in siblings if o != best])
        stamp = "obs-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        entry = self.brain.root / TRASH / stamp
        entry.mkdir(parents=True)
        (entry / "forgotten-observation.json").write_text(json.dumps(
            {"category": best["category"], "line": self._line(best), "position": position}))
        self.refresh_summary()
        return {"trash": stamp, "text": best["text"], "category": best["category"]}

    def restore(self, stamp: str) -> str:
        """Undo a forget: put the observation back in its note, exactly as it was."""
        if not re.fullmatch(r"obs-[\d-]+", stamp):
            raise ValueError("Unknown trash entry")
        entry = self.brain.root / TRASH / stamp
        try:
            saved = json.loads((entry / "forgotten-observation.json").read_text())
        except FileNotFoundError:
            raise FileNotFoundError("Nothing to restore") from None
        m = LINE_RE.match(saved["line"])
        restored = {"category": saved["category"], "text": m["text"], "kind": m["kind"],
                    "seen": [d.strip() for d in m["seen"].split(",") if d.strip()], "source": m["source"]}
        path = self._ensure_note(saved["category"])
        current = self.observations(saved["category"])
        if not any(o["text"] == restored["text"] for o in current):
            lines = [self._line(o) for o in current]
            lines.insert(min(saved.get("position", len(lines)), len(lines)), self._line(restored))   # restore at its old position
            self._rewrite(path, lines)
        shutil.rmtree(entry)
        self.refresh_summary()
        return restored["text"]

    # ---------- summary sent to the AI ----------
    def summary(self, per_category: int = 6) -> str:
        sections = []
        for cat, (title, _) in CATEGORIES.items():
            ranked = sorted(self.observations(cat), key=lambda o: o["score"], reverse=True)[:per_category]
            if not ranked:
                continue
            lines = []
            for o in ranked:
                strength = "strong" if o["score"] >= 1.5 else "fading" if o["score"] < 0.35 else "noted"
                times = f"{len(o['seen'])}×" if len(o["seen"]) > 1 else "once"
                lines.append(f"- {o['text']} ({o['kind']}, {strength}, seen {times}, last {o['seen'][-1]})")
            sections.append(f"## {title}\n" + "\n".join(lines))
        voice = Voice(self.brain).guide()
        if voice:
            sections.insert(0, "## How I talk\n" + voice)
        return "\n\n".join(sections) or "Nothing learned yet."

    def refresh_summary(self) -> None:
        self.brain.write("How I work",
                         "Thread's current picture of you, ranked by how often and how recently each thing "
                         "came up. It updates itself; edit the notes it links to, not this one.\n\n" + self.summary()
                         + "\n\nFrom: " + ", ".join(self.brain.link(self._path(c).relative_to(self.brain.root))
                                                   for c in CATEGORIES if self._path(c).exists())
                         + (", " + self.brain.link(Voice(self.brain).path.relative_to(self.brain.root))
                            if Voice(self.brain).path.exists() else ""),
                         folder=FOLDER, tags=["fingerprint", "summary"])
