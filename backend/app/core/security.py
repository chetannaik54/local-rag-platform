"""Password hashing and token helpers (standard library only)."""
import base64
import hashlib
import hmac
import os
import re
import secrets

from app.core.errors import AppError

_N, _R, _P = 2**14, 8, 1
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$|^[^@\s]+@localhost$")


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time check. Always does the scrypt work, even when the account does not exist."""
    if not stored:
        stored = _dummy_hash()
        password += "\x00"  # never matches the dummy
    try:
        _, n, r, p, salt, digest = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(got, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


_DUMMY: str | None = None


def _dummy_hash() -> str:
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_password(secrets.token_hex(8))
    return _DUMMY


def check_password_policy(password: str, min_length: int) -> None:
    if len(password) < min_length:
        raise AppError("weak_password", f"Password must be at least {min_length} characters.", 422)
    if len(password) > 200:
        raise AppError("weak_password", "Password is too long (max 200 characters).", 422)
    if password.lower() in {"password", "password123", "123456789012", "qwertyuiop12"}:
        raise AppError("weak_password", "That password is too common.", 422)


def normalize_email(email: str) -> str:
    e = email.strip().lower()
    if not _EMAIL.match(e) or len(e) > 200:
        raise AppError("validation_error", "Enter a valid email address.", 422)
    return e


def new_token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(24)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
