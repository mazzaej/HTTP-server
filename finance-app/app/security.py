"""Single-user auth (HMAC-signed cookie) and at-rest encryption for Plaid tokens."""
import hashlib
import hmac
import time

from cryptography.fernet import Fernet

from .config import settings

SESSION_TTL = 60 * 60 * 24 * 14  # 14 days


def _sign(payload: str) -> str:
    return hmac.new(settings.session_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_session() -> str:
    exp = str(int(time.time()) + SESSION_TTL)
    return f"{exp}.{_sign(exp)}"


def valid_session(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp, sig = token.split(".", 1)
    if not hmac.compare_digest(sig, _sign(exp)):
        return False
    return exp.isdigit() and int(exp) > time.time()


def check_password(pw: str) -> bool:
    expected = settings.app_password
    return bool(expected) and hmac.compare_digest(pw.encode(), expected.encode())


def _fernet() -> Fernet:
    key = settings.fernet_key
    if not key:
        raise RuntimeError("FERNET_KEY is not set; refusing to store Plaid tokens unencrypted.")
    return Fernet(key.encode())


def encrypt(s: str) -> str:
    return _fernet().encrypt(s.encode()).decode()


def decrypt(s: str) -> str:
    return _fernet().decrypt(s.encode()).decode()
