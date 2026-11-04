"""Chat backup uploaded by the browser extension: one JSON file per chat, kept for 10 days."""

import json
import re
import time
from pathlib import Path

RETENTION_DAYS = 10
SOURCES = {"chatgpt": "ChatGPT", "claude": "Claude"}
ROLES = {"user", "assistant"}
CHAT_ID = re.compile(r"^[A-Za-z0-9-]{6,80}$")
MAX_MESSAGES = 2000   # per chat; guards against runaway uploads


class ChatBackup:
    def __init__(self, folder: Path):
        self.folder = Path(folder)

    def _path(self, key: str) -> Path:
        source, _, chat_id = key.partition("-")
        if source not in SOURCES or not CHAT_ID.match(chat_id):
            raise ValueError("Unknown chat")
        return self.folder / f"{source}-{chat_id}.json"

    def save(self, chat: dict, now: float | None = None) -> dict:
        """Insert or merge a chat; messages are deduplicated by id."""
        now = now or time.time()
        source, chat_id = str(chat.get("source", "")), str(chat.get("chat_id", ""))
        path = self._path(f"{source}-{chat_id}")
        self.cleanup(now)
        try:
            saved = json.loads(path.read_text())
        except FileNotFoundError:
            saved = {"source": source, "chat_id": chat_id, "first_saved": now, "messages": []}
        saved["title"] = str(chat.get("title") or saved.get("title") or "Untitled chat")[:200]
        saved["url"] = str(chat.get("url") or saved.get("url") or "")[:500]
        saved["updated"] = now
        by_id = {m["id"]: m for m in saved["messages"]}
        order = [m["id"] for m in saved["messages"]]
        for m in chat.get("messages") or []:
            mid, role, text = str(m.get("id", ""))[:100], m.get("role"), str(m.get("text", ""))
            if not mid or role not in ROLES or not text.strip():
                continue
            if mid not in by_id:
                order.append(mid)
            by_id[mid] = {"id": mid, "role": role, "text": text}
        saved["messages"] = [by_id[i] for i in order][-MAX_MESSAGES:]
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(saved))
        tmp.chmod(0o600)
        tmp.replace(path)
        return {"key": path.stem, "messages": len(saved["messages"])}

    def _each(self):
        """Yield saved chats one at a time to bound memory use."""
        for path in self.folder.glob("*.json"):
            try:
                chat = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            chat["key"] = path.stem
            yield chat

    def cleanup(self, now: float | None = None) -> int:
        """Delete every chat first saved more than 10 days ago. Returns how many went."""
        cutoff = (now or time.time()) - RETENTION_DAYS * 86400
        gone = 0
        for chat in self._each():
            if chat.get("first_saved", 0) < cutoff:
                (self.folder / f"{chat['key']}.json").unlink(missing_ok=True)
                gone += 1
        return gone

    def overview(self, now: float | None = None) -> list[dict]:
        now = now or time.time()
        self.cleanup(now)
        out = []
        for c in self._each():   # stream: one chat in memory at a time
            first_user = next((m["text"] for m in c["messages"] if m["role"] == "user"), "")
            out.append({"key": c["key"], "source": SOURCES[c["source"]], "title": c.get("title", ""),
                        "url": c.get("url", ""), "messages": len(c["messages"]), "updated": c.get("updated"),
                        "deletes_in_days": max(0, round(RETENTION_DAYS - (now - c["first_saved"]) / 86400, 1)),
                        "preview": " ".join(first_user.split())[:140]})
        return sorted(out, key=lambda c: c["updated"] or 0, reverse=True)

    def get(self, key: str) -> dict:
        self.cleanup()
        path = self._path(key)
        if not path.exists():
            raise FileNotFoundError("That chat isn't backed up (or its 10 days are up).")
        chat = json.loads(path.read_text())
        chat["key"] = key
        chat["source_name"] = SOURCES[chat["source"]]
        return chat

    def search(self, query: str, limit: int = 8) -> list[dict]:
        """Chats whose messages mention the query's words, best first, with the matching lines."""
        self.cleanup()
        words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2] or [query.lower().strip()]
        hits = []
        for c in self._each():
            matches = [m for m in c["messages"] if any(w in m["text"].lower() for w in words)]
            title_hit = any(w in c.get("title", "").lower() for w in words)
            if not matches and not title_hit:
                continue
            score = sum(sum(m["text"].lower().count(w) for w in words) for m in matches) + 3 * title_hit
            snippets = [f'{m["role"]}: {" ".join(m["text"].split())[:200]}' for m in matches[:3]]
            hits.append({"chat": c["key"], "title": c.get("title", ""), "app": SOURCES[c["source"]],
                         "score": score, "matches": snippets})
        return sorted(hits, key=lambda h: h["score"], reverse=True)[:limit]


def as_markdown(chat: dict) -> str:
    """Render a chat as Markdown."""
    lines = [f"# {chat.get('title', 'Chat')}", "", f"{chat['source_name']} chat, backed up by Thread. {chat.get('url', '')}", ""]
    for m in chat["messages"]:
        lines += [f"**{'You' if m['role'] == 'user' else chat['source_name']}:**", "", m["text"].strip(), ""]
    return "\n".join(lines)
