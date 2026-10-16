"""商品日经营集成测试脚本。

用途：验证商品同店归属、独立业务键、数据集隔离、覆盖确认、版本冲突、权限与归档。
运行：backend 下设置 $env:RUN_MYSQL_TESTS='1'，执行 uv run pytest -q。
前置：开发 MySQL 已配置并执行 uv run alembic upgrade head。
隔离：复用外层事务和请求 SAVEPOINT，测试商品、日数据和审计最终全部回滚。
"""

import os
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from test_catalog import create as create_product
from test_catalog import payload as product_payload
from test_daily_data import payload as daily_payload
from test_identity import new_store, sign_in, version
from test_identity import system as system

from app.daily_data import business_today
from app.models import AuditEvent, Enterprise, Product, ProductDaily, Store, StoreGrant

pytestmark = pytest.mark.skipif(os.getenv("RUN_MYSQL_TESTS") != "1", reason="显式启用真实 MySQL")
URL = "/api/v1/daily-data/product"


def body(store, product):
    return {**daily_payload(store), "product_id": product["id"]}


def create(client, headers, payload):
    response = client.post(URL, headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_independent_products_overwrite_and_audit(system):
    client, db, people = system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    product = create_product(client, headers, product_payload(store))
    other = create_product(client, headers, product_payload(store, "P-02", "SKU-02"))
    payload = body(store, product)
    item = create(client, headers, payload)
    create(client, headers, body(store, other))  # 同店同日不同商品各自保存。
    assert item["product_name"] == product["name"] and item["product_code"] == product["code"]
    assert client.post(URL, headers=headers, json=payload).status_code == 409
    assert (
        client.patch(URL, headers=headers, json={**payload, "overwrite": True}).status_code == 428
    )
    assert client.patch(URL, headers={**headers, **version(1)}, json=payload).status_code == 409
    changed = {**payload, "overwrite": True, "paid_amount": "0", "paid_orders": "9007199254740993"}
    response = client.patch(URL, headers={**headers, **version(1)}, json=changed)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["version"] == 2
    assert response.json()["data"]["paid_orders"] == "9007199254740993"
    assert response.json()["data"]["refund_amount"] == "1500.00"
    assert client.patch(URL, headers={**headers, **version(1)}, json=changed).status_code == 409
    assert client.get("/api/v1/daily-data/store").json()["data"]["total"] == 0
    query = {
        "store_id": store["id"],
        "product_id": product["id"],
        "start_date": payload["business_date"],
        "end_date": payload["business_date"],
    }
    assert client.get(URL, params=query).json()["data"]["total"] == 1
    assert client.get(URL, params={"page_size": 1, "page": 2}).json()["data"]["total"] == 2
    assert client.get(f"{URL}/{item['id']}").json()["data"]["paid_amount"] == "0.00"
    events = db.scalars(
        select(AuditEvent)
        .where(AuditEvent.target_type == "product_daily", AuditEvent.target_id == item["id"])
        .order_by(AuditEvent.id)
    ).all()
    assert [e.action for e in events] == ["PRODUCT_DAILY_CREATED", "PRODUCT_DAILY_UPDATED"]
    assert all(str(e.store_id) == store["id"] for e in events)
    assert events[1].redacted_diff["before"]["paid_amount"] == "1000.00"
    assert events[1].redacted_diff["after"]["paid_amount"] == "0.00"


@pytest.mark.parametrize(
    "patch",
    [
        {"paid_amount": "-1"},
        {"business_date": "2099-01-01"},
        {"paid_orders": True},
        {"product_id": None},
    ],
)
def test_product_facts_validation(system, patch):
    client, db, people = system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    product = create_product(client, headers, product_payload(store))
    count = (
        select(func.count()).select_from(AuditEvent).where(AuditEvent.store_id == int(store["id"]))
    )
    before = db.scalar(count)
    assert (
        client.post(URL, headers=headers, json={**body(store, product), **patch}).status_code == 422
    )
    assert client.get(URL).json()["data"]["total"] == 0
    assert db.scalar(count) == before


def test_same_store_foreign_key_and_missing_dates(system):
    client, db, people = system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    other = new_store(client, headers, "OTHER")
    product = create_product(client, headers, product_payload(store))
    assert client.post(URL, headers=headers, json=body(other, product)).status_code == 422
    # 绕过 API 验证组合外键仍拒绝跨店；检查具体外键违规，防止误将连接错误判为通过。
    with pytest.raises(IntegrityError) as failure, db.begin_nested():
        db.add(
            ProductDaily(
                store_id=int(other["id"]),
                product_id=int(product["id"]),
                business_date=business_today(),
                paid_amount=0,
                paid_orders=0,
                visitors=0,
                refund_amount=0,
            )
        )
        db.flush()
    assert failure.value.orig.args[0] == 1452
    day = (business_today() - timedelta(days=2)).isoformat()
    item = create(client, headers, {**body(store, product), "business_date": day})
    assert item["is_current_day"] is False
    params = {"start_date": day, "end_date": business_today().isoformat()}
    assert client.get(URL, params=params).json()["data"]["total"] == 1
    assert (
        client.get(
            URL, params={"start_date": business_today().isoformat(), "end_date": day}
        ).status_code
        == 422
    )


def test_archive_preserves_history_and_blocks_writes(system):
    client, _, people = system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    product = create_product(client, headers, product_payload(store))
    payload = body(store, product)
    item = create(client, headers, payload)
    assert (
        client.post(
            f"/api/v1/products/{product['id']}/archive", headers={**headers, **version(1)}
        ).status_code
        == 200
    )
    assert client.get(f"{URL}/{item['id']}").json()["data"]["product_status"] == "ARCHIVED"
    assert (
        client.patch(
            URL, headers={**headers, **version(1)}, json={**payload, "overwrite": True}
        ).status_code
        == 409
    )
    assert (
        client.post(
            URL,
            headers=headers,
            json={**payload, "business_date": (business_today() - timedelta(days=1)).isoformat()},
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/v1/stores/{store['id']}/archive", headers={**headers, **version(1)}
        ).status_code
        == 200
    )
    assert client.get(f"{URL}/{item['id']}").status_code == 200


def test_scope_revocation_and_other_enterprise(system):
    client, db, people = system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    product = create_product(client, headers, product_payload(store))
    item = create(client, headers, body(store, product))
    headers = sign_in(client, people["op"])
    assert client.get(f"{URL}/{item['id']}").status_code == 404
    assert client.get(URL).json()["data"]["total"] == 0
    assert client.post(URL, headers=headers, json=body(store, product)).status_code == 404
    grant = StoreGrant(
        user_id=people["op"]["id"], store_id=int(store["id"]), granted_by=people["admin"]["id"]
    )
    db.add(grant)
    db.commit()
    assert client.get(f"{URL}/{item['id']}").status_code == 200
    assert (
        client.patch(
            URL, headers={**headers, **version(1)}, json={**body(store, product), "overwrite": True}
        ).status_code
        == 200
    )
    db.delete(grant)
    db.commit()
    assert (
        client.patch(
            URL, headers={**headers, **version(2)}, json={**body(store, product), "overwrite": True}
        ).status_code
        == 404
    )
    enterprise = Enterprise(name="其他企业")
    db.add(enterprise)
    db.flush()
    foreign_store = Store(
        enterprise_id=enterprise.id, code="OTHER", name="外部店铺", platform="手工"
    )
    db.add(foreign_store)
    db.flush()
    foreign_product = Product(
        store_id=foreign_store.id,
        code="OTHER",
        name="外部商品",
        category="测试",
        selling_points="测试",
    )
    db.add(foreign_product)
    db.flush()
    foreign = ProductDaily(
        store_id=foreign_store.id,
        product_id=foreign_product.id,
        business_date=business_today(),
        paid_amount=0,
        paid_orders=0,
        visitors=0,
        refund_amount=0,
    )
    db.add(foreign)
    db.commit()
    sign_in(client, people["admin"])
    assert client.get(f"{URL}/{foreign.id}").status_code == 404
    assert client.get(URL).json()["data"]["total"] == 1


def test_database_negative_check(system):
    client, db, people = system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    product = create_product(client, headers, product_payload(store))
    with pytest.raises(OperationalError) as failure, db.begin_nested():
        db.add(
            ProductDaily(
                store_id=int(store["id"]),
                product_id=int(product["id"]),
                business_date=business_today(),
                paid_amount=0,
                paid_orders=-1,
                visitors=0,
                refund_amount=0,
            )
        )
        db.flush()
    assert failure.value.orig.args[0] == 3819
