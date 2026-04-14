import base64
import hashlib
import hmac
import json
import os
import secrets
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app_config import PLATFORM_ENCRYPTION_KEY


_HASH_ITERATIONS = 390000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _HASH_ITERATIONS)
    return f"pbkdf2_sha256${_HASH_ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_str, salt_b64, digest_b64 = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_str)
        salt = base64.b64decode(salt_b64.encode("utf-8"))
        expected = base64.b64decode(digest_b64.encode("utf-8"))
    except Exception:
        return False
    current = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(current, expected)


def _fernet() -> Fernet:
    key = (PLATFORM_ENCRYPTION_KEY or "").strip()
    if not key:
        raise RuntimeError("PLATFORM_ENCRYPTION_KEY is missing")
    if len(key) == 44:
        try:
            base64.urlsafe_b64decode(key.encode("utf-8"))
            return Fernet(key.encode("utf-8"))
        except Exception:
            pass
    derived = base64.urlsafe_b64encode(hashlib.sha256(key.encode("utf-8")).digest())
    return Fernet(derived)


def encrypt_json(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return _fernet().encrypt(raw).decode("utf-8")


def decrypt_json(value: str) -> dict[str, Any]:
    if not value:
        return {}
    try:
        raw = _fernet().decrypt(value.encode("utf-8"))
    except InvalidToken as exc:
        raise RuntimeError("Unable to decrypt integration credentials") from exc
    return json.loads(raw.decode("utf-8"))


def mask_secret(value: str, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return value[:keep] + "..." + value[-keep:]
