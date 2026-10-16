"""密码与会话摘要：数据库不保存原密码、原会话令牌或原 CSRF 令牌。"""

import hashlib
import hmac
import re

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.errors import AppError

hasher = PasswordHasher()
# 不存在的账号也执行密码校验，减弱通过时间差枚举账号的可能。
DUMMY_HASH = hasher.hash("dummy-password-that-is-never-a-valid-account")


def validate_password(value: str) -> str:
    """密码允许 12—64 字符，至少包含大小写、数字、其他字符中的两类。"""
    groups = sum(
        bool(re.search(pattern, value)) for pattern in (r"[a-z]", r"[A-Z]", r"\d", r"[^a-zA-Z0-9]")
    )
    if not 12 <= len(value) <= 64 or groups < 2:
        raise ValueError("密码须为12—64个字符，且至少包含两类字符")
    return value


def verify_password(value: str, encoded: str) -> bool:
    try:
        return hasher.verify(encoded, value)
    except (VerificationError, InvalidHashError):
        return False


def digest(value: str, pepper: str) -> bytes:
    if len(pepper) < 32:
        raise AppError(503, "AUTH_NOT_CONFIGURED", "请先配置至少32字符的 SESSION_PEPPER")
    return hmac.digest(pepper.encode(), value.encode(), hashlib.sha256)


def csrf_token(token: str, pepper: str) -> str:
    """CSRF 值由随机会话令牌派生，重载页面时可以安全恢复。"""
    return digest("csrf:" + token, pepper).hex()
