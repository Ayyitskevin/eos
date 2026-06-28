"""Encrypt OAuth tokens at rest using Fernet derived from EOS_SECRET_KEY."""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from . import config


def _fernet() -> Fernet | None:
    material = config.TOKEN_ENCRYPTION_KEY
    if not material:
        return None
    key = base64.urlsafe_b64encode(hashlib.sha256(material.encode()).digest())
    return Fernet(key)


def encrypt(plain: str) -> str:
    f = _fernet()
    if not f:
        return plain
    return f.encrypt(plain.encode()).decode()


def decrypt(cipher: str) -> str:
    f = _fernet()
    if not f:
        return cipher
    try:
        return f.decrypt(cipher.encode()).decode()
    except InvalidToken:
        return cipher
