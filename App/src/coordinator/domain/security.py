"""Пароли, роли и маскирование секретов (ТЗ, п. 14 и 15). Только стандартная библиотека."""
import base64
import hashlib
import hmac
import os
import re
from enum import StrEnum

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
MIN_PASSWORD_LENGTH = 10


class Role(StrEnum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMINISTRATOR = "administrator"


_RANK = {Role.VIEWER: 1, Role.OPERATOR: 2, Role.ADMINISTRATOR: 3}


def role_allows(role: Role, required: Role) -> bool:
    return _RANK[role] >= _RANK[required]


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    encoded = base64.b64encode(salt).decode(), base64.b64encode(digest).decode()
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${encoded[0]}${encoded[1]}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        actual = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
        )
        return hmac.compare_digest(actual, base64.b64decode(digest))
    except ValueError:
        return False


# Для входа, когда пользователь не найден: время ответа не должно выдавать, есть ли такой логин.
DUMMY_PASSWORD_HASH = hash_password("нет такого пользователя")

_MASK = "***"
_SECRET_PATTERNS = (
    # Адрес с паролем: postgresql://user:secret@host
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s@/]+(@)", re.I), rf"\1{_MASK}\2"),
    # Заголовки и параметры: Authorization: Bearer x, token=x, "password": "x"
    (re.compile(r"(\bbearer\s+)[A-Za-z0-9._~+/=-]+", re.I), rf"\1{_MASK}"),
    (re.compile(
        r"""((?:password|passwd|pwd|secret|token|api[_-]?key|cookie|session|authorization)"""
        r"""["']?\s*[:=]\s*["']?)[^\s"',;&]+""",
        re.I,
    ), rf"\1{_MASK}"),
    # Закрытый ключ целиком.
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
     _MASK),
)


def mask_secrets(text: str) -> str:
    """Убирает из текста то, что нельзя писать в журнал (ТЗ, п. 15)."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text
