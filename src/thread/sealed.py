"""Encryption at rest for per-user credentials (Canvas token and feed URL, Google token).

Online, files are sealed with Fernet using THREAD_SECRET_KEY, which is kept outside the data volume.
Sealed files are prefixed "thread-sealed:v1:"; plaintext files are re-sealed on first read. Without a
key (local mode) files stay plaintext with mode 600.
"""

import os
from pathlib import Path

KEY_ENV = "THREAD_SECRET_KEY"
PREFIX = "thread-sealed:v1:"


class SecretKeyError(Exception):
    """The key is missing or wrong, so a sealed secret can't be read."""


class Sealer:
    def __init__(self, key: str):
        from cryptography.fernet import Fernet   # lazy import
        try:
            self._fernet = Fernet(key.strip().encode())
        except (ValueError, TypeError) as e:
            raise SecretKeyError(f"{KEY_ENV} isn't a valid key (make one with: python -c "
                                 "'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')") from e

    @classmethod
    def from_env(cls) -> "Sealer | None":
        key = os.environ.get(KEY_ENV, "").strip()
        return cls(key) if key else None

    def seal(self, text: str) -> str:
        return PREFIX + self._fernet.encrypt(text.encode()).decode()

    def open(self, sealed: str) -> str:
        from cryptography.fernet import InvalidToken
        try:
            return self._fernet.decrypt(sealed[len(PREFIX):].encode()).decode()
        except InvalidToken as e:
            raise SecretKeyError(f"Couldn't unlock a saved secret: {KEY_ENV} isn't the key it was saved with.") from e


def read_secret(path: Path, sealer: "Sealer | None") -> str | None:
    """The secret in `path`, or None if there isn't one. A plain file is sealed on the spot when a key exists."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    if text.startswith(PREFIX):
        if sealer is None:
            raise SecretKeyError(f"This secret is encrypted, but {KEY_ENV} isn't set.")
        return sealer.open(text.strip())
    if sealer is not None and text.strip():
        write_secret(path, text.strip(), sealer)   # upgrade a legacy plaintext file
    return text.strip() or None


def write_secret(path: Path, text: str, sealer: "Sealer | None") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(sealer.seal(text) if sealer is not None else text)
    tmp.chmod(0o600)
    tmp.replace(path)
