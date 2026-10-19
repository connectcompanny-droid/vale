"""Field-level encryption for personal document data (GDPR: data minimisation + security)."""

import json
from typing import Any

from cryptography.fernet import Fernet

from app.config import get_settings


def _fernet() -> Fernet:
    key = get_settings().fernet_key
    if not key:
        raise RuntimeError("FERNET_KEY is not set; refusing to store personal data unencrypted.")
    return Fernet(key.encode())


def encrypt_json(data: dict[str, Any]) -> bytes:
    return _fernet().encrypt(json.dumps(data, ensure_ascii=False).encode())


def decrypt_json(token: bytes) -> dict[str, Any]:
    return json.loads(_fernet().decrypt(token).decode())
