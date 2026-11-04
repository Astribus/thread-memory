"""Parse and write me.md, the profile note sent to the AI at the start of each session.

Layout:

    # Who I am
    - Name: Sam
    - School: Carnegie Mellon
    # What I'm working toward
    - Build Thread
    # How I like answers
    - Length: Short and direct
    # My rules for my AI
    - Save anything important I tell you to Thread.
    # Anything else
    Free text...

Unknown sections are preserved under "Anything else".
"""

import re

from thread import timezones
from thread.brain import PROFILE_NAME, Brain

ABOUT = [   # (key, me.md label, form placeholder)
    ("name", "Name", "What should your AI call you?"),
    ("pronouns", "Pronouns", "e.g. he/him, she/her, they/them"),
    ("school", "School", "e.g. Carnegie Mellon University"),
    ("year", "Year", "e.g. First-year"),
    ("major", "Major", "e.g. Data Science + AI"),
    ("classes", "Classes this semester", "e.g. 21-127, 21-241, 36-200"),
    ("location", "Where I live", "e.g. Pittsburgh"),
    # IANA zone name; used for every displayed deadline and event time.
    ("timezone", "Time zone", "Choose your time zone"),
    ("work", "Job or projects", "e.g. building a startup called Thread"),
]
STYLE = [   # (key, label, choices)
    ("length", "Length", ["Short and direct", "Balanced", "Detailed"]),
    ("tone", "Tone", ["Casual", "Friendly", "Professional"]),
    ("level", "Explanations", ["Simple, like I'm new to it", "Normal", "Technical"]),
    ("feedback", "Feedback", ["Blunt, no sugar-coating", "Honest but kind", "Encouraging"]),
]
SECTIONS = {
    "about": "Who I am",
    "goals": "What I'm working toward",
    "style": "How I like answers",
    "rules": "My rules for my AI",
    "extra": "Anything else",
}
ALIASES = {"school / year": "school",   # labels from older me.md layouts
           "where i live / time zone": "location"}
# Soft length guides (the UI warns, never truncates). The profile is sent with every request.
LIMITS = {"line": 500, "items": 30, "extra": 4000}


def _one_line(text: str) -> str:
    return " ".join(str(text or "").split())


def _split(text: str) -> tuple[str, list[tuple[str, list[str]]]]:
    """Frontmatter, then (heading, lines) for each '# heading' section."""
    front = ""
    m = re.match(r"^---\n.*?\n---\n", text, re.DOTALL)
    if m:
        front, text = m.group(0), text[m.end():]
    sections, current = [], None
    for line in text.splitlines():
        if line.startswith("# "):
            current = (line[2:].strip(), [])
            sections.append(current)
        elif current:
            current[1].append(line)
        elif line.strip():
            sections.append(current := ("", [line]))
    return front, sections


def read_profile(brain: Brain) -> dict:
    _, sections = _split(brain.profile())
    about = {key: "" for key, _, _ in ABOUT}
    style = {key: "" for key, _, _ in STYLE}
    goals, rules, extra = [], [], []
    by_label = {label.lower(): key for key, label, _ in ABOUT}
    style_labels = {label.lower(): key for key, label, _ in STYLE}
    for heading, lines in sections:
        items = [re.sub(r"^\s*[-*]\s+", "", l).strip() for l in lines if l.strip()]
        h = heading.lower()
        if h == SECTIONS["about"].lower():
            for item in items:
                label, _, value = item.partition(":")
                key = by_label.get(label.strip().lower()) or ALIASES.get(label.strip().lower())
                if key:
                    about[key] = about[key] or value.strip()
                elif value.strip() or not item.endswith(":"):   # drop old empty "Label:" lines
                    extra.append(item)
        elif h == SECTIONS["style"].lower():
            for item in items:
                label, _, value = item.partition(":")
                key = style_labels.get(label.strip().lower())
                if key:
                    style[key] = value.strip()
                else:
                    extra.append(item)
        elif h == SECTIONS["goals"].lower():
            goals += items
        elif h == SECTIONS["rules"].lower():
            rules += items
        elif h == SECTIONS["extra"].lower():
            extra += [l.rstrip() for l in lines if l.strip()]
        else:   # unknown section: keep it under "Anything else"
            extra += ([f"{heading}:"] if heading else []) + [l.rstrip() for l in lines if l.strip()]
    return {"about": about, "goals": goals, "style": style, "rules": rules, "extra": "\n".join(extra).strip()}


def write_profile(brain: Brain, data: dict) -> dict:
    """Save the form back into me.md, keeping its frontmatter (date, tags, privacy)."""
    front, _ = _split(brain.profile())
    about = dict(data.get("about") or {})
    # The zone is also set elsewhere (sign-up, the switch prompt); keep it if the form omits it.
    if "timezone" not in about:
        about["timezone"] = read_profile(brain)["about"]["timezone"]
    if _one_line(about.get("timezone")):
        zone = timezones.canonical(_one_line(about["timezone"]))
        if not zone:
            raise ValueError("That time zone isn't one Thread knows. Pick one from the list.")
        about["timezone"] = zone
    style = data.get("style") or {}
    lines = [f"# {SECTIONS['about']}"]
    lines += [f"- {label}: {_one_line(about.get(key))}".rstrip() for key, label, _ in ABOUT]
    goals = [_one_line(g) for g in data.get("goals") or [] if _one_line(g)]
    lines += ["", f"# {SECTIONS['goals']}"] + [f"- {g}" for g in goals]
    choices = {key: options for key, _, options in STYLE}
    lines += ["", f"# {SECTIONS['style']}"]
    for key, label, _ in STYLE:
        value = _one_line(style.get(key))
        if value and value in choices[key]:
            lines.append(f"- {label}: {value}")
    rules = [_one_line(r) for r in data.get("rules") or [] if _one_line(r)]
    lines += ["", f"# {SECTIONS['rules']}"] + [f"- {r}" for r in rules]
    extra = str(data.get("extra") or "").strip()
    # Lines starting with "# " would start a new section.
    extra = "\n".join(("\\" + l if l.startswith("# ") else l) for l in extra.splitlines())
    if extra:
        lines += ["", f"# {SECTIONS['extra']}", extra]
    (brain.root / PROFILE_NAME).write_text((front or "---\ntags: [profile]\n---\n") + "\n" + "\n".join(lines) + "\n")
    return read_profile(brain)


# Profile pre-fills for new accounts; all editable.
SCHOOLS_BY_DOMAIN = {"andrew.cmu.edu": "Carnegie Mellon University", "cmu.edu": "Carnegie Mellon University"}


def school_from_email(email: str) -> str:
    """'sam@andrew.cmu.edu' -> 'Carnegie Mellon University'; '' when the domain isn't a known school."""
    domain = email.rsplit("@", 1)[-1].strip().lower()
    return SCHOOLS_BY_DOMAIN.get(domain, "")


def guess_name(email: str) -> str:
    """First-name guess from the email local part ('riley.park@...' -> 'Riley'); '' if unreliable."""
    local = email.split("@", 1)[0].strip()
    first = re.split(r"[._+\-]", local, maxsplit=1)[0]
    if not first.isalpha() or len(first) < 3 or len(first) > 20 or first == local and len(local) > 12:
        return ""
    return first.capitalize()


def fill_if_empty(brain: Brain, **about) -> dict:
    """Set profile lines that are still blank; never overwrite what the person (or their AI) wrote."""
    profile = read_profile(brain)
    changed = False
    for key, value in about.items():
        if value and not profile["about"].get(key):
            profile["about"][key] = value
            changed = True
    return write_profile(brain, profile) if changed else profile


# Starter templates: one major plus any number of uses, merged into the form.
# Style values must be exact STYLE choices (enforced by a test).
MAJOR_TEMPLATES = [
    {"key": "cs", "label": "CS / Engineering",
     "goals": ["Really understand the hardest topics in my CS classes", "Build projects I can put on my resume"],
     "style": {"length": "Balanced", "tone": "Casual", "level": "Technical", "feedback": "Honest but kind"},
     "rules": ["When I'm stuck on code, give me a hint before the full answer.",
               "Show me a small example before the general rule."]},
    {"key": "science", "label": "Pre-med / Science",
     "goals": ["Keep my grades strong for med or grad school", "Understand the concepts, not just memorize them"],
     "style": {"length": "Balanced", "tone": "Friendly", "level": "Normal", "feedback": "Honest but kind"},
     "rules": ["Explain the why behind a process or formula, step by step."]},
    {"key": "business", "label": "Business",
     "goals": ["Land an internship", "Get better at presenting and writing clearly"],
     "style": {"length": "Short and direct", "tone": "Professional", "level": "Normal",
               "feedback": "Blunt, no sugar-coating"},
     "rules": ["Lead with the bottom line, then the details."]},
    {"key": "humanities", "label": "Humanities / Writing",
     "goals": ["Write essays with clearer, stronger arguments", "Read more critically"],
     "style": {"length": "Detailed", "tone": "Friendly", "level": "Normal", "feedback": "Honest but kind"},
     "rules": ["Keep my own voice when you help me edit."]},
    {"key": "exploring", "label": "Still exploring",
     "goals": ["Figure out what I want to major in"],
     "style": {"length": "Balanced", "tone": "Friendly", "level": "Simple, like I'm new to it",
               "feedback": "Encouraging"},
     "rules": ["Explain things simply, like I'm new to the topic."]},
]
USE_TEMPLATES = [
    {"key": "homework", "label": "Homework help", "goals": [],
     "rules": ["Help me understand the problem instead of just giving me the final answer."]},
    {"key": "study", "label": "Studying for exams", "goals": ["Do well on my exams"],
     "rules": ["When I'm studying, quiz me one question at a time and wait for my answer."]},
    {"key": "writing", "label": "Writing", "goals": [],
     "rules": ["When I share my writing, point out the 2 or 3 biggest problems first.",
               "Never rewrite my essays for me. Give feedback and let me revise."]},
    {"key": "organize", "label": "Staying organized", "goals": [],
     "rules": ["Whenever I mention homework or an exam, save the due date.",
               "Ask me before adding anything to my calendar."]},
]


def form_schema() -> dict:
    return {
        "about": [{"key": k, "label": l, "placeholder": p, **({"kind": "timezone"} if k == "timezone" else {})}
                  for k, l, p in ABOUT],
        "timezones": timezones.choices(),
        "style": [{"key": k, "label": l, "choices": c} for k, l, c in STYLE],
        "limits": LIMITS,
        "templates": {"majors": MAJOR_TEMPLATES, "uses": USE_TEMPLATES},
    }
