"""商品管理集成测试脚本。

用途：验证商品/SKU 原子保存、金额、店铺授权、归档、版本冲突与审计。
运行：backend 下设置 $env:RUN_MYSQL_TESTS='1'，执行 uv run pytest -q。
前置：开发 MySQL 已配置并执行 uv run alembic upgrade head。
隔离：复用身份测试的外层事务/SAVEPOINT fixture，全部测试数据最终回滚。
"""

import os

import pytest
from sqlalchemy import func, select
from test_identity import new_store, sign_in, version
from test_identity import system as system

from app.models import AuditEvent, Product, Sku, StoreGrant

pytestmark = pytest.mark.skipif(os.getenv("RUN_MYSQL_TESTS") != "1", reason="显式启用真实 MySQL")


def payload(store, code="P-01", sku_code="SKU-01"):
    return {
        "store_id": store["id"],
        "code": code,
        "name": "测试商品",
        "category": "食品",
        "selling_points": "独立包装",
        "skus": [{"code": sku_code, "spec_name": "默认规格", "price": "19.90", "cost": None}],
    }


def create(client, headers, body):
    response = client.post("/api/v1/products", headers=headers, json=body)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def editable(view):
    return {
        k: view[k]
        for k in (
            "store_id",
            "code",
            "name",
            "category",
            "selling_points",
            "brand",
            "audience",
            "source_url",
            "skus",
        )
    }


def test_product_lifecycle_versions_and_audit(system):
    client, db, people = system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    item = create(client, headers, payload(store))
    assert item["skus"][0]["price"] == "19.90"
    assert item["skus"][0]["cost"] is None
    url = f"/api/v1/products/{item['id']}"
    body = editable(item)
    body["name"] = "已修改商品"
    body["skus"][0]["price"] = "20.01"
    body["skus"].append({"code": "SKU-02", "spec_name": "大包装", "price": "30", "cost": "0"})
    assert client.patch(url, headers=headers, json=body).status_code == 428
    changed = client.patch(url, headers={**headers, **version(1)}, json=body)
    assert changed.status_code == 200, changed.text
    updated = changed.json()["data"]
    assert updated["version"] == 2 and len(updated["skus"]) == 2
    assert updated["skus"][0]["id"] == item["skus"][0]["id"]
    assert updated["skus"][0]["price"] == "20.01"
    assert updated["skus"][1]["cost"] == "0.00"
    assert client.patch(url, headers={**headers, **version(1)}, json=body).status_code == 409
    response = client.post(f"{url}/archive", headers={**headers, **version(2)})
    assert response.status_code == 200
    assert client.get(url).json()["data"]["status"] == "ARCHIVED"
    assert client.patch(url, headers={**headers, **version(3)}, json=body).status_code == 409
    assert client.get("/api/v1/products?status=ARCHIVED").json()["data"]["total"] == 1
    events = db.scalars(
        select(AuditEvent)
        .where(AuditEvent.target_type == "products", AuditEvent.actor_id == people["admin"]["id"])
        .order_by(AuditEvent.id)
    ).all()
    assert [e.action for e in events] == ["PRODUCT_CREATED", "PRODUCT_UPDATED", "PRODUCT_ARCHIVED"]
    assert all(str(e.store_id) == store["id"] for e in events)
    assert events[1].redacted_diff["before"]["skus"][0]["price"] == "19.90"


@pytest.mark.parametrize("change", ["negative", "precision", "empty", "duplicate", "blank", "url"])
def test_invalid_product_rejected(system, change):
    client, _, people = system
    headers = sign_in(client, people["admin"])
    body = payload(new_store(client, headers))
    if change == "negative":
        body["skus"][0]["price"] = "-0.01"
    if change == "precision":
        body["skus"][0]["price"] = "0.001"
    if change == "empty":
        body["skus"] = []
    if change == "duplicate":
        body["skus"] *= 2
    if change == "blank":
        body["selling_points"] = "  "
    if change == "url":
        body["source_url"] = "javascript:alert(1)"
    assert client.post("/api/v1/products", headers=headers, json=body).status_code == 422
    assert client.get("/api/v1/products").json()["data"]["total"] == 0


def test_duplicate_sku_rolls_back_product_and_audit(system):
    client, db, people = system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    create(client, headers, payload(store))
    audit_count = (
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.actor_id == people["admin"]["id"])
    )
    before = db.scalar(audit_count)
    bad = payload(store, code="P-02")
    assert client.post("/api/v1/products", headers=headers, json=bad).status_code == 409
    assert (
        db.scalar(
            select(func.count()).select_from(Product).where(Product.store_id == int(store["id"]))
        )
        == 1
    )
    assert (
        db.scalar(select(func.count()).select_from(Sku).where(Sku.store_id == int(store["id"])))
        == 1
    )
    assert db.scalar(audit_count) == before
    # 不同店铺允许相同商品与 SKU 编码。
    other = new_store(client, headers, code="TEST-B")
    create(client, headers, payload(other))


def test_operator_scope_and_live_grants(system):
    client, db, people = system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    hidden = new_store(client, headers, code="HIDDEN")
    item = create(client, headers, payload(hidden))
    db.add(
        StoreGrant(
            user_id=people["op"]["id"], store_id=int(store["id"]), granted_by=people["admin"]["id"]
        )
    )
    db.commit()
    headers = sign_in(client, people["op"])
    assert client.get("/api/v1/products").json()["data"]["total"] == 0
    assert client.get(f"/api/v1/products/{item['id']}").status_code == 404
    assert client.post("/api/v1/products", headers=headers, json=payload(hidden)).status_code == 404
    assert (
        client.patch(
            f"/api/v1/products/{item['id']}", headers={**headers, **version(1)}, json=editable(item)
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/v1/products/{item['id']}/archive", headers={**headers, **version(1)}
        ).status_code
        == 404
    )
    owned = create(client, headers, payload(store))
    body = editable(owned)
    body["name"] = "运营人员修改"
    assert (
        client.patch(
            f"/api/v1/products/{owned['id']}", headers={**headers, **version(1)}, json=body
        ).status_code
        == 200
    )
    grant = db.scalar(select(StoreGrant).where(StoreGrant.user_id == people["op"]["id"]))
    db.delete(grant)
    db.commit()
    assert client.get(f"/api/v1/products/{owned['id']}").status_code == 404


def test_sku_identity_and_product_identity_preserved(system):
    client, _, people = system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    item = create(client, headers, payload(store))
    other = create(client, headers, payload(store, "P-02", "SKU-02"))
    url = f"/api/v1/products/{item['id']}"
    headers.update(version(1))
    body = editable(item)
    body["skus"] = other["skus"]
    assert client.patch(url, headers=headers, json=body).status_code == 422
    body = editable(item)
    body["skus"][0]["code"] = "REPLACED"
    assert client.patch(url, headers=headers, json=body).status_code == 409
    body = editable(client.get(url).json()["data"])
    body["code"] = "REPLACED"
    assert client.patch(url, headers=headers, json=body).status_code == 409


def test_archived_store_blocks_product_writes(system):
    client, _, people = system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    item = create(client, headers, payload(store))
    assert (
        client.post(
            f"/api/v1/stores/{store['id']}/archive", headers={**headers, **version(1)}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/products", headers=headers, json=payload(store, "P-02", "SKU-02")
        ).status_code
        == 409
    )
    url = f"/api/v1/products/{item['id']}"
    assert client.get(url).status_code == 200
    assert (
        client.patch(url, headers={**headers, **version(1)}, json=editable(item)).status_code == 409
    )
