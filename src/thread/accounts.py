"""Account store (users, invites, hashed tokens and sessions) and the `thread-admin` CLI.

Deliberately free of web/MCP imports so the CLI fits next to the server on a 256 MB machine.
thread.auth builds the sign-in pages and OAuth server on top of this.
"""

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("THREAD_CONFIG_DIR", Path.home() / ".config" / "thread"))
AUTH_FILE = CONFIG_DIR / "accounts.json"


SESSION_TTL = 7 * 24 * 60 * 60   # browser session
SESSION_COOKIE = "thread_session"
ACCOUNT_COOKIE = "thread_acct"
ACCESS_TTL = 60 * 60              # 1 hour
REFRESH_TTL = 14 * 24 * 60 * 60   # 14 days for a connected AI app
CODE_TTL = 5 * 60   # authorization code
PENDING_TTL = 10 * 60   # pending OAuth request
MAX_FAILURES, LOCK_SECONDS = 5, 15 * 60   # per account
MIN_PASSWORD = 12
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
WRONG = "Wrong email or password."
LOCKED = "Too many wrong passwords. Try again in 15 minutes."
STEP_UP_SECONDS = 15 * 60   # max auth age for sensitive actions
SIGNIN_KEEP, SIGNIN_DAYS = 20, 30   # recent sign-ins: count and age limit
TWO_FA_PENDING_TTL = 15 * 60   # time to confirm a new TOTP secret


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
    except ValueError:
        return False
    return hmac.compare_digest(hash_password(password, bytes.fromhex(salt)), stored)


# Dummy hash so unknown emails take as long as wrong passwords.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def normalize_email(email: str) -> str:
    return email.strip().lower()


def new_invite_code() -> str:
    """e.g. 'k7mq-p9xd-4tw2': lowercase alphanumerics without look-alike characters."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


class AuthStore:
    """Persistent auth state in ~/.config/thread/accounts.json (mode 600)."""

    def __init__(self, path: Path = AUTH_FILE, sealer=None):
        """`sealer` encrypts TOTP secrets; without it 2FA can't be enabled (disabling doesn't need it)."""
        self.path = path
        self.sealer = sealer
        self._load()

    def _file_stamp(self):
        try:
            st = self.path.stat()
            return (st.st_mtime_ns, st.st_size, st.st_ino)
        except FileNotFoundError:
            return None

    def _load(self) -> None:
        self._stamp = self._file_stamp()
        try:
            self._data = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self._data = {}
        for key in ("users", "invites", "clients", "access", "refresh", "sessions", "backup_keys", "used_refresh"):
            self._data.setdefault(key, {})

    @property
    def data(self) -> dict:
        # The server and `thread-admin` (via `fly ssh`) share this file; reload when it changes on disk
        # so neither overwrites the other's writes.
        if self._file_stamp() != self._stamp:
            self._load()
        return self._data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=1))
        tmp.chmod(0o600)
        tmp.replace(self.path)
        self._stamp = self._file_stamp()

    # ---- accounts ----
    def user_id(self, email: str) -> str | None:
        email = normalize_email(email)
        return next((uid for uid, u in self.data["users"].items() if u["email"] == email), None)

    def email(self, user_id: str) -> str:
        return self.data["users"].get(user_id, {}).get("email", "")

    def check(self, email: str, password: str) -> str | None:
        """The account id if the email and password match, else None (same time either way)."""
        uid = self.user_id(email)
        stored = self.data["users"][uid]["password"] if uid else _DUMMY_HASH
        return uid if check_password(password, stored) and uid else None

    def create_user(self, email: str, password: str) -> str:
        email = normalize_email(email)
        if not EMAIL_RE.match(email):
            raise ValueError("That doesn't look like an email address.")
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"Use at least {MIN_PASSWORD} characters for your password.")
        if self.user_id(email):
            raise ValueError("There's already an account with that email. Sign in instead.")
        uid = secrets.token_hex(8)   # folder name on disk; never derived from the email
        self.data["users"][uid] = {"email": email, "password": hash_password(password),
                                   "created": int(time.time())}
        self.save()
        return uid

    def set_password(self, user_id: str, password: str) -> None:
        if user_id not in self.data["users"]:
            raise KeyError(user_id)
        self.data["users"][user_id]["password"] = hash_password(password)
        # Changing the password revokes all of this user's sessions and tokens.
        for kind in ("access", "refresh", "sessions", "backup_keys"):
            self.data[kind] = {k: v for k, v in self.data[kind].items() if v.get("user") != user_id}
        self.save()

    # ---- two-factor (thread.twofactor) ----
    def has_2fa(self, user_id: str) -> bool:
        return bool(self.data["users"].get(user_id, {}).get("totp"))

    def twofa_info(self, user_id: str) -> dict:
        """What Settings shows. Never the secret or the recovery codes."""
        t = self.data["users"].get(user_id, {}).get("totp")
        return {"on": bool(t), "since": t["since"] if t else None,
                "recovery_left": sum(1 for h in t["recovery"] if h) if t else 0}

    def start_2fa(self, user_id: str) -> str:
        """A new app secret for this person, kept (encrypted) until they type a code from it. Returns the secret."""
        from thread import twofactor
        if self.sealer is None:
            raise ValueError("Two-step sign-in needs THREAD_SECRET_KEY set on the server.")
        if self.has_2fa(user_id):
            raise ValueError("Two-step sign-in is already on.")
        secret = twofactor.new_secret()
        self.data["users"][user_id]["totp_pending"] = {"secret": self.sealer.seal(secret), "created": int(time.time())}
        self.save()
        return secret

    def confirm_2fa(self, user_id: str, code: str) -> list[str] | None:
        """Turn two-step sign-in on if `code` comes from the secret shown in start_2fa. Returns the recovery
        codes (the only time they exist in plain form), or None if the code is wrong."""
        from thread import twofactor
        user = self.data["users"][user_id]
        pending = user.get("totp_pending")
        if not pending or time.time() - pending["created"] > TWO_FA_PENDING_TTL:
            raise ValueError("That setup took too long. Start again.")
        secret = self.sealer.open(pending["secret"])
        step = twofactor.match_step(secret, code)
        if step is None:
            return None
        codes = twofactor.new_recovery_codes()
        user.pop("totp_pending", None)
        user["totp"] = {"secret": pending["secret"], "since": int(time.time()), "last_step": step,
                        "recovery": [twofactor.hash_recovery(c) for c in codes]}
        self.save()
        return codes

    def check_second_step(self, user_id: str, text: str) -> str | None:
        """'app' or 'recovery' if `text` is a valid, unused code for this account, else None. A good app code
        can't be used again (its step is remembered); a recovery code is crossed out."""
        from thread import twofactor
        t = self.data["users"].get(user_id, {}).get("totp")
        if not t or self.sealer is None:
            return None
        if twofactor.looks_like_app_code(text):
            step = twofactor.match_step(self.sealer.open(t["secret"]), text, t.get("last_step", -1))
            if step is None:
                return None
            t["last_step"] = step
            self.save()
            return "app"
        i = twofactor.recovery_index(text, t["recovery"])
        if i is None:
            return None
        t["recovery"][i] = ""   # blank out; keep the list length for the remaining count
        self.save()
        return "recovery"

    def new_recovery_codes(self, user_id: str) -> list[str]:
        """A fresh set; the old codes stop working."""
        from thread import twofactor
        t = self.data["users"][user_id].get("totp")
        if not t:
            raise ValueError("Two-step sign-in is off.")
        codes = twofactor.new_recovery_codes()
        t["recovery"] = [twofactor.hash_recovery(c) for c in codes]
        self.save()
        return codes

    def disable_2fa(self, user_id: str) -> bool:
        """Turn two-step sign-in off (Settings, after password + code; or `thread-admin twofa-off`)."""
        user = self.data["users"].get(user_id)
        if not user or not (user.get("totp") or user.get("totp_pending")):
            return False
        user.pop("totp", None)
        user.pop("totp_pending", None)
        self.save()
        return True

    # ---- browser sessions and recent sign-ins ----
    def new_session(self, user_id: str, device: str = "", how: str = "password") -> str:
        """A browser session for this account; returns the cookie value (only its hash is stored). `auth_at` is
        when the person last proved who they are, for step-up (STEP_UP_SECONDS)."""
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.data["sessions"] = {k: v for k, v in self.data["sessions"].items() if v["expires_at"] > now}
        self.data["sessions"][_hash_token(token)] = {"user": user_id, "expires_at": now + SESSION_TTL, "auth_at": now,
                                                     "created": now, "device": device[:80]}
        self.note_signin(user_id, device, how)
        self.save()
        return token

    def note_signin(self, user_id: str, device: str, how: str, count: bool = True) -> None:
        """Remember a sign-in for Settings → Account → Recent sign-ins. Device = browser and system only
        ("Safari on iPhone"); no IP address or place is kept."""
        user = self.data["users"].get(user_id)
        if user is None:
            return
        cutoff = time.time() - SIGNIN_DAYS * 86400
        recent = [s for s in user.get("signins", []) if s["at"] > cutoff]
        recent.append({"at": int(time.time()), "device": device[:80], "how": how})
        user["signins"] = recent[-SIGNIN_KEEP:]
        if count:   # counts toward the 2FA prompt
            user["signin_count"] = user.get("signin_count", 0) + 1

    def signins(self, user_id: str) -> list[dict]:
        cutoff = time.time() - SIGNIN_DAYS * 86400
        return [s for s in reversed(self.data["users"].get(user_id, {}).get("signins", [])) if s["at"] > cutoff]

    def session_entry(self, token: str | None) -> dict | None:
        if not token:
            return None
        entry = self.data["sessions"].get(_hash_token(token))
        if not entry or entry["expires_at"] <= time.time() or entry.get("user") not in self.data["users"]:
            return None
        return entry

    def session_fresh(self, token: str | None) -> bool:
        """Did this browser's person prove who they are in the last STEP_UP_SECONDS?"""
        entry = self.session_entry(token)
        return bool(entry) and time.time() - entry.get("auth_at", 0) < STEP_UP_SECONDS

    def mark_fresh(self, token: str) -> None:
        entry = self.session_entry(token)
        if entry:
            entry["auth_at"] = time.time()
            self.save()

    def active_sessions(self, user_id: str) -> int:
        now = time.time()
        return sum(1 for v in self.data["sessions"].values() if v.get("user") == user_id and v["expires_at"] > now)

    def sign_out_others(self, user_id: str, keep_token: str | None, apps: bool = True) -> None:
        """Sign this person out of every other browser (and, with `apps`, every connected AI app). The chat-backup
        key is left alone: it can only upload chats, never read them."""
        keep = _hash_token(keep_token) if keep_token else None
        self.data["sessions"] = {k: v for k, v in self.data["sessions"].items()
                                 if v.get("user") != user_id or k == keep}
        if apps:
            for kind in ("access", "refresh"):
                self.data[kind] = {k: v for k, v in self.data[kind].items() if v.get("user") != user_id}
        self.save()

    # ---- chat-backup keys (browser extension) ----
    def new_backup_key(self, user_id: str) -> str:
        """A new key for this account's extension. Making one replaces the old one."""
        self.data["backup_keys"] = {k: v for k, v in self.data["backup_keys"].items() if v["user"] != user_id}
        key = "thr_" + secrets.token_urlsafe(24)
        self.data["backup_keys"][_hash_token(key)] = {"user": user_id, "created": int(time.time())}
        self.save()
        return key

    def backup_user(self, key: str) -> str | None:
        entry = self.data["backup_keys"].get(_hash_token(key.strip()))
        return entry["user"] if entry and entry["user"] in self.data["users"] else None

    def has_backup_key(self, user_id: str) -> bool:
        return any(v["user"] == user_id for v in self.data["backup_keys"].values())

    def connected_apps(self, user_id: str) -> list[str]:
        """AI apps signed in to this account right now (an unexpired refresh token), by friendly name:
        "Claude", "ChatGPT", or whatever name the app registered with. Used by the setup guide."""
        now, names = time.time(), []
        for token in self.data["refresh"].values():
            if token.get("user") != user_id or token.get("expires_at", 0) <= now:
                continue
            raw = (self.data["clients"].get(token.get("client_id")) or {}).get("client_name") or "An AI app"
            low = raw.lower()
            name = "Claude" if "claude" in low else "ChatGPT" if ("chatgpt" in low or "openai" in low) else raw
            if name not in names:
                names.append(name)
        return sorted(names)

    # ---- invites ----
    def make_invite(self, note: str = "", by: str = "") -> str:
        """`by` is who's inviting; the sign-up page shows "<name> invited you"."""
        code = new_invite_code()
        self.data["invites"][_hash_token(code)] = {"note": note, "by": by, "created": int(time.time())}
        self.save()
        return code

    def invite_valid(self, code: str) -> bool:
        return _hash_token(code.strip().lower()) in self.data["invites"]

    def invite_info(self, code: str) -> dict | None:
        """The invite's record (note, by), or None if the code doesn't work."""
        return self.data["invites"].get(_hash_token(code.strip().lower()))

    def session_user(self, token: str | None) -> str | None:
        """The account a browser session cookie belongs to, or None if it's missing, expired or unknown.
        Shared by the home screen and the AI-app consent page ("Continue as you")."""
        entry = self.session_entry(token)
        return entry["user"] if entry else None

    def use_invite(self, code: str) -> None:
        self.data["invites"].pop(_hash_token(code.strip().lower()), None)
        self.save()


def admin_cli() -> None:
    """`uv run thread-admin ...`: the owner's tools for running Thread online.
      invite [note] [--by NAME]  make a one-time sign-up link
      users                  list accounts
      reset EMAIL [--2fa]    give someone a new temporary password (signs them out everywhere); --2fa also
                             turns off their two-step sign-in (lost phone AND recovery codes)
      twofa-off EMAIL        turn off someone's two-step sign-in only (they keep their password)
      folder EMAIL           print where that account's files live (used by scripts/upload-brain.sh)
      feedback [N]           the latest N bug reports and ideas (default 20), newest first"""
    parser = argparse.ArgumentParser(prog="thread-admin")
    sub = parser.add_subparsers(dest="command", required=True)
    invite = sub.add_parser("invite", help="make a one-time sign-up code")
    invite.add_argument("note", nargs="?", default="", help="who it's for (only you see this)")
    invite.add_argument("--by", default=os.environ.get("THREAD_INVITER", ""),
                        help='your first name, shown as "<name> invited you" (default: $THREAD_INVITER)')
    sub.add_parser("users", help="list accounts")
    reset = sub.add_parser("reset", help="give someone a new temporary password")
    reset.add_argument("email")
    reset.add_argument("--2fa", dest="twofa", action="store_true",
                       help="also turn off two-step sign-in (only after checking it's really them; see DEPLOY.md)")
    twofa = sub.add_parser("twofa-off", help="turn off someone's two-step sign-in")
    twofa.add_argument("email")
    folder = sub.add_parser("folder", help="print where an account's files live")
    folder.add_argument("email")
    fb = sub.add_parser("feedback", help="show the latest bug reports and ideas")
    fb.add_argument("n", nargs="?", type=int, default=20)
    args = parser.parse_args()

    if args.command == "feedback":
        from thread.feedback import Feedback, describe, feedback_file
        path = feedback_file(online=True)
        if not path.exists() and feedback_file(online=False).exists():
            path = feedback_file(online=False)   # local-mode reports
        reports = Feedback(path).latest(args.n)
        print("\n\n".join(describe(r) for r in reports) if reports else "No reports yet.")
        return

    store = AuthStore()
    if args.command == "invite":
        code = store.make_invite(args.note, by=args.by)
        # Share the link; it prefills the code.
        public = os.environ.get("THREAD_PUBLIC_URL", "http://127.0.0.1:8770").rstrip("/")
        print(f"Invite code: {code}\nInvite link: {public}/join/{code}\n"
              "It works once. Text them the link; it opens sign-up with the code filled in.")
    elif args.command == "users":
        for uid, u in sorted(store.data["users"].items(), key=lambda kv: kv[1]["created"]):
            print(f"{u['email']}  (joined {time.strftime('%Y-%m-%d', time.localtime(u['created']))}, folder {uid})")
        print(f"{len(store.data['users'])} accounts, {len(store.data['invites'])} unused invite codes")
    elif args.command == "folder":
        # Mirrors thread.workspace.data_dir without importing the app.
        data_dir = lambda: Path(os.environ.get("THREAD_DATA_DIR", Path.home() / ".config" / "thread" / "data"))
        uid = store.user_id(args.email)
        if not uid:
            raise SystemExit(f"No account with the email {args.email}.")
        print(data_dir() / "users" / uid)
    elif args.command == "twofa-off":
        uid = store.user_id(args.email)
        if not uid:
            raise SystemExit(f"No account with the email {args.email}.")
        if not store.disable_2fa(uid):
            raise SystemExit(f"{store.email(uid)} doesn't have two-step sign-in on.")
        store.sign_out_others(uid, None)
        _admin_log(store, uid, "twofa-off")
        print(f"Two-step sign-in is off for {store.email(uid)}, and they're signed out everywhere.\n"
              "It shows in their Recent sign-ins. Ask them to turn it back on in Settings.")
    elif args.command == "reset":
        uid = store.user_id(args.email)
        if not uid:
            raise SystemExit(f"No account with the email {args.email}.")
        temporary = secrets.token_urlsafe(12)
        store.set_password(uid, temporary)
        cleared = args.twofa and store.disable_2fa(uid)
        _admin_log(store, uid, "reset+twofa-off" if cleared else "reset")
        print(f"Temporary password for {store.email(uid)}: {temporary}\n"
              + ("Two-step sign-in is OFF too.\n" if cleared else "")
              + "They're signed out everywhere. Tell them to change it in Settings after signing in.")


def _admin_log(store: AuthStore, user_id: str, action: str) -> None:
    """Record an admin action in admin-log.jsonl and in the user's recent sign-ins."""
    line = {"at": int(time.time()), "action": action, "user": user_id}
    log = store.path.with_name("admin-log.jsonl")
    with log.open("a") as f:
        f.write(json.dumps(line) + "\n")
    log.chmod(0o600)
    store.note_signin(user_id, "Thread support", {"reset": "password reset by support",
                                                  "twofa-off": "two-step sign-in turned off by support",
                                                  "reset+twofa-off": "password reset and two-step sign-in turned off by support"}[action], count=False)
    store.save()


