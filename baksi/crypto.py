"""Шифрование токенов ботов и хеши для сессий. Ключ берётся из SECRET_KEY: без него токены из бэкапа не открыть."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken


class Vault:
    def __init__(self, secret_key: str) -> None:
        key = base64.urlsafe_b64encode(hashlib.sha256(("baksi-vault:" + secret_key).encode()).digest())
        self._fernet = Fernet(key)
        self._hmac_key = hashlib.sha256(("baksi-hmac:" + secret_key).encode()).digest()

    def encrypt(self, text: str) -> str:
        return self._fernet.encrypt(text.encode()).decode()

    def decrypt(self, blob: str | None) -> str | None:
        if not blob:
            return None
        try:
            return self._fernet.decrypt(blob.encode()).decode()
        except InvalidToken:
            return None

    def hash(self, token: str) -> str:
        """Хеш одноразовых ссылок и сессий: в базе лежит только он, сам токен не восстановить."""
        return hmac.new(self._hmac_key, token.encode(), hashlib.sha256).hexdigest()


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def bind_code() -> str:
    """Короткий код для /bind: 6 цифр, легко набрать с телефона."""
    return f"{secrets.randbelow(900000) + 100000}"


def mask_token(token: str) -> str:
    if ":" not in token:
        return "•••"
    head, tail = token.split(":", 1)
    return f"{head}:{'•' * 8}{tail[-4:]}"
