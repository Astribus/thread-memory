"""TOTP (RFC 6238) two-factor authentication and one-time recovery codes.

- Secrets are 160 random bits (RFC 4226 §4), encrypted at rest with thread.sealed.
- Codes are accepted within ±1 step, and each step only once (NIST SP 800-63B §3.1.4.2).
- Recovery codes are single-use and stored as salted scrypt hashes.

No web dependencies, so `thread-admin` can import it.
"""

import base64
import hashlib
import hmac
import io
import secrets
import struct
import time
from urllib.parse import quote

ISSUER = "Thread"
STEP = 30   # RFC 6238 default
DIGITS = 6
DRIFT_STEPS = 1   # accept ±1 step
RECOVERY_COUNT = 10       # recovery codes per set
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # no look-alike characters


def new_secret() -> str:
    """A fresh app secret, base32 without padding (what authenticator apps expect)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    s = secret.replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def code_at(secret: str, step: int) -> str:
    """The code for one 30-second step (RFC 4226 HOTP with the step number as the counter)."""
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10 ** DIGITS).zfill(DIGITS)


def current_step(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // STEP)


def match_step(secret: str, code: str, last_step: int = -1, now: float | None = None) -> int | None:
    """The step `code` belongs to (within the drift window and newer than `last_step`), or None.
    Every candidate is compared in constant time, so timing doesn't reveal how close a guess was."""
    code = "".join(ch for ch in str(code) if ch.isdigit())
    if len(code) != DIGITS:
        return None
    here, found = current_step(now), None
    for step in range(here - DRIFT_STEPS, here + DRIFT_STEPS + 1):
        if hmac.compare_digest(code_at(secret, step), code) and step > last_step and found is None:
            found = step
    return found


def spaced(secret: str) -> str:
    """'ABCD EFGH ...' so the key is easy to type by hand."""
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))


def otpauth_uri(email: str, secret: str) -> str:
    """otpauth:// URI with issuer and account name."""
    return (f"otpauth://totp/{quote(ISSUER)}:{quote(email)}?secret={secret}&issuer={quote(ISSUER)}"
            f"&algorithm=SHA1&digits={DIGITS}&period={STEP}")


def qr_data_uri(text: str) -> str:
    """QR code as an SVG data URI, rendered locally with segno."""
    import segno
    out = io.BytesIO()
    segno.make(text, error="m").save(out, kind="svg", scale=1, border=4, xmldecl=False, omitsize=True,
                                     dark="#000", light="#fff")
    return "data:image/svg+xml;base64," + base64.b64encode(out.getvalue()).decode()


# ---- recovery codes ----
def new_recovery_codes(n: int = RECOVERY_COUNT) -> list[str]:
    """'k7mqp-9xd4t'-style codes: 10 characters from 31 (~49 bits each), easy to read off paper."""
    raw = ["".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(10)) for _ in range(n)]
    return [f"{r[:5]}-{r[5:]}" for r in raw]


def normalize_recovery(code: str) -> str:
    return "".join(ch for ch in code.lower() if ch.isalnum())


def hash_recovery(code: str, salt: bytes | None = None) -> str:
    """Salted scrypt with a lower cost (N=2^12) than passwords: codes are high-entropy."""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(normalize_recovery(code).encode(), salt=salt, n=2**12, r=8, p=1, dklen=32)
    return f"scrypt12${salt.hex()}${digest.hex()}"


def recovery_index(code: str, hashes: list[str]) -> int | None:
    """Which stored hash this recovery code matches, or None. Checks every hash (no early exit)."""
    if len(normalize_recovery(code)) != 10:
        return None
    found = None
    for i, stored in enumerate(hashes):
        try:
            _, salt, _digest = stored.split("$")
        except ValueError:
            continue
        if hmac.compare_digest(hash_recovery(code, bytes.fromhex(salt)), stored) and found is None:
            found = i
    return found


def looks_like_app_code(text: str) -> bool:
    digits = "".join(ch for ch in text if not ch.isspace())
    return digits.isdigit() and len(digits) == DIGITS
