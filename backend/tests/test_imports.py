"""CSV 模板与持久预检测试脚本。

用途：验证六类模板、严格解析、逐行错误、版本依赖、权限、文件限制、租约恢复及业务表只读。
运行：backend 执行 uv run pytest tests/test_imports.py -q；MySQL 用例需设置 RUN_MYSQL_TESTS=1。
前置：MySQL 用例需开发数据库已迁移至 head；上传文件写入 pytest tmp_path。
隔离：API 与 Worker 共用外层事务/SAVEPOINT，测试数据最终回滚，临时文件由 pytest 管理。
"""

import csv
import io
import os
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select
from test_catalog import create as create_product
from test_catalog import payload as product_payload
from test_daily_data import create as create_daily
from test_daily_data import payload as daily_payload
from test_identity import new_store, sign_in
from test_identity import system as system

from app.csv_preview import MAX_BYTES, TEMPLATES, CsvFormatError, read_csv
from app.import_execution import process_next_confirmation
from app.import_storage import upload_path
from app.imports import csv_safe, fail_import, process_next_import
from app.models import (
    AdDaily,
    AuditEvent,
    Competitor,
    ImportBatch,
    ImportDependency,
    ImportRow,
    Product,
    ProductDaily,
    Sku,
    Store,
    StoreDaily,
    StoreGrant,
    User,
    utcnow,
)


def csv_bytes(kind, rows):
    """按真实下载模板生成测试 CSV，保留空单元格和带换行的字段。"""
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=TEMPLATES[kind])
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in TEMPLATES[kind]})
    return output.getvalue().encode("utf-8-sig")


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\xff",
        b"a,a\n1,2\n",
        b"business_date,paid_amount\n2026-01-01,1\n",
        b'business_date,paid_amount,paid_orders,visitors,refund_amount\n"bad',
        b"business_date,paid_amount,paid_orders,visitors,refund_amount\n1,2\n",
    ],
)
def test_malformed_csv_rejected(raw):
    with pytest.raises(CsvFormatError):
        read_csv(raw, "store")


def test_file_and_row_limits_and_multiline_positions():
    with pytest.raises(CsvFormatError, match="10 MB"):
        read_csv(b"x" * (MAX_BYTES + 1), "store")
    with pytest.raises(CsvFormatError, match="10000"):
        read_csv(csv_bytes("store", [{}] * 10001), "store")
    with pytest.raises(CsvFormatError, match="没有数据"):
        read_csv(csv_bytes("store", []), "store")
    result = read_csv(
        csv_bytes("stores", [{"notes": "第一行\n第二行"}, {"notes": "第三行"}]), "stores"
    )
    assert [n for n, _ in result] == [2, 4]


@pytest.mark.parametrize("value", ["=1+1", "+SUM(A1)", "-1", "@ABC", "  =2", "\tABC", "\rABC"])
def test_error_csv_formula_escaping(value):
    assert csv_safe(value).startswith("'")


@pytest.fixture
def upload_system(system, tmp_path):
    client, db, people = system
    settings = client.app.state.settings.model_copy(update={"upload_dir": str(tmp_path)})
    client.app.state.settings = settings
    return client, db, people, settings


mysql = pytest.mark.skipif(os.getenv("RUN_MYSQL_TESTS") != "1", reason="显式启用真实 MySQL")


def upload(client, headers, kind, store, rows, allow_duplicate=False):
    data = {"type": kind, "allow_duplicate": str(allow_duplicate).lower()}
    if store is not None:
        data["store_id"] = store["id"]
    response = client.post(
        "/api/v1/imports",
        headers=headers,
        data=data,
        files={"file": ("input.csv", csv_bytes(kind, rows), "text/csv")},
    )
    assert response.status_code == 202, response.text
    return response.json()["data"]


def process(client, db, settings, batch):
    assert process_next_import(db, settings)
    response = client.get(f"/api/v1/imports/{batch['id']}")
    assert response.status_code == 200
    return response.json()["data"]


@mysql
@pytest.mark.parametrize("kind", list(TEMPLATES))
def test_six_templates_preview_readonly_then_import(upload_system, kind):
    client, db, people, settings = upload_system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    reference = (
        create_product(client, headers, product_payload(store)) if kind == "product" else None
    )
    day = utcnow().date().isoformat()
    values = {
        "stores": {"code": "CSV-S", "name": "CSV店铺", "platform": "手工"},
        "products": {
            "code": "CSV-P",
            "name": "CSV商品",
            "category": "食品",
            "selling_points": "卖点",
            "sku_code": "CSV-SKU",
            "spec_name": "默认规格",
            "price": "10",
        },
        "competitors": {
            "code": "CSV-C",
            "name": "竞品",
            "platform": "人工",
            "source_note": "来源说明",
            "collected_at": day,
        },
        "store": {
            "business_date": day,
            "paid_amount": "1",
            "paid_orders": "0",
            "visitors": "0",
            "refund_amount": "2",
        },
        "product": {
            "business_date": day,
            "product_code": reference["code"] if reference else "",
            "paid_amount": "1",
            "paid_orders": "0",
            "visitors": "0",
            "refund_amount": "2",
        },
        "ad": {
            "business_date": day,
            "channel": "渠道",
            "spend": "0",
            "impressions": "0",
            "clicks": "1",
        },
    }
    enterprise = db.get(User, people["admin"]["id"]).enterprise_id
    models = [Product, Sku, Competitor, StoreDaily, ProductDaily, AdDaily]

    def counts():
        return [
            db.scalar(select(func.count()).select_from(m).where(m.store_id == int(store["id"])))
            for m in models
        ] + [
            db.scalar(
                select(func.count()).select_from(Store).where(Store.enterprise_id == enterprise)
            )
        ]

    before = counts()
    download = client.get(f"/api/v1/import-templates/{kind}")
    assert download.status_code == 200 and download.content.startswith(b"\xef\xbb\xbf")
    assert next(csv.reader(io.StringIO(download.content.decode("utf-8-sig")))) == TEMPLATES[kind]
    batch = upload(client, headers, kind, None if kind == "stores" else store, [values[kind]])
    assert batch["status"] == "UPLOADED"
    result = process(client, db, settings, batch)
    assert result["status"] == "PREVIEWED", result
    assert result["new_count"] == 1 and result["error_count"] == 0
    assert counts() == before
    assert client.get(f"/api/v1/imports/{batch['id']}").json()["data"]["rows"] == result["rows"]
    assert (
        client.post(f"/api/v1/imports/{batch['id']}/confirm", headers=headers, json={}).status_code
        == 422
    )
    if kind == "ad":
        assert result["rows"][0]["warnings"]
    confirmed = client.post(
        f"/api/v1/imports/{batch['id']}/confirm",
        headers={**headers, "Idempotency-Key": "six-template-test"},
        json={"preview_token": result["preview_token"], "allow_overwrite": False},
    )
    assert confirmed.status_code == 202, confirmed.text
    assert process_next_confirmation(db, settings)
    assert client.get(f"/api/v1/imports/{batch['id']}").json()["data"]["status"] == "SUCCEEDED"
    after = counts()
    increments = [0] * len(before)
    for index in {
        "stores": [6],
        "products": [0, 1],
        "competitors": [2],
        "store": [3],
        "product": [4],
        "ad": [5],
    }[kind]:
        increments[index] = 1
    assert after == [count + increment for count, increment in zip(before, increments, strict=True)]


@mysql
def test_duplicate_rows_all_invalid_and_error_download(upload_system):
    client, db, people, settings = upload_system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    row = {
        "business_date": "2026-10-01",
        "paid_amount": "1",
        "paid_orders": "1",
        "visitors": "1",
        "refund_amount": "0",
    }
    batch = upload(client, headers, "store", store, [row, {**row, "paid_amount": ""}])
    result = process(client, db, settings, batch)
    assert result["status"] == "INVALID" and result["error_count"] == 2
    assert all(
        r["action"] == "ERROR" and any("重复" in e["message"] for e in r["errors"])
        for r in result["rows"]
    )
    errors = client.get(f"/api/v1/imports/{batch['id']}/errors")
    assert errors.status_code == 200 and "业务键重复" in errors.content.decode("utf-8-sig")
    assert client.get("/api/v1/daily-data/store").json()["data"]["total"] == 0


@mysql
def test_products_group_consistency_and_sku_ownership(upload_system):
    client, db, people, settings = upload_system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    existing = create_product(client, headers, product_payload(store))
    row = {
        "code": "CSV-P",
        "name": "商品",
        "category": "食品",
        "selling_points": "卖点",
        "sku_code": "NEW-A",
        "spec_name": "默认",
        "price": "1",
    }
    batch = upload(
        client,
        headers,
        "products",
        store,
        [
            row,
            {**row, "name": "不同名称", "sku_code": "NEW-B"},
            {**row, "code": "OTHER", "sku_code": existing["skus"][0]["code"]},
        ],
    )
    result = process(client, db, settings, batch)
    assert result["error_count"] == 3
    assert all(any("必须一致" in e["message"] for e in r["errors"]) for r in result["rows"][:2])
    assert any("其他商品" in e["message"] for e in result["rows"][2]["errors"])


@mysql
def test_overwrite_dependencies_duplicate_upload_and_lease_recovery(upload_system):
    client, db, people, settings = upload_system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    current = create_daily(client, headers, daily_payload(store))
    row = {k: v for k, v in daily_payload(store).items() if k != "store_id"}
    row["paid_amount"] = "50"
    batch = upload(client, headers, "store", store, [row])
    duplicate = client.post(
        "/api/v1/imports",
        headers=headers,
        data={"type": "store", "store_id": store["id"]},
        files={"file": ("a.csv", csv_bytes("store", [row]))},
    )
    assert duplicate.status_code == 409
    tracked = db.get(ImportBatch, int(batch["id"]))
    tracked.status, tracked.lease_owner, tracked.lease_until = (
        "VALIDATING",
        "expired",
        utcnow() - timedelta(seconds=1),
    )
    db.commit()
    result = process(client, db, settings, batch)
    assert (
        result["overwrite_count"] == 1 and result["rows"][0]["before"]["paid_amount"] == "1000.00"
    )
    dependency = db.scalar(
        select(ImportDependency).where(
            ImportDependency.batch_id == int(batch["id"]),
            ImportDependency.entity_type == "store_daily",
        )
    )
    assert dependency.entity_id == int(current["id"]) and dependency.expected_version == 1
    fail_import(db, int(batch["id"]), "expired", "TEST", "迟到旧 Worker")
    assert client.get(f"/api/v1/imports/{batch['id']}").json()["data"]["status"] == "PREVIEWED"
    assert not process_next_import(db, settings)
    second = upload(client, headers, "store", store, [row], allow_duplicate=True)
    assert second["id"] != batch["id"]
    assert (
        client.get(f"/api/v1/daily-data/store/{current['id']}").json()["data"]["paid_amount"]
        == "1000.00"
    )


@mysql
def test_upload_limits_auth_and_storage_path(upload_system):
    client, db, people, settings = upload_system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    data = {"type": "store", "store_id": store["id"]}
    assert (
        client.post("/api/v1/imports", data=data, files={"file": ("a.csv", b"x")}).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/imports", headers=headers, data=data, files={"file": ("a.xlsx", b"x")}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/imports",
            headers=headers,
            data=data,
            files={"file": ("a.csv", b"x" * (MAX_BYTES + 1))},
        ).status_code
        == 413
    )
    batch = upload(client, headers, "store", store, [{}])
    tracked = db.get(ImportBatch, int(batch["id"]))
    assert upload_path(tracked.file_path, settings).is_file()
    with pytest.raises(ValueError):
        upload_path("../../password.env", settings)
    assert "file_path" not in client.get(f"/api/v1/imports/{batch['id']}").json()["data"]
    upload_path(tracked.file_path, settings).write_bytes(b"tampered")
    result = process(client, db, settings, batch)
    assert result["status"] == "FAILED" and "摘要" in result["error_message"]


@mysql
def test_permissions_revoked_before_worker(upload_system):
    client, db, people, settings = upload_system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    grant = StoreGrant(
        user_id=people["op"]["id"], store_id=int(store["id"]), granted_by=people["admin"]["id"]
    )
    db.add(grant)
    db.commit()
    headers = sign_in(client, people["op"])
    assert client.get("/api/v1/import-templates/stores").status_code == 403
    batch = upload(client, headers, "store", store, [{}])
    db.delete(grant)
    db.commit()
    assert process_next_import(db, settings)
    assert client.get(f"/api/v1/imports/{batch['id']}").status_code == 404
    assert client.get(f"/api/v1/imports/{batch['id']}/errors").status_code == 404
    assert client.get("/api/v1/imports").json()["data"]["total"] == 0
    stored = db.get(ImportBatch, int(batch["id"]))
    db.refresh(stored)
    assert stored.status == "FAILED"


@mysql
def test_bad_header_persists_failure_and_audit(upload_system):
    client, db, people, settings = upload_system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    response = client.post(
        "/api/v1/imports",
        headers=headers,
        data={"type": "store", "store_id": store["id"]},
        files={"file": ("bad.csv", b"bad,header\n1,2\n")},
    )
    assert response.status_code == 202
    batch = response.json()["data"]
    result = process(client, db, settings, batch)
    assert result["status"] == "FAILED" and "表头" in result["error_message"]
    actions = db.scalars(
        select(AuditEvent.action)
        .where(AuditEvent.target_type == "import_batches", AuditEvent.target_id == batch["id"])
        .order_by(AuditEvent.id)
    ).all()
    assert actions == ["IMPORT_UPLOADED", "IMPORT_PREVIEW_FAILED"]


@mysql
def test_ten_thousand_rows_preview_without_business_writes(upload_system):
    client, db, people, settings = upload_system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    rows = [
        {
            "business_date": (date(1990, 1, 1) + timedelta(days=i)).isoformat(),
            "paid_amount": "0",
            "paid_orders": "0",
            "visitors": "0",
            "refund_amount": "0",
        }
        for i in range(10000)
    ]
    batch = upload(client, headers, "store", store, rows)
    result = process(client, db, settings, batch)
    assert result["status"] == "PREVIEWED"
    assert result["row_count"] == result["new_count"] == 10000
    assert (
        db.scalar(
            select(func.count())
            .select_from(ImportRow)
            .where(ImportRow.batch_id == int(batch["id"]))
        )
        == 10000
    )
    assert client.get("/api/v1/daily-data/store").json()["data"]["total"] == 0
