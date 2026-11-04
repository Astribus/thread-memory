"""The gray box: a folder of Markdown notes that Thread reads and writes.

Kept separate from the MCP server so the storage logic can be tested on its own.
"""

import os
import re
import shutil
from datetime import date, datetime
from pathlib import Path

PROFILE_NAME = "me.md"
# Above this many notes, list_memory returns a per-folder summary.
TREE_FULL_LIMIT = 300

# Stop words ignored when ranking search results.
STOPWORDS = {"the", "is", "my", "me", "and", "or", "of", "to", "in", "on", "at", "for",
             "what", "when", "where", "who", "how", "do", "did", "does", "an", "it", "i"}

PRIVACY_LEVELS = ("strict", "light")
# Saving mode. A missing `saving:` line means the user hasn't chosen: save everything and ask once.
SAVING_MODES = ("everything", "important", "ask")

PROFILE_TEMPLATE = """---
date: {today}
tags: [profile]
privacy: strict
---

# Who I am
- Name:
"""


def loose(text: str) -> str:
    """Compare titles ignoring case and punctuation: 'Exam 1: Weak spots' == 'exam-1-weak-spots'."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def default_root() -> Path:
    return Path(os.environ.get("THREAD_BRAIN_DIR", Path.home() / "thread-brain"))


# [[path]], [[path|label]], [[path#heading]] (Obsidian syntax).
LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
LINK_PARTS_RE = re.compile(r"\[\[([^\]|#]+)(#[^\]|]*)?(?:\|([^\]]*))?\]\]")
TRASH = ".trash"   # deleted notes, kept for undo
# Display names for Thread's own folders.
FRIENDLY_FOLDERS = {"fingerprint": "What my AI learned"}


def slugify(text: str) -> str:
    """Turn a title like 'CS 15-122: Week 3!' into a safe name like 'cs-15-122-week-3'."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if not slug:
        raise ValueError(f"Can't make a file name out of {text!r}")
    return slug


class Brain:
    def __init__(self, root: Path):
        self._meta: dict[str, tuple] = {}   # rel path -> (mtime_ns, size, title, date, links); see _scan
        self._graph_cache: tuple | None = None   # (signature, graph)
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        profile = self.root / PROFILE_NAME
        if not profile.exists():
            profile.write_text(PROFILE_TEMPLATE.format(today=date.today().isoformat()))

    def _notes(self) -> list[Path]:
        """Every note in the brain, skipping hidden folders (like the trash)."""
        return [self.root / rel for rel in self._note_rels()]

    def _note_rels(self) -> list[str]:
        """Sorted relative posix paths of every note, pruning hidden folders."""
        out, root = [], str(self.root)
        for folder, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            base = os.path.relpath(folder, root)
            prefix = "" if base == "." else base.replace(os.sep, "/") + "/"
            out.extend(prefix + f for f in files if f.endswith(".md") and not f.startswith("."))
        return sorted(out, key=lambda r: Path(r).parts)

    def _parts(self, folder: str) -> list[tuple[str, str]]:
        """'classes/36200 Reasoning with Data' -> [('classes', 'classes'), ('36200 Reasoning with Data', '36200-...')].
        Each part is slugified, so '..' or '/etc' can't escape the brain."""
        parts = [(p.strip(), slugify(p)) for p in folder.split("/") if re.search(r"[A-Za-z0-9]", p)]
        return self._reuse_existing(parts) or [("inbox", "inbox")]

    def _reuse_existing(self, parts: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """If 'classes/21-241' doesn't exist but 'classes/21241-matrices-...' does, use that one,
        so the same class never ends up split across two folders."""
        def compact(text: str) -> str:
            return re.sub(r"[^a-z0-9]", "", text)

        current, result = self.root, []
        for label, slug in parts:
            if not (current / slug).is_dir() and current.is_dir():
                # Folders match on course number, whichever name is longer ("36-200" vs "36-200 Reasoning with Data").
                want = compact(slug)
                matches = [d.name for d in current.iterdir() if d.is_dir() and (
                    compact(d.name).startswith(want)
                    or (want.startswith(compact(d.name)) and re.match(r"\d{5}", compact(d.name))))]
                if len(matches) == 1:
                    slug = matches[0]
            result.append((label, slug))
            current = current / slug
        return result

    def class_slug(self, course: str) -> str:
        """Existing classes/ slug for this course (matched by course number), else its own slug."""
        parts = self._parts(f"classes/{course.replace('/', ' ')}")
        return parts[1][1] if len(parts) > 1 else slugify(course)

    def _folder(self, folder: str) -> Path:
        return self.root.joinpath(*(slug for _, slug in self._parts(folder)))

    def _inside(self, path: Path) -> Path:
        path = (self.root / path).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("That path is outside the brain")
        return path

    def profile(self) -> str:
        return (self.root / PROFILE_NAME).read_text()

    def privacy(self) -> str:
        """'strict' (default) or 'light', from the `privacy:` line in me.md."""
        m = re.search(r"^privacy:\s*(\w+)", self.profile(), re.MULTILINE)
        level = m.group(1).lower() if m else "strict"
        return level if level in PRIVACY_LEVELS else "strict"

    def set_privacy(self, level: str) -> str:
        level = level.strip().lower()
        if level not in PRIVACY_LEVELS:
            raise ValueError(f"privacy must be one of: {', '.join(PRIVACY_LEVELS)}")
        self._set_front("privacy", level)
        return level

    def saving(self) -> str | None:
        """'everything', 'important' or 'ask', from the `saving:` line in me.md; None if never chosen."""
        m = re.search(r"^saving:\s*(\w+)", self.profile(), re.MULTILINE)
        mode = m.group(1).lower() if m else None
        return mode if mode in SAVING_MODES else None

    def set_saving(self, mode: str) -> str:
        mode = mode.strip().lower()
        if mode not in SAVING_MODES:
            raise ValueError(f"saving must be one of: {', '.join(SAVING_MODES)}")
        self._set_front("saving", mode)
        return mode

    def _set_front(self, key: str, value: str) -> None:
        """Set one `key: value` line in me.md's front matter, adding the front matter if it's missing."""
        path = self.root / PROFILE_NAME
        text = path.read_text()
        if re.search(rf"^{key}:", text, re.MULTILINE):
            text = re.sub(rf"^{key}:.*$", f"{key}: {value}", text, count=1, flags=re.MULTILINE)
        elif text.startswith("---\n"):
            text = "---\n" + f"{key}: {value}\n" + text[4:]
        else:
            text = f"---\n{key}: {value}\n---\n\n" + text
        path.write_text(text)

    def title(self, rel: Path) -> str:
        if rel == Path(PROFILE_NAME):
            return "Me"
        for line in (self.root / rel).read_text().splitlines():
            if line.startswith("# "):
                return line[2:].strip()
        return rel.stem

    def display_title(self, rel: Path) -> str:
        """Title shown in the UI. Built-in folders get display names; paths on disk are unchanged."""
        title = self.title(rel)
        parts = rel.with_suffix("").parts
        if len(parts) == 2 and parts[0] == parts[1] and title == parts[0]:   # folder hub with its default name
            return FRIENDLY_FOLDERS.get(title, title)
        return title

    def link(self, rel: Path) -> str:
        return f"[[{rel.with_suffix('').as_posix()}|{self.title(rel)}]]"

    def _hub(self, parts: list[tuple[str, str]]) -> Path:
        """Every folder has a hub note named after it that links up to its parent folder's hub,
        and the top-level hubs link to me.md, so every note connects back to the user."""
        if not parts:
            return Path(PROFILE_NAME)
        parent = self._hub(parts[:-1])
        label, slug = parts[-1]
        folder = self.root.joinpath(*(s for _, s in parts))
        hub = folder / f"{slug}.md"
        if not hub.exists():
            folder.mkdir(parents=True, exist_ok=True)
            hub.write_text(f"---\ndate: {date.today().isoformat()}\ntags: [folder]\n---\n\n"
                           f"# {label}\n\nPart of {self.link(parent)}\n")
        return hub.relative_to(self.root)

    def resolve(self, ref: str) -> Path | None:
        """Find a note from a path ('classes/x/note' or '.md') or a title ('Exam 1 weak spots')."""
        ref = ref.strip().removesuffix(".md")
        try:
            path = self._inside(Path(ref + ".md"))
            if path.exists():
                return path.relative_to(self.root)
        except ValueError:
            return None
        # Title or filename lookup via the cached index.
        found = self._resolver(self._scan())(ref)
        return Path(found + ".md") if found else None

    def _related_lines(self, related: list[str] | None) -> str:
        links = []
        for ref in related or []:
            rel = self.resolve(ref)
            links.append(self.link(rel) if rel else f"[[{ref.strip()}]]")   # unresolved: keep as a plain link
        return f"\nRelated: {', '.join(links)}" if links else ""

    def remember(self, title: str, content: str, folder: str = "inbox",
                 tags: list[str] | None = None, related: list[str] | None = None) -> Path:
        """Save a note. If a note with this title already exists, add to it instead of overwriting."""
        path = self._folder(folder) / f"{slugify(title)}.md"
        if not path.exists():
            return self.write(title, content, folder, tags, related)
        with path.open("a") as f:
            f.write(f"\n\n## Update {date.today().isoformat()}\n{content.strip()}\n{self._related_lines(related)}")
        return path.relative_to(self.root)

    def exists(self, folder: str, title: str) -> bool:
        return (self._folder(folder) / f"{slugify(title)}.md").exists()

    def write(self, title: str, content: str, folder: str = "inbox",
              tags: list[str] | None = None, related: list[str] | None = None) -> Path:
        """Create or fully replace a note. Used for generated notes like synced deadlines."""
        hub = self._hub(self._parts(folder))
        path = self._folder(folder) / f"{slugify(title)}.md"
        tag_list = ", ".join(tags or [])
        footer = f"\n\nPart of {self.link(hub)}" if path.relative_to(self.root) != hub else ""
        path.write_text(
            f"---\ndate: {date.today().isoformat()}\ntags: [{tag_list}]\n---\n\n# {title}\n\n"
            f"{content.strip()}{footer}{self._related_lines(related)}\n"
        )
        return path.relative_to(self.root)

    def add_link(self, from_ref: str, to_ref: str) -> str:
        source = self.resolve(from_ref)
        if source is None:
            raise ValueError(f"No note called {from_ref!r}")
        with (self.root / source).open("a") as f:
            f.write(self._related_lines([to_ref]) + "\n")
        return str(source)

    def link_all(self) -> int:
        """Give any note that isn't linked to its folder's hub a 'Part of' link. Safe to run repeatedly."""
        fixed = 0
        for path in self._notes():
            rel = path.relative_to(self.root)
            if rel == Path(PROFILE_NAME):
                continue
            parts = [(s.replace("-", " ").title(), s) for s in rel.parent.parts]
            hub = self._hub(parts) if parts else Path(PROFILE_NAME)
            if rel == hub:
                continue
            target = hub.with_suffix("").as_posix()
            if any(m.group(1).strip() == target for m in LINK_RE.finditer(path.read_text())):
                continue
            with path.open("a") as f:
                f.write(f"\nPart of {self.link(hub)}\n")
            fixed += 1
        return fixed

    def is_hub(self, rel: Path) -> bool:
        return rel.parent != Path(".") and rel.stem == rel.parent.name

    def tree_data(self) -> dict:
        """Folders and notes for the sidebar. A folder's hub note is the folder itself, so it isn't listed twice."""
        meta = self._scan()   # titles from the index
        def title_of(rel: Path) -> str:
            m = meta.get(rel.as_posix())
            return m[2] if m else self.title(rel)
        def shown_of(rel: Path) -> str:
            title, parts = title_of(rel), rel.with_suffix("").parts
            return FRIENDLY_FOLDERS.get(title, title) if len(parts) == 2 and parts[0] == parts[1] and title == parts[0] else title
        def walk(folder: Path) -> list[dict]:
            folders, notes = [], []
            for path in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
                if path.name.startswith("."):
                    continue
                rel = path.relative_to(self.root)
                if path.is_dir():
                    hub = rel / f"{path.name}.md"
                    title = shown_of(hub) if (self.root / hub).exists() else FRIENDLY_FOLDERS.get(path.name, path.name)
                    folders.append({"type": "folder", "path": rel.as_posix(), "name": title,
                                    "note": hub.with_suffix("").as_posix(), "children": walk(path)})
                elif path.suffix == ".md" and not self.is_hub(rel) and rel != Path(PROFILE_NAME):
                    notes.append({"type": "note", "id": rel.with_suffix("").as_posix(), "name": title_of(rel)})
            return folders + notes
        return {"type": "folder", "path": "", "name": "Thread", "children":
                [{"type": "note", "id": "me", "name": "Me"}] + walk(self.root)}

    def create_folder(self, parent: str, name: str) -> str:
        """Make a folder (with its hub note) inside `parent` ('' = top level). Returns the folder path."""
        label = name.strip()
        base = self._inside(Path(parent)) if parent else self.root
        if not base.is_dir():
            raise FileNotFoundError(f"No folder {parent!r}")
        if (base / slugify(label)).exists():
            raise FileExistsError(f"A folder called {label!r} already exists there")
        parents = [(part, part) for part in base.relative_to(self.root).parts]
        return self._hub(parents + [(label, slugify(label))]).parent.as_posix()

    def create_note(self, folder: str, title: str, content: str = "") -> str:
        """Make a new note inside an existing folder path ('' = inbox). Refuses to overwrite."""
        base = self._inside(Path(folder)) if folder else self.root / "inbox"
        path = base / f"{slugify(title)}.md"
        if path.exists():
            raise FileExistsError(f"A note called {title!r} already exists there")
        if not folder:
            return self.write(title, content, "inbox").with_suffix("").as_posix()
        hub = base / f"{base.name}.md"
        if not hub.exists():
            self.link_all()   # folder created outside Thread: add its hub first
        hub_rel = hub.relative_to(self.root)
        body = f"{content.strip()}\n\n" if content.strip() else ""
        path.write_text(f"---\ndate: {date.today().isoformat()}\ntags: []\n---\n\n# {title.strip()}\n\n"
                        f"{body}Part of {self.link(hub_rel)}\n")
        return path.relative_to(self.root).with_suffix("").as_posix()

    def save(self, note_id: str, text: str) -> None:
        """Replace a note's full text (from the editor). Keeps it connected if the 'Part of' link was removed."""
        path = self._inside(Path(note_id + ".md"))
        if not path.exists():
            raise FileNotFoundError(note_id)
        path.write_text(text if text.endswith("\n") else text + "\n")
        self.link_all()

    # ---------- rename ----------
    def _rewrite_links(self, moves: dict[str, str], titles: dict[str, tuple[str, str]]) -> None:
        """After notes move, point every [[link]] at the new place. `moves` maps old id -> new id;
        `titles` maps new id -> (old title, new title) so display text that showed the old title updates too."""
        old_titles = {loose(t[0]): new for new, t in titles.items()}

        def fix(match: re.Match) -> str:
            target, heading, shown = match.group(1).strip(), match.group(2) or "", match.group(3)
            new = moves.get(target.removesuffix(".md"))
            if new is None and loose(target) in old_titles:   # plain [[Old Title]] link
                new = old_titles[loose(target)]
            if new is None:
                return match.group(0)
            if new in titles and (shown is None or shown == titles[new][0]):
                shown = titles[new][1]
            return f"[[{new}{heading}|{shown}]]" if shown else f"[[{new}{heading}]]"

        for path in self._notes():
            text = path.read_text()
            new_text = LINK_PARTS_RE.sub(fix, text)
            if new_text != text:
                path.write_text(new_text)

    def _set_heading(self, path: Path, title: str) -> None:
        text = path.read_text()
        new_text, n = re.subn(r"^# .*$", f"# {title}", text, count=1, flags=re.MULTILINE)
        path.write_text(new_text if n else text)

    def rename_note(self, note_id: str, new_title: str) -> str:
        """Rename a note (title and file) and update every link to it. Returns the new id."""
        old = self._inside(Path(note_id + ".md"))
        rel = old.relative_to(self.root)
        if not old.exists() or rel == Path(PROFILE_NAME) or self.is_hub(rel):
            raise ValueError("That note can't be renamed here")
        new = old.with_name(f"{slugify(new_title)}.md")
        if new != old and new.exists():
            raise FileExistsError(f"A note called {new_title.strip()!r} already exists there")
        old_title = self.title(rel)
        self._set_heading(old, new_title.strip())
        old.rename(new)
        new_id = new.relative_to(self.root).with_suffix("").as_posix()
        self._rewrite_links({note_id: new_id}, {new_id: (old_title, new_title.strip())})
        return new_id

    def rename_folder(self, folder: str, new_name: str) -> str:
        """Rename a folder (its name and its folder note) and update every link into it. Returns the new path."""
        old_dir = self._inside(Path(folder))
        if not old_dir.is_dir() or old_dir == self.root:
            raise ValueError("That folder can't be renamed")
        new_dir = old_dir.with_name(slugify(new_name))
        if new_dir != old_dir and new_dir.exists():
            raise FileExistsError(f"A folder called {new_name.strip()!r} already exists there")
        old_hub_rel = old_dir.relative_to(self.root) / f"{old_dir.name}.md"
        old_title = self.title(old_hub_rel) if (self.root / old_hub_rel).exists() else old_dir.name
        old_ids = [p.relative_to(self.root).with_suffix("").as_posix() for p in self._notes() if p.is_relative_to(old_dir)]
        old_dir.rename(new_dir)
        hub = new_dir / f"{old_dir.name}.md"
        if hub.exists():
            hub = hub.rename(new_dir / f"{new_dir.name}.md")
            self._set_heading(hub, new_name.strip())
        old_prefix, new_prefix = folder.strip("/"), new_dir.relative_to(self.root).as_posix()
        moves = {}
        for old_id in old_ids:
            new_id = new_prefix + old_id[len(old_prefix):]
            if old_id == f"{old_prefix}/{old_dir.name}":   # the folder's hub note
                new_id = f"{new_prefix}/{new_dir.name}"
            moves[old_id] = new_id
        hub_id = f"{new_prefix}/{new_dir.name}"
        self._rewrite_links(moves, {hub_id: (old_title, new_name.strip())})
        return new_prefix

    # ---------- delete / undo ----------
    def delete(self, target: str) -> dict:
        """Move a note or a whole folder into the trash. Returns what Undo needs."""
        path = self._inside(Path(target + ".md")) if not target.endswith("/") and (self.root / (target + ".md")).exists() \
            else self._inside(Path(target.strip("/")))
        rel = path.relative_to(self.root)
        if not path.exists() or path == self.root or rel == Path(PROFILE_NAME) or rel.parts[0] == TRASH:
            raise ValueError("That can't be deleted")
        if path.is_file() and self.is_hub(rel):
            raise ValueError("Delete the folder instead of its folder note")
        count = sum(1 for _ in path.rglob("*.md")) if path.is_dir() else 1
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        dest = self.root / TRASH / stamp / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest))
        return {"trash": stamp, "notes": count}

    def restore(self, stamp: str) -> list[str]:
        """Undo a delete: put everything from that trash entry back where it was."""
        if not re.fullmatch(r"[\d-]+", stamp):
            raise ValueError("Unknown trash entry")
        base = self.root / TRASH / stamp
        if not base.is_dir():
            raise FileNotFoundError("Nothing to restore")
        restored = []
        for item in sorted(base.rglob("*")):
            if item.is_file():
                original = self.root / item.relative_to(base)
                if original.exists():
                    raise FileExistsError(f"{original.relative_to(self.root)} already exists again")
        for top in base.iterdir():
            original = self.root / top.relative_to(base)
            original.parent.mkdir(parents=True, exist_ok=True)
            if original.exists() and original.is_dir():   # merge into an existing folder
                for item in top.rglob("*"):
                    dest = original / item.relative_to(top)
                    if item.is_dir():
                        dest.mkdir(parents=True, exist_ok=True)
                    else:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(item), str(dest))
            else:
                shutil.move(str(top), str(original))
            restored.append(top.relative_to(base).as_posix())
        shutil.rmtree(base)
        return restored

    # ---- per-note index (title, date, links), refreshed by mtime/size ----
    def _scan(self) -> dict[str, tuple]:
        """Every note's (title, date, link targets), reading only the files that changed since last time."""
        fresh, root = {}, str(self.root)
        for rel in self._note_rels():
            full = os.path.join(root, rel)
            try:
                st = os.stat(full)
            except FileNotFoundError:
                continue
            old = self._meta.get(rel)
            if old and old[0] == st.st_mtime_ns and old[1] == st.st_size:
                fresh[rel] = old
                continue
            with open(full, encoding="utf-8") as f:
                text = f.read()
            stem = rel.rsplit("/", 1)[-1][:-3]
            heading = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), None)
            title = "Me" if rel == PROFILE_NAME else (heading or stem)
            created = re.search(r"^date:\s*(\d{4}-\d{2}-\d{2})", text, re.MULTILINE)
            links = tuple(m.group(1) for m in LINK_RE.finditer(text))
            fresh[rel] = (st.st_mtime_ns, st.st_size, title, created.group(1) if created else None, links)
        self._meta = fresh
        return fresh

    def _signature(self) -> tuple:
        """Changes whenever any note is added, removed or edited (a stat walk; no file is read)."""
        total, newest, count, root = 0, 0, 0, str(self.root)
        for rel in self._note_rels():
            try:
                st = os.stat(os.path.join(root, rel))
            except FileNotFoundError:
                continue
            total += st.st_size; newest = max(newest, st.st_mtime_ns); count += 1
        return count, newest, total

    def _resolver(self, meta: dict[str, tuple]):
        """resolve for many links at once: same answers, one lookup each instead of a scan of every note."""
        exact = {rel[:-3]: rel[:-3] for rel in meta}   # 'classes/x/note' -> itself
        loose_index: dict[str, str] = {}
        for rel in meta:   # same order as resolve(): first match wins
            loose_index.setdefault(rel.rsplit("/", 1)[-1][:-3], rel[:-3])
            loose_index.setdefault(loose(meta[rel][2]), rel[:-3])
        def find(ref: str) -> str | None:
            ref = ref.strip().removesuffix(".md")
            if ref in exact:
                return exact[ref]
            try:
                path = self._inside(Path(ref + ".md"))
            except ValueError:
                return None
            if path.exists():   # e.g. inside a hidden folder
                return path.relative_to(self.root).with_suffix("").as_posix()
            want = loose(ref)
            return loose_index.get(want) if want else None
        return find

    def graph_version(self) -> str:
        """A short tag for the current graph (from the last graph call), for HTTP ETags."""
        sig = self._graph_cache[0] if self._graph_cache else self._signature()
        return "-".join(str(x) for x in sig)

    def graph(self) -> dict:
        """Dots and lines for the home screen. One edge per pair, direction kept, unknown links faded."""
        signature = self._signature()
        if self._graph_cache and self._graph_cache[0] == signature:
            return self._graph_cache[1]
        meta = self._scan()
        find = self._resolver(meta)
        nodes, edges = {}, set()
        # Chat logs are excluded from the graph (they'd add ~1,000 nodes a year); they're still searchable.
        meta = {rel: v for rel, v in meta.items() if not rel.startswith("chats/")}
        for rel, (_, _, title, created, _) in meta.items():
            node_id = rel[:-3]
            parts = node_id.split("/")
            shown = (FRIENDLY_FOLDERS.get(title, title)
                     if len(parts) == 2 and parts[0] == parts[1] and title == parts[0] else title)
            nodes[node_id] = {"id": node_id, "title": shown, "folder": "/".join(parts[:-1]) or ".",
                              "unresolved": False, "date": created}
        for rel, (_, _, _, _, targets) in meta.items():
            source = rel[:-3]
            for raw in targets:
                target = find(raw) or raw.strip()
                if target.startswith("chats/"):
                    continue
                if target not in nodes:
                    nodes[target] = {"id": target, "title": target, "folder": "", "unresolved": True, "date": None}
                if target != source:
                    edges.add((source, target))
        degree = {n: 0 for n in nodes}
        for a, b in edges:
            degree[a] += 1
            degree[b] += 1
        for n in nodes.values():
            n["degree"] = degree[n["id"]]
        result = {"nodes": list(nodes.values()),
                  "edges": [{"source": a, "target": b} for a, b in sorted(edges)]}
        self._graph_cache = (signature, result)
        return result

    # ---- chat logs: one note per conversation under chats/<day>/ ----
    def log_chat(self, app: str, topic: str, summary: str, small: list[str] | None = None,
                 saved: list[str] | None = None, chat: str = "", when: datetime | None = None) -> str:
        """Start (or, with `chat`, add to) the log of one conversation. Returns its path."""
        when = when or datetime.now().astimezone()
        app = (app or "AI").strip()[:30]
        topic = " ".join((topic or "Chat").split())[:80]
        small = [" ".join(str(x).split()) for x in (small or []) if str(x).strip()]
        saved = [str(x).strip() for x in (saved or []) if str(x).strip()]
        clock = when.strftime("%-I:%M %p").lower()
        def to_link(t: str) -> str:   # unresolved paths are kept as plain text
            rel = Path(t if t.endswith(".md") else t + ".md")
            try:
                return self.link(rel) if (self.root / rel).is_file() else f"[[{t.removesuffix('.md')}]]"
            except (ValueError, OSError):
                return f"[[{t.removesuffix('.md')}]]"
        links = "".join(f"\n- {to_link(t)}" for t in saved)
        if chat:
            rel = chat if chat.endswith(".md") else chat + ".md"
            path = self._inside(Path(rel))
            if not str(path.relative_to(self.root)).startswith("chats/") or not path.exists():
                raise ValueError("That isn't a chat log. Start a new one without `chat`.")
            text = f"\n\n### Later ({clock})\n{summary.strip()}"
            if small:
                text += "\n\n**Small things**\n" + "\n".join(f"- {x}" for x in small)
            if saved:
                text += "\n\n**Saved to other notes**" + links
            with path.open("a") as f:
                f.write(text)
            return str(path.relative_to(self.root))[:-3]
        title = f"{when:%H%M} {app} · {topic}"
        body = (f"**{app}** · {when.strftime('%a %b %-d')}, {clock}\n\n## What we covered\n{summary.strip()}")
        if small:
            body += "\n\n## Small things\n" + "\n".join(f"- {x}" for x in small)
        if saved:
            body += "\n\n## Saved to other notes" + links
        path = Path(self.write(title, body, f"chats/{when:%Y-%m-%d}", tags=["chat", slugify(app)]))
        rel = path.relative_to(self.root) if path.is_absolute() else path
        full = self.root / rel   # dated by the chat's day
        full.write_text(re.sub(r"^date: .*$", f"date: {when:%Y-%m-%d}", full.read_text(), count=1, flags=re.M))
        return str(rel)[:-3]

    def recent_chats(self, days: int = 3, now: datetime | None = None, limit: int = 12) -> list[dict]:
        """The latest chat logs from any AI, newest first, with the start of each summary."""
        now = now or datetime.now().astimezone()
        base = self.root / "chats"
        if not base.is_dir():
            return []
        cutoff = (now.date().toordinal() - days)
        found = []
        for day in sorted((d for d in base.iterdir() if d.is_dir()), reverse=True):
            try:
                if date.fromisoformat(day.name).toordinal() < cutoff:
                    break
            except ValueError:
                continue
            for note in sorted(day.glob("*.md"), reverse=True):
                if note.stem == day.name:
                    continue   # the day's hub note
                text = note.read_text()
                body = text.split("---", 2)[-1] if text.startswith("---") else text
                found.append({"path": str(note.relative_to(self.root))[:-3], "day": day.name,
                              "title": (re.search(r"^# (.+)$", text, re.M) or [None, note.stem])[1],
                              "summary": " ".join(body.replace("#", "").split())[:600]})
                if len(found) >= limit:
                    return found
        return found

    def search(self, query: str, limit: int = 5) -> list[dict]:
        """Rank notes by the number of query words they contain."""
        words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in STOPWORDS]
        results = []
        for path in self._notes():
            if path == self.root / PROFILE_NAME:
                continue   # already sent as server instructions
            text = path.read_text()
            lower = text.lower()
            score = sum(lower.count(w) for w in words)
            if score == 0:
                continue
            first_hit = min(lower.find(w) for w in words if w in lower)
            start = max(0, first_hit - 80)
            results.append({
                "path": str(path.relative_to(self.root)),
                "score": score,
                "snippet": text[start:start + 240].replace("\n", " ").strip(),
            })
        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:limit]

    def read(self, path: str) -> str:
        return self._inside(Path(path)).read_text()

    def tree(self, folder: str = "") -> str:
        """Indented folder/note listing for the AI.

        Above TREE_FULL_LIMIT notes, returns per-folder counts and the newest notes instead. Pass `folder`
        to list one folder in full.
        """
        base = self._inside(Path(folder)) if folder else self.root
        if folder and not base.is_dir():
            raise ValueError(f"No folder called {folder!r}")
        rels = [r for r in self._note_rels() if not folder or r.startswith(folder.strip("/") + "/")]
        if len(rels) <= TREE_FULL_LIMIT:
            lines = []
            for path in sorted(base.rglob("*")):
                if any(part.startswith(".") for part in path.relative_to(self.root).parts):
                    continue
                rel = path.relative_to(base)
                indent = "  " * (len(rel.parts) - 1)
                lines.append(f"{indent}{rel.name}{'/' if path.is_dir() else ''}")
            return "\n".join(lines) or "(empty)"
        meta = self._scan()
        by_folder: dict[str, list[str]] = {}
        depth = (folder.strip("/").count("/") + 2) if folder else 2   # summarise two levels below `folder`
        for rel in rels:
            parts = rel.split("/")
            key = "/".join(parts[:min(len(parts) - 1, depth)]) or "(top level)"
            by_folder.setdefault(key, []).append(rel)
        lines = [f"{len(rels)} notes in {len(by_folder)} folders. Too many to list one by one, so here is each folder "
                 "with its newest notes. Call list_memory with folder=\"<folder>\" to see one folder in full, or "
                 "search_memory to find something specific.", ""]
        for key in sorted(by_folder):
            notes = sorted(by_folder[key], key=lambda r: meta.get(r, (0, 0, "", ""))[3] or "", reverse=True)
            real = [r for r in notes if r in meta and not self.is_hub(Path(r))]   # skip the folder's hub
            newest = ", ".join(meta[r][2] for r in real[:3])
            count = f"{len(notes)} note{'' if len(notes) == 1 else 's'}"
            lines.append(f"{key}/  ({count})" + (f"  newest: {newest}" if newest else ""))
        return "\n".join(lines)
