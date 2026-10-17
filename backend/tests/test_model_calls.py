"""M2 持久调用与额度验证脚本。

用途：验证管理员权限、请求幂等、额度、执行落账、跨日和超时恢复；模型响应全部受控。
运行：backend 下设置 RUN_MYSQL_TESTS=1 后 uv run pytest tests/test_model_calls.py -q。
前置：独立开发/测试 MySQL 已迁移至 head；沿用外层事务回滚，不访问 DeepSeek、不读取真实密钥。
离线自然日测试无需数据库。关键事务用真实 MySQL，不用 SQLite 模拟。
"""

import os
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from test_identity import sign_in
from test_identity import system as system
from test_model_settings import body, configure_app

from app import model_calls
from app.core.errors import AppError
from app.model_provider import ProbeResult
from app.models import DailyCallUsage, Enterprise, ModelCall, ModelConfig, User, utcnow

mysql = pytest.mark.skipif(os.getenv("RUN_MYSQL_TESTS") != "1", reason="需独立MySQL测试库")


def test_shanghai_natural_day():
    assert str(model_calls.shanghai_date(datetime(2026, 10, 17, 15, 59))) == "2026-10-17"
    assert str(model_calls.shanghai_date(datetime(2026, 10, 17, 16))) == "2026-10-18"


@pytest.fixture
def configured(system):
    client, db, people = system
    configure_app(client)
    headers = sign_in(client, people["admin"])
    saved = client.put(
        "/api/v1/settings/model", headers={**headers, "If-Match": '"0"'}, json=body()
    )
    assert saved.status_code == 200, saved.text
    return client, db, people, headers


def submit(client, headers, key=None, version=1):
    return client.post(
        "/api/v1/settings/model/test",
        json={"config_version": version},
        headers={**headers, "Idempotency-Key": key or uuid4().hex},
    )


def usage(client):
    return client.get("/api/v1/settings/model/calls").json()["data"]


@mysql
def test_idempotent_reserve_and_success(configured, monkeypatch):
    client, db, _, headers = configured
    key = uuid4().hex
    first, repeated = submit(client, headers, key), submit(client, headers, key)
    assert first.status_code == 202 and repeated.status_code == 200
    assert first.json()["data"]["call_id"] == repeated.json()["data"]["call_id"]
    assert usage(client)["reserved"] == 1 and usage(client)["consumed"] == 0
    invoked = []

    async def fake_probe(config, secret, hosts):
        # 网络期间短事务已提交，Worker 不占用数据库事务/行锁。
        assert not db.in_transaction()
        invoked.append(config.id)
        return ProbeResult(123, {"total_tokens": 8})

    monkeypatch.setattr(model_calls, "probe", fake_probe)
    assert model_calls.process_model_call(db, client.app.state.settings)
    assert not model_calls.process_model_call(db, client.app.state.settings)
    state = usage(client)
    assert state["consumed"] == 1 and state["reserved"] == 0
    assert state["items"][0]["status"] == "SUCCEEDED"
    assert len(invoked) == 1
    assert submit(client, headers, key).json()["data"]["status"] == "SUCCEEDED"
    assert client.get("/api/v1/settings/model").json()["data"]["connection_status"] == "SUCCEEDED"


@mysql
def test_key_version_conflict_and_invalid_key(configured):
    client, _, _, headers = configured
    key = uuid4().hex
    assert submit(client, headers, key).status_code == 202
    assert submit(client, headers, key, 2).status_code == 409
    assert submit(client, headers, "bad").status_code == 422
    assert submit(client, headers, version=2).status_code == 409


@mysql
def test_quota_and_config_switch_do_not_reset_usage(configured, monkeypatch):
    client, db, _, headers = configured
    assert submit(client, headers).status_code == 202

    async def fake(*args):
        return ProbeResult(1, {})

    monkeypatch.setattr(model_calls, "probe", fake)
    model_calls.process_model_call(db, client.app.state.settings)
    saved = client.put(
        "/api/v1/settings/model", headers={**headers, "If-Match": '"1"'}, json=body(daily_limit=1)
    )
    assert saved.status_code == 200
    response = submit(client, headers, version=2)
    assert response.status_code == 429 and response.json()["error"]["code"] == "AI_DAILY_LIMIT"
    assert usage(client)["consumed"] == 1


@mysql
def test_actor_concurrency_limit(configured):
    client, _, _, headers = configured
    assert submit(client, headers).status_code == 202
    assert submit(client, headers).status_code == 202
    response = submit(client, headers)
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "AI_CONCURRENCY_LIMIT"
    assert usage(client)["reserved"] == 2


@mysql
def test_enterprise_five_slot_limit(configured):
    client, db, people, headers = configured
    admin = db.get(User, people["admin"]["id"])
    enterprise = db.get(Enterprise, admin.enterprise_id)
    db.refresh(enterprise)
    day = model_calls.shanghai_date(utcnow())
    for _ in range(5):
        actor = User(
            enterprise_id=enterprise.id,
            login_name=uuid4().hex,
            role="ADMIN",
            password_hash=admin.password_hash,
            must_change_password=False,
        )
        db.add(actor)
        db.flush()
        db.add(
            ModelCall(
                enterprise_id=enterprise.id,
                actor_id=actor.id,
                config_id=enterprise.active_model_config_id,
                usage_date=day,
                request_key=uuid4().hex,
                deadline_at=utcnow() + timedelta(minutes=5),
            )
        )
    db.add(DailyCallUsage(enterprise_id=enterprise.id, usage_date=day, reserved=5, consumed=0))
    db.commit()
    response = submit(client, headers)
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "AI_CONCURRENCY_LIMIT"
    assert usage(client)["reserved"] == 5


@mysql
def test_queued_uses_frozen_model_and_current_limit(configured, monkeypatch):
    client, db, _, headers = configured
    submit(client, headers)
    submit(client, headers)
    assert (
        client.put(
            "/api/v1/settings/model",
            headers={**headers, "If-Match": '"1"'},
            json=body(model_name="new-model", daily_limit=1),
        ).status_code
        == 200
    )
    invoked = []

    async def fake(config, *args):
        invoked.append(config.model_name)
        return ProbeResult(1, {})

    monkeypatch.setattr(model_calls, "probe", fake)
    assert model_calls.process_model_call(db, client.app.state.settings)
    assert model_calls.process_model_call(db, client.app.state.settings)
    assert invoked == ["deepseek-flash"]
    state = usage(client)
    assert state["consumed"] == 1 and state["reserved"] == 0
    assert sorted(c["status"] for c in state["items"]) == ["RELEASED", "SUCCEEDED"]


@mysql
@pytest.mark.parametrize(
    "code,status",
    [
        ("MODEL_PROVIDER_ERROR", "FAILED"),
        ("MODEL_TIMEOUT", "UNKNOWN"),
        ("MODEL_RESULT_UNKNOWN", "UNKNOWN"),
    ],
)
def test_failed_sent_call_not_refunded_or_replayed(configured, monkeypatch, code, status):
    client, db, _, headers = configured
    key = uuid4().hex
    submit(client, headers, key)

    async def fail(*args):
        raise AppError(502, code, "controlled")

    monkeypatch.setattr(model_calls, "probe", fail)
    assert model_calls.process_model_call(db, client.app.state.settings)
    assert not model_calls.process_model_call(db, client.app.state.settings)
    result = usage(client)
    assert result["consumed"] == 1 and result["reserved"] == 0
    assert result["items"][0]["status"] == status
    assert submit(client, headers, key).json()["data"]["status"] == status


@mysql
@pytest.mark.parametrize("sent", [False, True])
def test_deadline_recovery(configured, monkeypatch, sent):
    client, db, _, headers = configured
    key = uuid4().hex
    response = submit(client, headers, key)
    call = db.get(ModelCall, int(response.json()["data"]["call_id"]))
    if sent:
        call.status = "STARTED"
        counter = db.scalar(
            select(DailyCallUsage).where(DailyCallUsage.enterprise_id == call.enterprise_id)
        )
        counter.reserved, counter.consumed = 0, 1
    call.deadline_at = utcnow() - timedelta(seconds=1)
    db.commit()

    async def forbidden(*args):
        pytest.fail("已过期或不确定的调用不应发送")

    monkeypatch.setattr(model_calls, "probe", forbidden)
    assert model_calls.process_model_call(db, client.app.state.settings)
    result = usage(client)
    assert result["reserved"] == 0 and result["consumed"] == int(sent)
    assert result["items"][0]["status"] == ("UNKNOWN" if sent else "RELEASED")


@mysql
def test_reserved_rollover_uses_actual_shanghai_day(configured, monkeypatch):
    client, db, _, headers = configured
    now = utcnow()
    monkeypatch.setattr(model_calls, "utcnow", lambda: now - timedelta(days=1))
    response = submit(client, headers)
    call = db.get(ModelCall, int(response.json()["data"]["call_id"]))
    call.deadline_at = now + timedelta(minutes=1)
    db.commit()
    monkeypatch.setattr(model_calls, "utcnow", lambda: now)

    async def fake(*args):
        return ProbeResult(1, {})

    monkeypatch.setattr(model_calls, "probe", fake)
    model_calls.process_model_call(db, client.app.state.settings)
    counters = db.scalars(
        select(DailyCallUsage)
        .where(DailyCallUsage.enterprise_id == call.enterprise_id)
        .order_by(DailyCallUsage.usage_date)
    ).all()
    assert [(c.consumed, c.reserved) for c in counters] == [(0, 0), (1, 0)]


@mysql
@pytest.mark.parametrize("change", ["role", "config", "key"])
def test_revoked_or_unreadable_before_send_releases(configured, monkeypatch, change):
    client, db, people, headers = configured
    response = submit(client, headers)
    call = db.get(ModelCall, int(response.json()["data"]["call_id"]))
    if change == "role":
        db.get(User, people["admin"]["id"]).role = "SUPERVISOR"
    elif change == "config":
        db.get(ModelConfig, call.config_id).revoked_at = utcnow()
    else:
        db.get(ModelConfig, call.config_id).secret_cipher = b"invalid"
    db.commit()

    async def forbidden(*args):
        pytest.fail("权限或配置失效后不能发送")

    monkeypatch.setattr(model_calls, "probe", forbidden)
    model_calls.process_model_call(db, client.app.state.settings)
    db.refresh(call)
    assert call.status == "RELEASED"
    counter = db.scalar(
        select(DailyCallUsage).where(DailyCallUsage.enterprise_id == call.enterprise_id)
    )
    assert counter.reserved == counter.consumed == 0


@mysql
def test_permissions_csrf_and_cross_enterprise(configured):
    client, db, people, headers = configured
    response = submit(client, headers)
    call_id = response.json()["data"]["call_id"]
    assert (
        client.post(
            "/api/v1/settings/model/test",
            json={"config_version": 1},
            headers={"Idempotency-Key": uuid4().hex},
        ).status_code
        == 403
    )
    other = Enterprise(name="隔离企业")
    db.add(other)
    db.flush()
    person = db.get(User, people["admin"]["id"])
    person.enterprise_id = other.id
    db.commit()
    assert client.get(f"/api/v1/settings/model/calls/{call_id}").status_code == 404
    assert usage(client)["items"] == []
    assert db.scalar(select(func.count()).select_from(ModelCall)) >= 1
    sign_in(client, people["op"])
    assert client.get("/api/v1/settings/model/calls").status_code == 403
