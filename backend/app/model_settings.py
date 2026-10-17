"""管理员 DeepSeek 配置：不可变版本、AES-256-GCM 加密、白名单与并发覆盖保护。"""

import base64
import binascii
import json
import re
import secrets
from typing import Literal
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import APIRouter, Depends, Header, Request
from pydantic import Field, SecretStr, field_validator
from sqlalchemy import func, select

from app.core.errors import AppError
from app.dependencies import administrator, get_db
from app.identity import audit, result, stamp
from app.models import Enterprise, ModelConfig, User
from app.schemas import Input

router = APIRouter(prefix="/api/v1/settings/model", tags=["model-settings"])
DEFAULT_ENDPOINT = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"


class ModelSettingsInput(Input):
    provider: Literal["deepseek"]
    endpoint: str = Field(max_length=2048)
    model_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
    api_key: SecretStr | None = Field(default=None, min_length=8, max_length=512)
    timeout_seconds: int = Field(ge=30, le=300, strict=True)
    daily_limit: int = Field(ge=1, le=2**31 - 1, strict=True)

    @field_validator("endpoint")
    @classmethod
    def endpoint_format(cls, value):
        """首个适配器只接官方 HTTPS 根地址或 /v1，不允许凭据、查询参数或任意代理。"""
        if not value.isascii() or any(c.isspace() for c in value):
            raise ValueError("请填写有效的 DeepSeek 官方 HTTPS 地址")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("请填写有效的 DeepSeek 官方 HTTPS 地址") from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname != "api.deepseek.com"
            or port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/") not in {"", "/v1"}
        ):
            raise ValueError("仅支持 https://api.deepseek.com 或其 /v1 地址")
        return DEFAULT_ENDPOINT + parsed.path.rstrip("/")

    @field_validator("api_key")
    @classmethod
    def key_format(cls, value):
        if value is not None:
            text = value.get_secret_value()
            if (
                not text.isascii()
                or any(ord(c) < 33 or ord(c) > 126 for c in text)
                or set(text) <= {"*", "•"}
            ):
                raise ValueError("API Key 不能包含空白、控制字符或使用掩码代替")
        return value


def master_key(settings):
    """主密钥沿用本机已有的 URL-safe Base64 编码，解码必须恰好 32 字节。"""
    try:
        text = settings.encryption_key.get_secret_value()
        raw = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
        if len(raw) != 32 or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", settings.encryption_key_id):
            raise ValueError
        return raw
    except (ValueError, binascii.Error) as exc:
        raise AppError(
            503, "MODEL_ENCRYPTION_NOT_CONFIGURED", "模型加密主密钥未正确配置，请联系管理员"
        ) from exc


def context(config):
    """将密文绑定到企业、不可变配置及用途，防止跨企业/跨配置挪用。"""
    return json.dumps(
        {
            "purpose": "model-api-key-v1",
            "enterprise_id": str(config.enterprise_id),
            "config_version": config.config_version,
            "provider": config.provider,
            "endpoint": config.endpoint,
            "model_name": config.model_name,
            "key_id": config.key_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def encrypt_secret(config, value, settings):
    config.key_id = settings.encryption_key_id
    config.nonce = secrets.token_bytes(12)
    config.secret_cipher = AESGCM(master_key(settings)).encrypt(
        config.nonce, value.encode(), context(config)
    )


def decrypt_secret(config, settings):
    if config.key_id != settings.encryption_key_id:
        raise AppError(503, "MODEL_KEY_UNAVAILABLE", "历史配置所需主密钥不可用，请重新填写 API Key")
    try:
        return (
            AESGCM(master_key(settings))
            .decrypt(config.nonce, config.secret_cipher, context(config))
            .decode()
        )
    except (InvalidTag, ValueError, UnicodeError) as exc:
        raise AppError(
            503, "MODEL_KEY_UNAVAILABLE", "模型密钥无法解密，请核对主密钥或重新填写 API Key"
        ) from exc


def configuration_view(config):
    """只返回明确白名单，密文、nonce、key_id 和原密钥均不下发。"""
    if config is None:
        return {
            "configured": False,
            "id": None,
            "config_version": 0,
            "provider": "deepseek",
            "endpoint": DEFAULT_ENDPOINT,
            "model_name": DEFAULT_MODEL,
            "secret_masked": None,
            "timeout_seconds": 120,
            "daily_limit": None,
            "connection_status": "UNTESTED",
        }
    return {
        "configured": True,
        "id": str(config.id),
        "config_version": config.config_version,
        "provider": config.provider,
        "endpoint": config.endpoint,
        "model_name": config.model_name,
        "secret_masked": "********",
        "timeout_seconds": config.timeout_seconds,
        "daily_limit": config.daily_limit,
        "connection_status": "UNTESTED",
        "created_at": stamp(config.created_at),
    }


@router.get("")
def get_configuration(request: Request, p=Depends(administrator), db=Depends(get_db)):
    enterprise = db.get(Enterprise, p.user.enterprise_id)
    config = (
        db.get(ModelConfig, enterprise.active_model_config_id)
        if enterprise.active_model_config_id
        else None
    )
    return result(request, configuration_view(config))


@router.put("")
def save_configuration(
    body: ModelSettingsInput,
    request: Request,
    if_match: str | None = Header(None),
    p=Depends(administrator),
    db=Depends(get_db),
):
    """企业锁串行分配配置版本；旧记录不修改，保存和脱敏审计一次提交。"""
    enterprise = db.scalar(
        select(Enterprise)
        .where(Enterprise.id == p.user.enterprise_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    actor = db.scalar(
        select(User)
        .where(User.id == p.user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        actor.role != "ADMIN"
        or actor.status != "ACTIVE"
        or actor.must_change_password
        or actor.enterprise_id != enterprise.id
    ):
        raise AppError(403, "AUTH_FORBIDDEN", "当前账号无权修改模型配置")
    current = (
        db.get(ModelConfig, enterprise.active_model_config_id)
        if enterprise.active_model_config_id
        else None
    )
    current_version = current.config_version if current else 0
    if if_match is None:
        raise AppError(428, "PRECONDITION_REQUIRED", "保存配置需要 If-Match 版本条件")
    if if_match != f'"{current_version}"':
        raise AppError(409, "DATA_VERSION_CONFLICT", "模型配置已变化，请读取最新版本并重新核对")
    settings = request.app.state.settings
    if "api.deepseek.com" not in {h.lower() for h in settings.allowed_model_hosts}:
        raise AppError(422, "MODEL_HOST_NOT_ALLOWED", "DeepSeek 官方域名尚未加入服务端允许集合")
    if body.api_key is None and current is None:
        raise AppError(422, "MODEL_KEY_REQUIRED", "首次配置需要填写 API Key")
    secret = body.api_key.get_secret_value() if body.api_key else decrypt_secret(current, settings)
    last_version = (
        db.scalar(
            select(func.max(ModelConfig.config_version)).where(
                ModelConfig.enterprise_id == enterprise.id
            )
        )
        or 0
    )
    config = ModelConfig(
        enterprise_id=enterprise.id,
        config_version=last_version + 1,
        created_by=actor.id,
        **body.model_dump(exclude={"api_key"}),
    )
    encrypt_secret(config, secret, settings)
    db.add(config)
    db.flush()
    enterprise.active_model_config_id = config.id
    audit(
        db,
        request,
        actor,
        "MODEL_CONFIG_CREATED",
        config,
        changes={
            "previous_config_version": current_version,
            "config_version": config.config_version,
            "provider": config.provider,
            "model_name": config.model_name,
            "timeout_seconds": config.timeout_seconds,
            "daily_limit": config.daily_limit,
            "secret_replaced": body.api_key is not None,
        },
    )
    db.commit()
    return result(request, configuration_view(config))
