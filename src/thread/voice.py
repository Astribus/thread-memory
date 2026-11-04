"""Voice samples: the user's own messages, stored verbatim in `fingerprint/my-voice.md`.

A style guide (length, casing, punctuation, slang) is derived from the samples and sent to the AI
along with a few examples.
"""

import hashlib
import json
import re
import statistics
from datetime import date

from difflib import SequenceMatcher

from thread.brain import Brain

TITLE = "My voice"
FOLDER = "fingerprint"
MAX_SAMPLES = 80
VOICE_MIN = 10   # samples needed before the AI mirrors casing and slang
LINE_RE = re.compile(r'^- "(?P<text>.+)" · (?P<date>\d{4}-\d{2}-\d{2})$')

# Blocked at every privacy level.
SECRETS = re.compile(
    r"password|passcode|passwd|api[ _-]?key|secret key|access token|\bsk-[A-Za-z0-9]{8,}|"
    r"\b(?:\d[ -]?){13,16}\b|\b\d{3}-\d{2}-\d{4}\b|routing number|account number",
    re.IGNORECASE,
)

# Blocked in strict mode. Matched as phrases so technical talk ("my token expired") isn't caught.
SENSITIVE = re.compile(
    r"https?://|www\.|@\w+\.\w+|\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b|\$\s?\d|\b\d+\s?/\s?\d+\b|"
    r"password|passcode|\bssn\b|social security|credit card|bank account|\bdebt\b|\bloans?\b|salary|"
    r"\bmom\b|\bdad\b|parents?\b|\bfamily\b|girlfriend|boyfriend|"
    r"\bgrades?\b|\bgpa\b|therapy|therapist|depress|anxiety|medical|doctor|"
    r"my address|home address|where i live|api key|\biq\b|my score|academic probation|my advisor|"
    r"\bfail(ed|ing)? (the|my|this|that|an?) (exam|test|midterm|final|quiz|class|course)|"
    r"\bdrop(ping|ped)? (out|the class|this class|that class|a class|the course)|withdr[ae]w(ing|n)? from|"
    r"\bgot an? [a-f][+-]?(?![\w+-])|\ban a\+?(?![\w+-])|\bgot a \d+|"
    r"\bexam\b.*\b(bad|wrong|fail)",
    re.IGNORECASE,
)
CASUAL = ["bro", "lmao", "lol", "ngl", "fr", "gonna", "wanna", "gotta", "ok", "okay", "yea", "yeah", "nah",
          "damn", "fuck", "fucking", "shit", "lowkey", "tbh", "idk", "dude", "holy", "lock in", "aight", "ight",
          "bet", "wait", "yo", "kinda", "sorta", "like"]
SWEARS = {"fuck", "fucking", "shit", "damn", "hell", "ass"}
NO_APOSTROPHE = re.compile(r"\b(dont|im|cant|wont|didnt|isnt|doesnt|ive|thats|whats|youre|theyre|wasnt|lets|ill|id)\b")
WITH_APOSTROPHE = re.compile(r"\b(don't|i'm|can't|won't|didn't|isn't|doesn't|i've|that's|what's|it's|you're|they're|wasn't)\b", re.I)


# Full names of other people.
FULL_NAME = re.compile(r"(?<![.!?]\s)(?<!^)\b[A-Z][a-z]+ [A-Z][a-z]+\b")


def clean(text: str, privacy: str = "strict") -> str | None:
    """Normalised sample, or None if it's too long, looks like code, or fails the privacy filters."""
    text = " ".join(text.split())
    words = text.split()
    if not 2 <= len(words) <= 45 or len(text) > 300:   # short messages are still voice
        return None
    if text.startswith(("<", "/", "!", "```")) or "`" in text or "{" in text or SECRETS.search(text):
        return None
    if privacy == "strict" and (SENSITIVE.search(text) or FULL_NAME.search(text)):
        return None
    return text.replace('"', "'")


def _fingerprint_of(text: str) -> str:
    return hashlib.sha256(" ".join(text.lower().split()).encode()).hexdigest()


class Voice:
    def __init__(self, brain: Brain):
        self.brain = brain

    # Forgotten samples stay forgotten on re-import; only their hashes are kept.
    @property
    def _forgotten_file(self):
        return self.brain.root / ".thread" / "forgotten-voice.json"

    def _forgotten(self) -> set[str]:
        try:
            return set(json.loads(self._forgotten_file.read_text()))
        except (FileNotFoundError, json.JSONDecodeError):
            return set()

    def _remember_forgotten(self, texts: list[str]) -> None:
        hashes = self._forgotten() | {_fingerprint_of(t) for t in texts}
        self._forgotten_file.parent.mkdir(parents=True, exist_ok=True)
        self._forgotten_file.write_text(json.dumps(sorted(hashes)))

    def _usable(self, message: str) -> str | None:
        text = clean(message, self.brain.privacy())
        return None if text is None or _fingerprint_of(text) in self._forgotten() else text

    @property
    def path(self):
        return self.brain.root / FOLDER / "my-voice.md"

    def samples(self) -> list[dict]:
        if not self.path.exists():
            return []
        found = []
        for line in self.path.read_text().splitlines():
            m = LINE_RE.match(line.strip())
            if m:
                found.append({"text": m["text"], "date": m["date"]})
        return found

    def add(self, message: str, when: date | None = None) -> str:
        text = self._usable(message)
        if text is None:
            return "Skipped (too short/long, code, a secret, previously forgotten, or private on strict privacy)."
        samples = self.samples()
        if any(s["text"] == text for s in samples):
            return "Already saved."
        samples.append({"text": text, "date": (when or date.today()).isoformat()})
        samples = sorted(samples, key=lambda s: s["date"])[-MAX_SAMPLES:]   # keep the newest
        self._write(samples)
        return f"Saved to your voice ({len(samples)} examples)."

    def add_many(self, messages: list[tuple[str, str]]) -> int:
        """Bulk import (date, text) pairs, e.g. from past chats. Returns how many were kept."""
        samples = self.samples()
        have = {s["text"] for s in samples}
        for day, message in messages:
            text = self._usable(message)
            if text and text not in have and not any(
                    SequenceMatcher(None, text, other).ratio() >= 0.85 for other in list(have)[-200:]):
                samples.append({"text": text, "date": day})
                have.add(text)
        samples = sorted(samples, key=lambda s: s["date"])[-MAX_SAMPLES:]
        self._write(samples)
        return len(samples)

    def forget(self, text: str) -> str:
        samples = self.samples()
        keep = [s for s in samples if text.lower() not in s["text"].lower()]
        self._remember_forgotten([s["text"] for s in samples if s not in keep])
        self._write(keep)
        return f"Removed {len(samples) - len(keep)} example(s)."

    # ---------- style ----------
    def style(self) -> dict:
        texts = [s["text"] for s in self.samples()]
        if not texts:
            return {}
        lower = [t for t in texts]
        joined = " ".join(lower).lower()
        words = [len(t.split()) for t in texts]
        slang = []
        for term in CASUAL:
            hits = len(re.findall(rf"\b{re.escape(term)}\b", joined))
            if hits >= 2:
                slang.append((hits, term))
        no_apos, with_apos = len(NO_APOSTROPHE.findall(joined)), len(WITH_APOSTROPHE.findall(" ".join(texts)))
        return {
            "count": len(texts),
            "median_words": int(statistics.median(words)),
            "lowercase_start": round(100 * sum(t[0].islower() for t in texts) / len(texts)),
            "no_end_punct": round(100 * sum(t[-1].isalnum() for t in texts) / len(texts)),
            "questions": round(100 * sum("?" in t for t in texts) / len(texts)),
            "exclaims": round(100 * sum("!" in t for t in texts) / len(texts)),
            "skips_apostrophes": no_apos > with_apos and no_apos >= 2,
            "swears": round(100 * sum(any(w in t.lower().split() for w in SWEARS) for t in texts) / len(texts)),
            "slang": [t for _, t in sorted(slang, reverse=True)[:8]],
        }

    def guide(self, examples: int = 6) -> str:
        s = self.style()
        if not s:
            return ""
        lines = [f"- Usually short: about {s['median_words']} words per message"]
        if s["lowercase_start"] >= 60:
            lines.append(f"- Mostly lowercase ({s['lowercase_start']}% of messages start lowercase)")
        if s["no_end_punct"] >= 50:
            lines.append(f"- Usually no period at the end ({s['no_end_punct']}% of messages)")
        if s["skips_apostrophes"]:
            lines.append("- Skips apostrophes (dont, im, cant)")
        if s["slang"]:
            lines.append("- Often says: " + ", ".join(s["slang"]))
        if s["swears"] >= 5:
            lines.append(f"- Swears casually when hyped or frustrated (~{s['swears']}% of messages)")
        if s["exclaims"] < 10:
            lines.append("- Rarely uses exclamation marks")
        recent = self.samples()[-examples:]
        lines.append("Examples of how they actually talk:")
        lines += [f'> "{x["text"]}"' for x in recent]
        return f"Learned from {s['count']} of their own messages.\n" + "\n".join(lines)

    def _write(self, samples: list[dict]) -> None:
        body = ("Real examples of how you talk, copied from your chats, so every AI can sound like you. "
                "Thread keeps the most recent " + str(MAX_SAMPLES) + ". Delete any line you don't want used.\n\n"
                + "\n".join(f'- "{s["text"]}" · {s["date"]}' for s in samples))
        self.brain.write(TITLE, body, folder=FOLDER, tags=["fingerprint", "voice"])
