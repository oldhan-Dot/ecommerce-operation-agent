"""M1 身份权限集成测试脚本。

用途：在真实 MySQL 上验证 Cookie 会话、CSRF、锁定、账号管理、店铺授权及审计。
运行：PowerShell 设置 $env:RUN_MYSQL_TESTS='1' 后，在 backend 执行 uv run pytest。
前置：backend/.env 已配置开发 MySQL，且 uv run alembic upgrade head 已执行。
隔离：每个用例使用最外层事务和请求级 SAVEPOINT，结束后回滚，不提交测试账号。
仅允许开发/测试配置；不使用 SQLite 模拟 MySQL 的行锁和唯一键行为。
"""

import os
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import make_engine
from app.core.security import hasher
from app.dependencies import get_db
from app.main import create_app
from app.models import AuditEvent, AuthSession, Enterprise, Store, User, utcnow

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_MYSQL_TESTS") != "1", reason="设置 RUN_MYSQL_TESTS=1 显式启用真实MySQL测试"
)
PASSWORD = "TestPassword-123!"  # 仅供事务回滚内的临时测试账号，不是应用默认密码。
ORIGIN = "http://127.0.0.1:5173"


@pytest.fixture
def system():
    """共享一个受控外层事务，每次 HTTP 请求在其中创建可提交的独立 SAVEPOINT。"""
    settings = get_settings()
    assert settings.app_env in {"dev", "test"}, "禁止对预发/生产配置执行此测试"
    assert settings.database_configured, "请先配置开发数据库"
    engine = make_engine(settings)
    connection = engine.connect()
    outer = connection.begin()
    db = Session(connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
    suffix = uuid4().hex[:12]
    enterprise = Enterprise(name=f"test-{suffix}")
    db.add(enterprise)
    db.flush()
    people = {}
    for name, role in (("admin", "ADMIN"), ("op", "OPERATOR"), ("supervisor", "SUPERVISOR")):
        person = User(
            enterprise_id=enterprise.id,
            login_name=f"{name}_{suffix}",
            role=role,
            password_hash=hasher.hash(PASSWORD),
            must_change_password=False,
        )
        db.add(person)
        db.flush()
        people[name] = {"id": person.id, "login_name": person.login_name}
    db.commit()
    app = create_app(settings.model_copy(update={"app_env": "test"}))

    def override_db():
        with Session(
            connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ) as request_db:
            yield request_db

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app, headers={"Origin": ORIGIN}) as client:
            yield client, db, people
    finally:
        db.close()
        outer.rollback()
        connection.close()
        engine.dispose()


def sign_in(client, person, password=PASSWORD):
    response = client.post(
        "/api/v1/auth/login", json={"login_name": person["login_name"], "password": password}
    )
    assert response.status_code == 200, response.text
    return {"X-CSRF-Token": response.json()["data"]["csrf_token"]}


def new_store(client, headers, code="TEST-A"):
    response = client.post(
        "/api/v1/stores",
        headers=headers,
        json={"code": code, "name": "事务内测试店铺", "platform": "手工维护"},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def version(value):
    return {"If-Match": f'"{value}"'}


def test_session_cookie_and_csrf(system):
    client, db, people = system
    response = client.post(
        "/api/v1/auth/login",
        json={"login_name": people["admin"]["login_name"], "password": PASSWORD},
    )
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]
    data = response.json()["data"]
    assert client.get("/api/v1/auth/session").json()["data"]["csrf_token"] == data["csrf_token"]
    assert (
        client.post("/api/v1/stores", json={"code": "X", "name": "X", "platform": "X"}).status_code
        == 403
    )
    assert (
        client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": data["csrf_token"]}).status_code
        == 204
    )
    assert client.get("/api/v1/auth/session").status_code == 401
    session = db.scalar(select(AuthSession).where(AuthSession.user_id == people["admin"]["id"]))
    assert session.revoked_at is not None


def test_wrong_or_missing_origin_rejected(system):
    client, _, people = system
    payload = {"login_name": people["admin"]["login_name"], "password": PASSWORD}
    assert (
        client.post(
            "/api/v1/auth/login", json=payload, headers={"Origin": "https://evil.invalid"}
        ).status_code
        == 403
    )
    client.headers.pop("Origin")
    assert client.post("/api/v1/auth/login", json=payload).status_code == 403


def test_account_lock_after_five_failures(system):
    client, db, people = system
    for _ in range(5):
        response = client.post(
            "/api/v1/auth/login",
            json={"login_name": people["op"]["login_name"], "password": "wrong"},
        )
        assert response.status_code == 401
    response = client.post(
        "/api/v1/auth/login", json={"login_name": people["op"]["login_name"], "password": PASSWORD}
    )
    assert response.status_code == 423
    db.expire_all()
    person = db.get(User, people["op"]["id"])
    assert person.failed_count == 5
    person.locked_until = utcnow() - timedelta(seconds=1)
    db.commit()
    sign_in(client, people["op"])


def test_login_source_rate_limit(system):
    client, _, _ = system
    for _ in range(20):
        response = client.post(
            "/api/v1/auth/login", json={"login_name": "missing-test-account", "password": "wrong"}
        )
        assert response.status_code == 401
    assert (
        client.post(
            "/api/v1/auth/login", json={"login_name": "missing-test-account", "password": "wrong"}
        ).status_code
        == 429
    )


@pytest.mark.parametrize("role", ["op", "supervisor"])
def test_non_admin_cannot_manage_users_or_stores(system, role):
    client, _, people = system
    headers = sign_in(client, people[role])
    assert client.get("/api/v1/users").status_code == 403
    assert client.get("/api/v1/audit-events").status_code == 403
    assert (
        client.post(
            "/api/v1/stores", headers=headers, json={"code": "X", "name": "X", "platform": "X"}
        ).status_code
        == 403
    )


def test_live_store_scope_and_revocation(system):
    client, _, people = system
    admin = sign_in(client, people["admin"])
    a = new_store(client, admin)
    b = new_store(client, admin, "TEST-B")
    op_id = people["op"]["id"]
    response = client.put(
        f"/api/v1/users/{op_id}/store-grants",
        headers=admin | version(1),
        json={"store_ids": [a["id"]]},
    )
    assert response.status_code == 200
    op = sign_in(client, people["op"])
    assert client.get("/api/v1/stores").json()["data"]["total"] == 1
    assert client.get(f"/api/v1/stores/{a['id']}").status_code == 200
    assert client.get(f"/api/v1/stores/{b['id']}").status_code == 404
    op_cookie = client.cookies.get("ea_session")
    admin = sign_in(client, people["admin"])
    assert (
        client.put(
            f"/api/v1/users/{op_id}/store-grants",
            headers=admin | version(2),
            json={"store_ids": []},
        ).status_code
        == 200
    )
    client.cookies.set("ea_session", op_cookie, domain="testserver.local", path="/")
    assert client.get(f"/api/v1/stores/{a['id']}").status_code == 404
    assert client.get("/api/v1/stores").json()["data"]["total"] == 0
    assert op["X-CSRF-Token"]  # 取消店铺授权不必退出账号，但资源权限即时失效。


def test_foreign_enterprise_store_cannot_be_granted(system):
    client, db, people = system
    another = Enterprise(name="other-test-enterprise")
    db.add(another)
    db.flush()
    foreign = Store(enterprise_id=another.id, code="OTHER", name="另一企业", platform="test")
    db.add(foreign)
    db.commit()
    headers = sign_in(client, people["admin"]) | version(1)
    response = client.put(
        f"/api/v1/users/{people['op']['id']}/store-grants",
        headers=headers,
        json={"store_ids": [str(foreign.id)]},
    )
    assert response.status_code == 404


def test_disable_revokes_existing_login(system):
    client, _, people = system
    sign_in(client, people["op"])
    old_cookie = client.cookies.get("ea_session")
    headers = sign_in(client, people["admin"]) | version(1)
    assert (
        client.patch(
            f"/api/v1/users/{people['op']['id']}", headers=headers, json={"status": "DISABLED"}
        ).status_code
        == 200
    )
    client.cookies.set("ea_session", old_cookie, domain="testserver.local", path="/")
    assert client.get("/api/v1/auth/session").status_code == 401
    assert (
        client.post(
            "/api/v1/auth/login",
            json={"login_name": people["op"]["login_name"], "password": PASSWORD},
        ).status_code
        == 401
    )


def test_temporary_password_forces_change_and_revokes_sessions(system):
    client, _, people = system
    headers = sign_in(client, people["admin"])
    name = "new_" + uuid4().hex[:12]
    response = client.post(
        "/api/v1/users",
        headers=headers,
        json={"login_name": name, "password": PASSWORD, "role": "OPERATOR"},
    )
    assert response.status_code == 201
    user = response.json()["data"]
    assert user["must_change_password"]
    own = sign_in(client, {"login_name": name})
    assert client.get("/api/v1/stores").json()["error"]["code"] == "AUTH_PASSWORD_CHANGE_REQUIRED"
    new_password = "ChangedPassword-456!"
    response = client.post(
        "/api/v1/auth/change-password",
        headers=own,
        json={"old_password": PASSWORD, "new_password": new_password},
    )
    assert response.status_code == 204
    assert client.get("/api/v1/auth/session").status_code == 401
    sign_in(client, {"login_name": name}, new_password)
    assert client.get("/api/v1/stores").status_code == 200


def test_password_reset_invalidates_old_session(system):
    client, _, people = system
    sign_in(client, people["op"])
    old_cookie = client.cookies.get("ea_session")
    headers = sign_in(client, people["admin"]) | version(1)
    new_password = "ResetPassword-789!"
    assert (
        client.post(
            f"/api/v1/users/{people['op']['id']}/password-reset",
            headers=headers,
            json={"new_password": new_password},
        ).status_code
        == 204
    )
    client.cookies.set("ea_session", old_cookie, domain="testserver.local", path="/")
    assert client.get("/api/v1/auth/session").status_code == 401
    sign_in(client, people["op"], new_password)
    assert client.get("/api/v1/auth/session").json()["data"]["user"]["must_change_password"]


def test_last_admin_and_optimistic_version(system):
    client, _, people = system
    headers = sign_in(client, people["admin"])
    admin_id = people["admin"]["id"]
    assert (
        client.patch(
            f"/api/v1/users/{admin_id}", headers=headers | version(1), json={"status": "DISABLED"}
        ).json()["error"]["code"]
        == "AUTH_LAST_ADMIN"
    )
    op_id = people["op"]["id"]
    assert (
        client.patch(
            f"/api/v1/users/{op_id}", headers=headers, json={"role": "SUPERVISOR"}
        ).status_code
        == 428
    )
    assert (
        client.patch(
            f"/api/v1/users/{op_id}", headers=headers | version(1), json={"role": "SUPERVISOR"}
        ).status_code
        == 200
    )
    assert (
        client.patch(
            f"/api/v1/users/{op_id}", headers=headers | version(1), json={"role": "OPERATOR"}
        ).status_code
        == 409
    )


@pytest.mark.parametrize("expiry", ["absolute", "idle"])
def test_session_expiry(system, expiry):
    client, db, people = system
    sign_in(client, people["op"])
    session = db.scalar(select(AuthSession).where(AuthSession.user_id == people["op"]["id"]))
    if expiry == "absolute":
        session.expires_at = utcnow() - timedelta(seconds=1)
    else:
        session.last_seen_at = utcnow() - timedelta(minutes=31)
    db.commit()
    assert client.get("/api/v1/auth/session").status_code == 401


def test_store_archive_duplicate_and_audit_redaction(system):
    client, db, people = system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    duplicate = client.post(
        "/api/v1/stores",
        headers=headers,
        json={"code": "TEST-A", "name": "重复编码", "platform": "test"},
    )
    assert duplicate.status_code == 409
    assert (
        client.post(
            f"/api/v1/stores/{store['id']}/archive", headers=headers | version(1)
        ).status_code
        == 200
    )
    response = client.patch(
        f"/api/v1/stores/{store['id']}",
        headers=headers | version(2),
        json={"code": "TEST-A", "name": "X", "platform": "test"},
    )
    assert response.status_code == 409
    audits = client.get("/api/v1/audit-events").json()["data"]
    assert audits["total"] >= 3
    assert PASSWORD not in str(audits)
    assert "password_hash" not in str(audits)
    assert (
        db.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.enterprise_id == db.get(User, people["admin"]["id"]).enterprise_id)
        )
        >= 3
    )


@pytest.mark.parametrize("secret", ["short-7", "abcdefghijklmnop", "x" * 65])
def test_password_validation_does_not_echo_secrets(system, secret):
    client, _, people = system
    headers = sign_in(client, people["admin"])
    response = client.post(
        "/api/v1/users",
        headers=headers,
        json={"login_name": "invalid", "password": secret, "role": "OPERATOR"},
    )
    assert response.status_code == 422
    assert secret not in response.text
    assert "request_id" in response.json()
