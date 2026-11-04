"""Generate a realistic synthetic Thread brain for scale tests. Never touches ~/thread-brain.
Usage: python gen.py OUT_DIR N_NOTES"""
import json, random, sys, time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from thread.brain import Brain, slugify  # noqa: E402
from thread.voice import Voice  # noqa: E402
from thread.chats import ChatBackup  # noqa: E402

R = random.Random(42)
WORDS = ("exam lecture proof matrix eigenvalue regression lab quiz homework draft meeting recruiter interview "
         "offer project roadmap launch bug design user feedback sleep gym run doctor idea startup paper reading "
         "notes plan deadline review friend family trip flight budget rent grocery coffee schedule reflection "
         "goal habit focus office hours study group syllabus chapter section theorem lemma dataset model "
         "training inference latency memory cache server deploy invoice client pitch investor demo").split()
AREAS = {
    "classes": ["MATH 201 Linear Algebra", "MATH 210 Proofs", "STAT 220 Data Analysis", "CS 150 Data Structures",
                "PHIL 110 Ethics", "MATH 120 Calculus II", "CS 340 Machine Learning", "CS 230 Systems", "STAT 310 Probability"],
    "work": ["Internships", "Recruiting", "Research lab", "Campus job", "Side gig"],
    "meetings": ["1:1s", "Standups", "Coffee chats", "Advisor", "Interviews"],
    "people": ["Friends", "Family", "Mentors", "Professors", "Coworkers"],
    "projects": ["Robotics club", "Game jam", "Data viz", "Hackathon", "Website"],
    "health": ["Sleep", "Gym", "Food", "Doctor"],
    "ideas": ["Startups", "Research", "Books", "Random"],
    "personal": ["Travel", "Money", "Home"],
}
FIRST = "Sam Alex Jordan Taylor Morgan Casey Riley Jamie Avery Quinn Drew Parker Rowan Emerson Skyler".split()
LAST = "Chen Patel Kim Garcia Nguyen Smith Lee Brown Martinez Davis Lopez Wilson Anderson Thomas".split()

def sentence(k=None):
    k = k or R.randint(8, 22)
    return " ".join(R.choice(WORDS) for _ in range(k)).capitalize() + "."

def title_for(area, sub, i):
    if area == "meetings":
        d = date(2026, 1, 1) + timedelta(days=R.randint(0, 300))
        return f"{R.choice(['Sync', 'Chat', 'Call', 'Review', 'Meeting'])} with {R.choice(FIRST)} · {d:%b %-d}"
    if area == "people":
        return f"{R.choice(FIRST)} {R.choice(LAST)} {i}"
    if area == "classes":
        return f"{sub.split()[0]} {R.choice(['Lecture', 'HW', 'Quiz', 'Exam', 'Notes', 'Recitation'])} {i}"
    return f"{sub}: {' '.join(R.choice(WORDS) for _ in range(R.randint(2, 4)))} {i}"

def main(out: Path, n: int):
    t0 = time.time()
    brain = Brain(out)
    me = (out / "me.md")
    me.write_text("---\nprivacy: light\ndate: 2025-09-01\ntags: [profile]\n---\n\n# Who I am\n- Name: Test User\n- School: Carnegie Mellon University\n"
                  "- Major: Data Science\n\n# My rules for my AI\n- Save anything important.\n")
    notes = []            # (rel_without_md, title)
    # create hubs through Brain so the structure matches production
    per_area = n // len(AREAS)
    for area, subs in AREAS.items():
        for s in subs:
            brain.write(f"{s} overview", sentence(30), folder=f"{area}/{s}")
    hubs = [p.relative_to(out).with_suffix("").as_posix() for p in out.rglob("*.md")]
    hub_titles = {h: brain.title(Path(h + ".md")) for h in hubs}
    # preferential attachment: hubs plus a long tail
    weights, pool = [], []
    count = len(list(out.rglob("*.md")))
    i = 0
    while count < n:
        area = R.choice(list(AREAS)); sub = R.choice(AREAS[area]); i += 1
        title = title_for(area, sub, i)
        folder = f"{area}/{sub}"
        k = 0 if R.random() < 0.06 else min(int(R.paretovariate(1.6)), 25)   # ~6% orphans
        targets = []
        for _ in range(k):
            if pool and R.random() < 0.85:
                targets.append(R.choices(pool, weights=weights)[0])
            elif hubs:
                targets.append(R.choice(hubs))
        links = []
        for t in set(targets):
            style = R.random()
            if style < 0.70:
                links.append(f"[[{t}|{dict(notes).get(t) or hub_titles.get(t, t)}]]")
            elif style < 0.95:
                links.append(f"[[{dict(notes).get(t) or hub_titles.get(t, t)}]]")   # title-only link
            else:
                links.append(f"[[Some missing note {R.randint(1, 500)}]]")               # unresolved
        body = "\n\n".join(sentence() for _ in range(R.randint(1, 6)))
        if links:
            body += "\n\nRelated: " + ", ".join(links)
        d = date(2025, 9, 1) + timedelta(days=R.randint(0, 410))
        folder_path = out / area / slugify(sub)
        folder_path.mkdir(parents=True, exist_ok=True)
        slug = slugify(title)
        path = folder_path / f"{slug}.md"
        if path.exists():
            continue
        hub_rel = f"{area}/{slugify(sub)}/{slugify(sub)}"
        path.write_text(f"---\ndate: {d.isoformat()}\ntags: [{area}]\n---\n\n# {title}\n\n{body}\n\nPart of [[{hub_rel}|{sub}]]\n")
        rel = path.relative_to(out).with_suffix("").as_posix()
        notes.append((rel, title)); pool.append(rel); weights.append(1 + len(links))
        for t in targets:
            if t in pool:
                weights[pool.index(t)] += 1
        count += 1
    # fingerprint and voice
    from thread.fingerprint import Fingerprint
    fp = Fingerprint(brain)
    cats = ["thinking", "voice", "learning", "working", "taste", "patterns", "now"]
    kinds = ["prefers", "dislikes", "pattern", "struggles", "works", "style", "goal", "fact"]
    for j in range(min(200, n // 10)):
        try:
            fp.learn(R.choice(cats), sentence(10) + f" #{j}", R.choice(kinds), "chat")
        except Exception:
            pass
    v = Voice(brain)
    for j in range(80):
        v.add(" ".join(R.choice(WORDS) for _ in range(R.randint(4, 20))) + f" lol {j}")
    # 10 days of chats: ~15/day, 10–60 messages each
    chats = ChatBackup(out.parent / (out.name + "-chats"))
    now = time.time()
    for c in range(150):
        msgs = [{"id": f"m{m}", "role": "user" if m % 2 == 0 else "assistant",
                 "text": " ".join(sentence() for _ in range(R.randint(1, 8)))} for m in range(R.randint(10, 60))]
        chats.save({"source": R.choice(["claude", "chatgpt"]), "chat_id": f"chat-{c:04d}", "title": sentence(5), "messages": msgs},
                   now=now - R.randint(0, 9) * 86400)
    print(f"{n} notes target -> {len(list(out.rglob('*.md')))} md files in {time.time() - t0:.1f}s; chats at {chats.folder}")

if __name__ == "__main__":
    main(Path(sys.argv[1]), int(sys.argv[2]))
