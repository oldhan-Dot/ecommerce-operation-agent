"""店铺日经营集成测试脚本。

用途：验证上海业务日、零与缺失、金额和整数精度、重复键确认、版本与权限、事务审计。
运行：backend 中设置 $env:RUN_MYSQL_TESTS='1'，执行 uv run pytest -q。
前置：开发 MySQL 已在 backend/.env 配置且执行 uv run alembic upgrade head。
隔离：复用身份测试的外层事务与请求 SAVEPOINT；测试数据和审计最终回滚。
"""

import os
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from test_identity import new_store, sign_in, version
from test_identity import system as system

from app import daily_data
from app.daily_data import business_today
from app.models import AuditEvent, Enterprise, Store, StoreDaily, StoreGrant

pytestmark = pytest.mark.skipif(os.getenv("RUN_MYSQL_TESTS") != "1", reason="显式启用真实 MySQL")
URL = "/api/v1/daily-data/store"


def test_shanghai_day_uses_business_timezone(monkeypatch):
    class FixedClock:
        @staticmethod
        def now(tz):
            return datetime(2026, 10, 16, 16, 30, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(daily_data, "datetime", FixedClock)
    assert business_today() == date(2026, 10, 17)


def payload(store):
    return {
        "store_id": store["id"],
        "business_date": business_today().isoformat(),
        "paid_amount": "1000.00",
        "paid_orders": "10",
        "visitors": "200",
        "refund_amount": "1500.00",
    }


def create(client, headers, body):
    response = client.post(URL, headers=headers, json=body)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_confirmed_update_and_stale_version_with_audit(system):
    client, db, people = system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    body = payload(store)
    item = create(client, headers, body)
    assert item["is_current_day"] is True
    assert item["refund_amount"] == "1500.00"  # 允许退款大于当天支付。
    assert client.post(URL, headers=headers, json=body).status_code == 409
    assert client.patch(URL, headers=headers, json={**body, "overwrite": True}).status_code == 428
    assert client.patch(URL, headers={**headers, **version(1)}, json=body).status_code == 409
    changed = {**body, "paid_amount": "0", "paid_orders": "0", "overwrite": True}
    response = client.patch(URL, headers={**headers, **version(1)}, json=changed)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["version"] == 2
    assert response.json()["data"]["paid_amount"] == "0.00"
    assert client.patch(URL, headers={**headers, **version(1)}, json=changed).status_code == 409
    assert client.get(URL).json()["data"]["total"] == 1
    assert client.get(f"{URL}/{item['id']}").json()["data"]["paid_orders"] == "0"
    events = db.scalars(
        select(AuditEvent)
        .where(AuditEvent.target_type == "store_daily", AuditEvent.store_id == int(store["id"]))
        .order_by(AuditEvent.id)
    ).all()
    assert [e.action for e in events] == ["STORE_DAILY_CREATED", "STORE_DAILY_UPDATED"]
    assert events[1].redacted_diff["before"]["paid_amount"] == "1000.00"
    assert events[1].redacted_diff["after"]["paid_amount"] == "0.00"


@pytest.mark.parametrize(
    "patch",
    [
        {"paid_amount": ""},
        {"paid_amount": None},
        {"paid_amount": "-0.01"},
        {"refund_amount": "1.001"},
        {"paid_orders": -1},
        {"paid_orders": 1.5},
        {"paid_orders": True},
        {"visitors": "1e3"},
        {"visitors": str(2**63)},
        {"business_date": "2099-01-01"},
        {"business_date": "2026-02-30"},
        {"business_date": "2026-01-01T00:00:00"},
    ],
)
def test_invalid_facts_rejected_without_audit(system, patch):
    client, db, people = system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    count = (
        select(func.count()).select_from(AuditEvent).where(AuditEvent.store_id == int(store["id"]))
    )
    before = db.scalar(count)
    assert client.post(URL, headers=headers, json={**payload(store), **patch}).status_code == 422
    assert client.get(URL).json()["data"]["total"] == 0
    assert db.scalar(count) == before


def test_large_integer_money_and_missing_dates(system):
    client, _, people = system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    day = (business_today() - timedelta(days=2)).isoformat()
    item = create(
        client,
        headers,
        {
            **payload(store),
            "business_date": day,
            "paid_orders": "9007199254740993",
            "visitors": str(2**63 - 1),
            "paid_amount": "9999999999999999.99",
            "refund_amount": "0",
        },
    )
    loaded = client.get(f"{URL}/{item['id']}").json()["data"]
    assert loaded["paid_orders"] == "9007199254740993"
    assert loaded["visitors"] == str(2**63 - 1)
    assert loaded["paid_amount"] == "9999999999999999.99"
    assert loaded["is_current_day"] is False
    params = {"store_id": store["id"], "start_date": day, "end_date": business_today().isoformat()}
    assert client.get(URL, params=params).json()["data"]["total"] == 1  # 不生成缺失日零记录。
    assert (
        client.get(
            URL, params={**params, "start_date": business_today().isoformat(), "end_date": day}
        ).status_code
        == 422
    )


def test_store_scope_grants_archive_and_enterprise(system):
    client, db, people = system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    hidden = new_store(client, headers, code="HIDDEN")
    hidden_item = create(client, headers, payload(hidden))
    enterprise = Enterprise(name="其他企业")
    db.add(enterprise)
    db.flush()
    other = Store(enterprise_id=enterprise.id, code="OTHER", name="外部店铺", platform="手工")
    db.add(other)
    db.flush()
    foreign = StoreDaily(
        store_id=other.id,
        business_date=business_today(),
        paid_amount=0,
        paid_orders=0,
        visitors=0,
        refund_amount=0,
    )
    db.add(foreign)
    db.add(
        StoreGrant(
            user_id=people["op"]["id"], store_id=int(store["id"]), granted_by=people["admin"]["id"]
        )
    )
    db.commit()
    assert client.get(f"{URL}/{foreign.id}").status_code == 404
    assert client.get(URL).json()["data"]["total"] == 1
    headers = sign_in(client, people["op"])
    assert client.get(f"{URL}/{hidden_item['id']}").status_code == 404
    assert client.get(URL).json()["data"]["total"] == 0
    assert client.post(URL, headers=headers, json=payload(hidden)).status_code == 404
    assert (
        client.patch(
            URL, headers={**headers, **version(1)}, json={**payload(hidden), "overwrite": True}
        ).status_code
        == 404
    )
    own = create(client, headers, payload(store))
    grant = db.scalar(select(StoreGrant).where(StoreGrant.user_id == people["op"]["id"]))
    db.delete(grant)
    db.commit()
    assert client.get(f"{URL}/{own['id']}").status_code == 404
    headers = sign_in(client, people["admin"])
    assert (
        client.post(
            f"/api/v1/stores/{store['id']}/archive", headers={**headers, **version(1)}
        ).status_code
        == 200
    )
    assert client.get(f"{URL}/{own['id']}").status_code == 200
    assert (
        client.patch(
            URL, headers={**headers, **version(1)}, json={**payload(store), "overwrite": True}
        ).status_code
        == 409
    )


def test_database_rejects_negative_metrics(system):
    client, db, people = system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    # 绕开 API 直接写模型，确认数据库 CHECK 也能拦截负值；嵌套事务独立回滚。
    # PyMySQL 将 MySQL CHECK 违规归为 OperationalError，必须进一步核对错误码。
    with pytest.raises(OperationalError) as failure, db.begin_nested():
        db.add(
            StoreDaily(
                store_id=int(store["id"]),
                business_date=business_today(),
                paid_amount=-1,
                paid_orders=0,
                visitors=0,
                refund_amount=0,
            )
        )
        db.flush()
    assert failure.value.orig.args[0] == 3819
