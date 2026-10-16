"""投放日经营集成测试脚本。

用途：验证渠道业务键、归因未知/零值与口径、异常点击提示、覆盖版本、权限及数据库约束。
运行：backend 设置 $env:RUN_MYSQL_TESTS='1' 后执行 uv run pytest -q。
前置：backend/.env 为开发 MySQL，已运行 uv run alembic upgrade head。
隔离：复用身份测试外层事务及请求 SAVEPOINT，测试记录与审计最终回滚，不执行广告投放。
"""

import os

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from test_identity import new_store, sign_in, version
from test_identity import system as system

from app.daily_data import business_today
from app.models import AdDaily, AuditEvent, Enterprise, Store, StoreGrant

pytestmark = pytest.mark.skipif(os.getenv("RUN_MYSQL_TESTS") != "1", reason="显式启用真实 MySQL")
URL = "/api/v1/daily-data/ad"


def body(store, channel="渠道A"):
    return {
        "store_id": store["id"],
        "channel": channel,
        "business_date": business_today().isoformat(),
        "spend": "100.00",
        "impressions": "1000",
        "clicks": "20",
        "attributed_amount": None,
        "attribution_note": None,
    }


def create(client, headers, payload):
    response = client.post(URL, headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_channel_key_overwrite_and_audit(system):
    client, db, people = system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    payload = body(store)
    item = create(client, headers, payload)
    create(client, headers, body(store, "渠道B"))
    assert item["attributed_amount"] is None and item["is_current_day"] is True
    assert client.post(URL, headers=headers, json=body(store, " 渠道A ")).status_code == 409
    assert (
        client.patch(URL, headers=headers, json={**payload, "overwrite": True}).status_code == 428
    )
    assert client.patch(URL, headers={**headers, **version(1)}, json=payload).status_code == 409
    changed = {**payload, "spend": "0", "impressions": "9007199254740993", "overwrite": True}
    response = client.patch(URL, headers={**headers, **version(1)}, json=changed)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["version"] == 2
    assert response.json()["data"]["spend"] == "0.00"
    assert response.json()["data"]["impressions"] == "9007199254740993"
    assert client.patch(URL, headers={**headers, **version(1)}, json=changed).status_code == 409
    params = {
        "store_id": store["id"],
        "channel": "渠道A",
        "start_date": payload["business_date"],
        "end_date": payload["business_date"],
    }
    assert client.get(URL, params=params).json()["data"]["total"] == 1
    assert client.get(URL).json()["data"]["total"] == 2
    assert client.get("/api/v1/daily-data/store").json()["data"]["total"] == 0
    assert client.get("/api/v1/daily-data/product").json()["data"]["total"] == 0
    events = db.scalars(
        select(AuditEvent)
        .where(AuditEvent.target_type == "ad_daily", AuditEvent.target_id == item["id"])
        .order_by(AuditEvent.id)
    ).all()
    assert [e.action for e in events] == ["AD_DAILY_CREATED", "AD_DAILY_UPDATED"]
    assert events[1].redacted_diff["before"]["spend"] == "100.00"
    assert events[1].redacted_diff["after"]["spend"] == "0.00"


def test_unknown_zero_and_basis_transitions(system):
    client, _, people = system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    payload = body(store)
    item = create(client, headers, payload)
    assert client.get(f"{URL}/{item['id']}").json()["data"]["attributed_amount"] is None
    known = {
        **payload,
        "attributed_amount": "0",
        "attribution_note": " 点击后7日支付成交 ",
        "overwrite": True,
    }
    response = client.patch(URL, headers={**headers, **version(1)}, json=known)
    assert response.status_code == 200
    assert response.json()["data"]["attributed_amount"] == "0.00"
    assert response.json()["data"]["attribution_note"] == "点击后7日支付成交"
    response = client.patch(
        URL, headers={**headers, **version(2)}, json={**payload, "overwrite": True}
    )
    assert response.status_code == 200
    assert response.json()["data"]["attributed_amount"] is None
    assert response.json()["data"]["attribution_note"] is None


def test_click_anomaly_is_preserved_as_warning(system):
    client, _, people = system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    item = create(client, headers, {**body(store), "impressions": "0", "clicks": "5"})
    assert item["warnings"]
    assert client.get(f"{URL}/{item['id']}").json()["data"]["warnings"]


@pytest.mark.parametrize(
    "patch",
    [
        {"channel": "  "},
        {"business_date": "2099-01-01"},
        {"spend": ""},
        {"spend": "0.001"},
        {"impressions": None},
        {"clicks": 1.5},
        {"attributed_amount": "-1"},
        {"attributed_amount": "0"},
        {"attributed_amount": "100", "attribution_note": " \t "},
    ],
)
def test_invalid_input_is_atomic(system, patch):
    client, db, people = system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    count = (
        select(func.count()).select_from(AuditEvent).where(AuditEvent.store_id == int(store["id"]))
    )
    before = db.scalar(count)
    assert client.post(URL, headers=headers, json={**body(store), **patch}).status_code == 422
    assert client.get(URL).json()["data"]["total"] == 0
    assert db.scalar(count) == before


def test_scope_revocation_archive_and_other_enterprise(system):
    client, db, people = system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    item = create(client, headers, body(store))
    headers = sign_in(client, people["op"])
    assert client.get(f"{URL}/{item['id']}").status_code == 404
    assert client.get(URL).json()["data"]["total"] == 0
    assert client.post(URL, headers=headers, json=body(store)).status_code == 404
    grant = StoreGrant(
        user_id=people["op"]["id"], store_id=int(store["id"]), granted_by=people["admin"]["id"]
    )
    db.add(grant)
    db.commit()
    assert (
        client.patch(
            URL, headers={**headers, **version(1)}, json={**body(store), "overwrite": True}
        ).status_code
        == 200
    )
    db.delete(grant)
    db.commit()
    assert (
        client.patch(
            URL, headers={**headers, **version(2)}, json={**body(store), "overwrite": True}
        ).status_code
        == 404
    )
    enterprise = Enterprise(name="其他企业")
    db.add(enterprise)
    db.flush()
    other = Store(enterprise_id=enterprise.id, code="OTHER", name="外部店铺", platform="手工")
    db.add(other)
    db.flush()
    foreign = AdDaily(
        store_id=other.id,
        channel="外部渠道",
        business_date=business_today(),
        spend=0,
        impressions=0,
        clicks=0,
    )
    db.add(foreign)
    db.commit()
    headers = sign_in(client, people["admin"])
    assert client.get(f"{URL}/{foreign.id}").status_code == 404
    assert client.get(URL).json()["data"]["total"] == 1
    assert (
        client.post(
            f"/api/v1/stores/{store['id']}/archive", headers={**headers, **version(1)}
        ).status_code
        == 200
    )
    assert client.get(f"{URL}/{item['id']}").status_code == 200
    assert client.post(URL, headers=headers, json=body(store, "新渠道")).status_code == 409
    assert (
        client.patch(
            URL, headers={**headers, **version(2)}, json={**body(store), "overwrite": True}
        ).status_code
        == 409
    )


@pytest.mark.parametrize(
    "patch", [{"spend": -1}, {"attributed_amount": 0, "attribution_note": " "}]
)
def test_database_constraints(system, patch):
    client, db, people = system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    values = {
        "store_id": int(store["id"]),
        "channel": "渠道",
        "business_date": business_today(),
        "spend": 0,
        "impressions": 0,
        "clicks": 0,
        **patch,
    }
    # 绕过 API 验证数据库 CHECK；PyMySQL 对此使用 OperationalError(3819)。
    with pytest.raises(OperationalError) as failure, db.begin_nested():
        db.add(AdDaily(**values))
        db.flush()
    assert failure.value.orig.args[0] == 3819
