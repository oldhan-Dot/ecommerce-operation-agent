"""M2 DeepSeek 配置测试脚本。

用途：验证 AES-GCM 随机密文/企业绑定、严格地址及密钥校验、管理员权限、不可变版本与并发覆盖。
运行：backend 下 uv run pytest tests/test_model_settings.py -q。
前置：离线测试无需网络；MySQL 用例需 RUN_MYSQL_TESTS=1，开发测试库已迁移至 head。
使用固定的无效测试密钥，不向 DeepSeek 发请求；数据库测试沿用外层事务最终回滚。
"""

import base64
import os

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from test_identity import sign_in
from test_identity import system as system

from app.core.config import Settings
from app.core.errors import AppError
from app.model_settings import ModelSettingsInput, decrypt_secret, encrypt_secret, master_key
from app.models import AuditEvent, Enterprise, ModelConfig, User

TEST_SECRET = "demo-deepseek-key-not-real"


def settings(**changes):
    return Settings(
        _env_file=None,
        encryption_key=base64.urlsafe_b64encode(b"a" * 32).decode().rstrip("="),
        **changes,
    )


def config(**changes):
    return ModelConfig(
        enterprise_id=1,
        config_version=1,
        provider="deepseek",
        endpoint="https://api.deepseek.com",
        model_name="deepseek-flash",
        **changes,
    )


def body(**changes):
    return {
        "provider": "deepseek",
        "endpoint": "https://api.deepseek.com",
        "model_name": "deepseek-flash",
        "api_key": TEST_SECRET,
        "timeout_seconds": 120,
        "daily_limit": 100,
        **changes,
    }


def configure_app(client, *, allowed=True):
    """仅替换测试加密与域名参数，保留夹具的会话密钥、Origin 和数据库设置。"""
    test = settings()
    client.app.state.settings = client.app.state.settings.model_copy(
        update={
            "encryption_key": test.encryption_key,
            "encryption_key_id": test.encryption_key_id,
            "allowed_model_hosts": ["api.deepseek.com"] if allowed else [],
        }
    )


def test_authenticated_encryption_randomized():
    a, b = config(), config()
    encrypt_secret(a, TEST_SECRET, settings())
    encrypt_secret(b, TEST_SECRET, settings())
    assert a.nonce != b.nonce and a.secret_cipher != b.secret_cipher
    assert TEST_SECRET.encode() not in a.secret_cipher
    assert decrypt_secret(a, settings()) == decrypt_secret(b, settings()) == TEST_SECRET


@pytest.mark.parametrize(
    "field,value",
    [
        ("enterprise_id", 2),
        ("config_version", 2),
        ("model_name", "other"),
        ("endpoint", "https://elsewhere.example"),
        ("provider", "other"),
        ("key_id", "other"),
    ],
)
def test_secret_bound_to_config_and_enterprise(field, value):
    item = config()
    encrypt_secret(item, TEST_SECRET, settings())
    setattr(item, field, value)
    with pytest.raises(AppError) as exc:
        decrypt_secret(item, settings())
    assert exc.value.code == "MODEL_KEY_UNAVAILABLE" and TEST_SECRET not in str(exc.value)


@pytest.mark.parametrize(
    "key", ["", "a" * 32, "bad!base64", base64.urlsafe_b64encode(b"short").decode()]
)
def test_invalid_master_key_is_redacted(key):
    with pytest.raises(AppError):
        master_key(Settings(_env_file=None, encryption_key=key))


@pytest.mark.parametrize(
    "url",
    [
        "http://api.deepseek.com",
        "https://127.0.0.1",
        "https://169.254.169.254",
        "https://api.deepseek.com.evil.example",
        "https://api.deepseek.com:444",
        "https://secret@api.deepseek.com",
        "https://api.deepseek.com?a=secret",
        "https://api.deepseek.com/#secret",
        "https://api.deepseek.com/chat/completions",
        "https://api.deepseek.com:bad",
        "https://api.deepseek.com/ v1",
    ],
)
def test_invalid_endpoint_rejected(url):
    with pytest.raises(ValidationError):
        ModelSettingsInput.model_validate(body(endpoint=url))


def test_canonical_endpoint_and_secret_repr():
    value = ModelSettingsInput.model_validate(body(endpoint="https://API.DEEPSEEK.COM:443/v1/"))
    assert value.endpoint == "https://api.deepseek.com/v1"
    assert TEST_SECRET not in str(value) and TEST_SECRET not in repr(value)


@pytest.mark.parametrize(
    "change",
    [
        {"api_key": "********"},
        {"api_key": " has spaces "},
        {"api_key": "abc\n123456"},
        {"daily_limit": 0},
        {"daily_limit": True},
        {"timeout_seconds": 301},
        {"timeout_seconds": "120"},
        {"model_name": "\nmodel"},
        {"provider": "other"},
    ],
)
def test_strict_configuration_inputs(change):
    with pytest.raises(ValidationError):
        ModelSettingsInput.model_validate(body(**change))


mysql = pytest.mark.skipif(os.getenv("RUN_MYSQL_TESTS") != "1", reason="显式启用真实 MySQL")


@mysql
def test_config_versions_secret_redaction_and_stale_updates(system):
    client, db, people = system
    configure_app(client)
    headers = sign_in(client, people["admin"])
    initial = client.get("/api/v1/settings/model").json()["data"]
    assert initial["configured"] is False and initial["config_version"] == 0
    assert client.put("/api/v1/settings/model", headers=headers, json=body()).status_code == 428
    missing_key = body()
    del missing_key["api_key"]
    assert (
        client.put(
            "/api/v1/settings/model", headers={**headers, "If-Match": '"0"'}, json=missing_key
        ).status_code
        == 422
    )
    response = client.put(
        "/api/v1/settings/model", headers={**headers, "If-Match": '"0"'}, json=body()
    )
    assert response.status_code == 200, response.text
    first = response.json()["data"]
    assert first["config_version"] == 1 and first["connection_status"] == "UNTESTED"
    assert (
        TEST_SECRET not in response.text
        and "secret_cipher" not in response.text
        and "nonce" not in response.text
    )
    original = db.get(ModelConfig, int(first["id"]))
    encrypted = original.secret_cipher
    second = client.put(
        "/api/v1/settings/model",
        headers={**headers, "If-Match": '"1"'},
        json={**missing_key, "daily_limit": 20},
    )
    assert second.status_code == 200 and second.json()["data"]["config_version"] == 2
    active = db.get(ModelConfig, int(second.json()["data"]["id"]))
    assert decrypt_secret(active, client.app.state.settings) == TEST_SECRET
    assert active.nonce != original.nonce and active.secret_cipher != encrypted
    db.refresh(original)
    assert original.secret_cipher == encrypted and original.daily_limit == 100
    assert (
        client.put(
            "/api/v1/settings/model", headers={**headers, "If-Match": '"1"'}, json=body()
        ).status_code
        == 409
    )
    events = db.scalars(select(AuditEvent).where(AuditEvent.action == "MODEL_CONFIG_CREATED")).all()
    assert len(events) == 2 and all(TEST_SECRET not in str(e.redacted_diff) for e in events)
    assert client.get("/api/v1/settings/model").json()["data"]["config_version"] == 2
    enterprise = db.get(Enterprise, active.enterprise_id)
    # API 使用独立 Session 提交，测试夹具的 expire_on_commit=False 需显式刷新旧实体。
    db.refresh(enterprise)
    assert enterprise.active_model_config_id == active.id


@mysql
def test_roles_and_deployment_host_restriction(system):
    client, db, people = system
    configure_app(client, allowed=False)
    for role in ("op", "supervisor"):
        headers = sign_in(client, people[role])
        assert client.get("/api/v1/settings/model").status_code == 403
        assert (
            client.put(
                "/api/v1/settings/model", headers={**headers, "If-Match": '"0"'}, json=body()
            ).status_code
            == 403
        )
    headers = sign_in(client, people["admin"])
    response = client.put(
        "/api/v1/settings/model", headers={**headers, "If-Match": '"0"'}, json=body()
    )
    assert (
        response.status_code == 422 and response.json()["error"]["code"] == "MODEL_HOST_NOT_ALLOWED"
    )


@mysql
def test_enterprise_isolation_and_pointer_constraint(system):
    client, db, people = system
    configure_app(client)
    headers = sign_in(client, people["admin"])
    saved = client.put(
        "/api/v1/settings/model", headers={**headers, "If-Match": '"0"'}, json=body()
    )
    assert saved.status_code == 200
    config_id = int(saved.json()["data"]["id"])
    other = Enterprise(name="另一企业")
    db.add(other)
    db.flush()
    login_name = people["admin"]["login_name"] + "_other"
    actor = User(
        enterprise_id=other.id,
        login_name=login_name,
        role="ADMIN",
        must_change_password=False,
        password_hash=db.get(User, people["admin"]["id"]).password_hash,
    )
    db.add(actor)
    db.commit()
    with pytest.raises(IntegrityError), db.begin_nested():
        other.active_model_config_id = config_id
        db.flush()
    db.refresh(other)
    assert other.active_model_config_id is None
    sign_in(client, {"login_name": login_name})
    assert client.get("/api/v1/settings/model").json()["data"]["configured"] is False
