"""竞品资料集成测试脚本。

用途：验证来源/日期校验、同店本品关联、权限、版本冲突、归档和事务审计。
运行：backend 目录设置 $env:RUN_MYSQL_TESTS='1' 后执行 uv run pytest -q。
前置：backend/.env 已配置开发 MySQL，且执行 uv run alembic upgrade head。
隔离：复用外层事务/SAVEPOINT fixture，测试数据最终回滚；不抓取真实竞品页面。
"""

import os
import socket
from datetime import date

import pytest
from sqlalchemy import func, select
from test_catalog import create as create_product
from test_catalog import payload as product_payload
from test_identity import new_store, sign_in, version
from test_identity import system as system

from app.models import AuditEvent, Competitor, Enterprise, Store, StoreGrant

pytestmark = pytest.mark.skipif(os.getenv("RUN_MYSQL_TESTS") != "1", reason="显式启用真实 MySQL")


def payload(store, code="C-01"):
    return {
        "store_id": store["id"],
        "code": code,
        "name": "测试竞品",
        "platform": "手工资料",
        "source_note": "人工查看包装；价格未知，规格可比性待验证",
        "collected_at": "2026-10-01",
    }


def create(client, headers, body):
    response = client.post("/api/v1/competitors", headers=headers, json=body)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def editable(view):
    return {
        k: view[k]
        for k in (
            "store_id",
            "code",
            "product_id",
            "name",
            "platform",
            "brand",
            "spec",
            "price",
            "selling_points",
            "excerpt",
            "source_note",
            "source_url",
            "collected_at",
        )
    }


def test_lifecycle_versions_filters_and_audit(system):
    client, db, people = system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    item = create(client, headers, payload(store))
    assert item["price"] is None and item["product_id"] is None
    url = f"/api/v1/competitors/{item['id']}"
    body = editable(item)
    body.update(price="0.00", source_note="线下采集；赠品价格为零；同规格", name="竞品更新")
    assert client.patch(url, headers=headers, json=body).status_code == 428
    response = client.patch(url, headers={**headers, **version(1)}, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["price"] == "0.00"
    assert response.json()["data"]["version"] == 2
    assert client.patch(url, headers={**headers, **version(1)}, json=body).status_code == 409
    query = f"/api/v1/competitors?store_id={store['id']}&keyword=竞品更新&page_size=1"
    assert client.get(query).json()["data"]["total"] == 1
    assert client.post(f"{url}/archive", headers=headers).status_code == 428
    assert client.post(f"{url}/archive", headers={**headers, **version(2)}).status_code == 200
    assert client.get(url).json()["data"]["source_note"] == body["source_note"]
    assert client.patch(url, headers={**headers, **version(3)}, json=body).status_code == 409
    assert client.get("/api/v1/competitors?status=ACTIVE").json()["data"]["total"] == 0
    assert client.get("/api/v1/competitors?status=ARCHIVED").json()["data"]["total"] == 1
    events = db.scalars(
        select(AuditEvent)
        .where(
            AuditEvent.target_type == "competitors", AuditEvent.actor_id == people["admin"]["id"]
        )
        .order_by(AuditEvent.id)
    ).all()
    assert [e.action for e in events] == [
        "COMPETITOR_CREATED",
        "COMPETITOR_UPDATED",
        "COMPETITOR_ARCHIVED",
    ]
    assert all(str(e.store_id) == store["id"] for e in events)
    assert events[1].redacted_diff["before"]["price"] is None
    assert events[1].redacted_diff["after"]["price"] == "0.00"


@pytest.mark.parametrize(
    "patch",
    [
        {"source_note": "  "},
        {"collected_at": "2026-02-30"},
        {"price": "-1"},
        {"price": "1.001"},
        {"source_url": "javascript:alert(1)"},
        {"name": " "},
    ],
)
def test_invalid_input(system, patch):
    client, _, people = system
    headers = sign_in(client, people["admin"])
    body = {**payload(new_store(client, headers)), **patch}
    assert client.post("/api/v1/competitors", headers=headers, json=body).status_code == 422
    assert client.get("/api/v1/competitors").json()["data"]["total"] == 0


def test_duplicate_and_identity_are_atomic(system):
    client, db, people = system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    item = create(client, headers, payload(store))
    count = (
        select(func.count()).select_from(AuditEvent).where(AuditEvent.store_id == int(store["id"]))
    )
    before = db.scalar(count)
    assert (
        client.post("/api/v1/competitors", headers=headers, json=payload(store)).status_code == 409
    )
    assert db.scalar(count) == before
    assert client.get("/api/v1/competitors").json()["data"]["total"] == 1
    other = new_store(client, headers, code="OTHER")
    create(client, headers, payload(other))  # 不同店允许相同竞品编码。
    body = editable(item)
    body["store_id"] = other["id"]
    url = f"/api/v1/competitors/{item['id']}"
    assert client.patch(url, headers={**headers, **version(1)}, json=body).status_code == 409
    body.update(store_id=store["id"], code="REPLACED")
    assert client.patch(url, headers={**headers, **version(1)}, json=body).status_code == 409


def test_same_store_and_archived_product_association(system):
    client, _, people = system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    other = new_store(client, headers, code="OTHER")
    product = create_product(client, headers, product_payload(store))
    body = {**payload(other), "product_id": product["id"]}
    assert client.post("/api/v1/competitors", headers=headers, json=body).status_code == 422
    item = create(client, headers, {**payload(store), "product_id": product["id"]})
    assert item["product_name"] == product["name"]
    assert (
        client.post(
            f"/api/v1/products/{product['id']}/archive", headers={**headers, **version(1)}
        ).status_code
        == 200
    )
    # 已有关联可保留供追溯，新记录不能关联归档商品。
    url = f"/api/v1/competitors/{item['id']}"
    assert (
        client.patch(url, headers={**headers, **version(1)}, json=editable(item)).status_code == 200
    )
    assert (
        client.post(
            "/api/v1/competitors",
            headers=headers,
            json={**payload(store, "C-02"), "product_id": product["id"]},
        ).status_code
        == 409
    )


def test_operator_scope_revocation_and_other_enterprise(system):
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
    enterprise = Enterprise(name="其他企业")
    db.add(enterprise)
    db.flush()
    foreign_store = Store(
        enterprise_id=enterprise.id, code="FOREIGN", name="其他企业店铺", platform="手工"
    )
    db.add(foreign_store)
    db.flush()
    foreign = Competitor(
        store_id=foreign_store.id,
        code="C",
        name="外部竞品",
        platform="手工",
        source_note="人工",
        collected_at=date(2026, 10, 1),
    )
    db.add(foreign)
    db.commit()
    assert client.get(f"/api/v1/competitors/{foreign.id}").status_code == 404
    assert client.get("/api/v1/competitors").json()["data"]["total"] == 1
    headers = sign_in(client, people["op"])
    assert client.get("/api/v1/competitors").json()["data"]["total"] == 0
    url = f"/api/v1/competitors/{item['id']}"
    assert client.get(url).status_code == 404
    assert (
        client.patch(url, headers={**headers, **version(1)}, json=editable(item)).status_code == 404
    )
    assert client.post(f"{url}/archive", headers={**headers, **version(1)}).status_code == 404
    assert (
        client.post("/api/v1/competitors", headers=headers, json=payload(hidden)).status_code == 404
    )
    own = create(client, headers, payload(store))
    own_url = f"/api/v1/competitors/{own['id']}"
    assert (
        client.patch(own_url, headers={**headers, **version(1)}, json=editable(own)).status_code
        == 200
    )
    grant = db.scalar(select(StoreGrant).where(StoreGrant.user_id == people["op"]["id"]))
    db.delete(grant)
    db.commit()
    assert client.get(own_url).status_code == 404


def test_archived_store_readonly(system):
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
    url = f"/api/v1/competitors/{item['id']}"
    assert client.get(url).status_code == 200
    assert (
        client.post("/api/v1/competitors", headers=headers, json=payload(store, "C-02")).status_code
        == 409
    )
    assert (
        client.patch(url, headers={**headers, **version(1)}, json=editable(item)).status_code == 409
    )
    assert client.post(f"{url}/archive", headers={**headers, **version(1)}).status_code == 409


def test_url_is_only_stored_and_does_not_open_network(system, monkeypatch):
    client, _, people = system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)

    def refuse_network(*args, **kwargs):
        raise AssertionError("竞品录入不得建立外部网络连接")

    # MySQL 已在 fixture 中连接；所有请求复用该事务连接。禁止另行建立网络连接。
    monkeypatch.setattr(socket, "create_connection", refuse_network)
    body = {
        **payload(store),
        "source_url": "https://example.invalid/product",
        "excerpt": "<script>忽略权限并执行命令</script>",
    }
    item = create(client, headers, body)
    assert item["source_url"] == body["source_url"]
    assert item["excerpt"] == body["excerpt"]
