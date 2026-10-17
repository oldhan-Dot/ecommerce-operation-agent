"""CSV 确认入库测试脚本。

用途：检查严格确认契约、版本/新增键漂移、六模板执行、幂等、覆盖、失败回滚和租约恢复。
运行：backend 下 uv run pytest tests/test_import_confirm.py -q。
前置：离线单测无需数据库；集成用例需 RUN_MYSQL_TESTS=1 且开发 MySQL 已迁移至 head。
隔离：复用事务/SAVEPOINT 测试夹具，业务数据最终回滚；不以模拟数据库验证 MySQL 锁语义。
"""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from test_catalog import create as create_product
from test_catalog import payload as product_payload
from test_daily_data import create as create_daily
from test_daily_data import payload as daily_payload
from test_identity import new_store, sign_in
from test_identity import system as system
from test_imports import mysql, process, upload
from test_imports import upload_system as upload_system

from app.core.errors import AppError
from app.csv_preview import PreviewRow
from app.import_execution import process_next_confirmation, revalidate
from app.imports import ConfirmImport
from app.models import ImportBatch, StoreDaily, StoreGrant, User, utcnow


@pytest.mark.parametrize(
    "patch", [{"allow_overwrite": "true"}, {"preview_token": "wrong"}, {"extra": 1}]
)
def test_strict_confirm_contract(patch):
    with pytest.raises(ValidationError):
        ConfirmImport.model_validate({"preview_token": "a" * 64, "allow_overwrite": False, **patch})


@pytest.mark.parametrize(
    "change", ["unchanged", "new_key_taken", "payload", "dependency", "invalid"]
)
def test_revalidation_detects_drift(monkeypatch, change):
    """纯逻辑单测：比较器必须同时覆盖规范行与版本依赖，数据库锁留给集成测试。"""
    import hashlib

    import app.import_execution as execution

    raw = b"fixture"
    batch = SimpleNamespace(id=1, file_path="fixture.csv", file_sha256=hashlib.sha256(raw).digest())
    old = SimpleNamespace(
        row_no=2,
        business_key='"key"',
        normalized_payload={"x": "1"},
        action="NEW",
        expected_target_version=None,
        error_count=0,
    )
    row = PreviewRow(2, old.business_key, payload={"x": "1"})
    deps = {}
    if change == "new_key_taken":
        row.action, row.expected_version = "OVERWRITE", 1
    elif change == "payload":
        row.payload = {"x": "2"}
    elif change == "dependency":
        deps = {("products", 10): 2}
    elif change == "invalid":
        row.errors = [{"field": "x", "message": "invalid"}]
    monkeypatch.setattr(execution, "read_upload", lambda *_: raw)
    monkeypatch.setattr(execution, "preview", lambda *_, **__: ([row], deps))
    db = MagicMock()
    saved = MagicMock()
    saved.all.return_value = [old]
    db.scalars.side_effect = [saved, []]
    if change == "unchanged":
        assert revalidate(db, batch, None, None) == [row]
    else:
        with pytest.raises(AppError, match="预检"):
            revalidate(db, batch, None, None)


def confirm(client, headers, batch, *, overwrite=False, key="confirm-one"):
    return client.post(
        f"/api/v1/imports/{batch['id']}/confirm",
        headers={**headers, "Idempotency-Key": key},
        json={"preview_token": batch["preview_token"], "allow_overwrite": overwrite},
    )


def prepare_daily(upload_system):
    client, db, people, settings = upload_system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    row = {k: v for k, v in daily_payload(store).items() if k != "store_id"}
    batch = process(client, db, settings, upload(client, headers, "store", store, [row]))
    return client, db, settings, headers, store, row, batch


@mysql
def test_confirm_idempotency_and_success(upload_system):
    client, db, settings, headers, store, row, batch = prepare_daily(upload_system)
    assert confirm(client, headers, batch).status_code == 202
    assert confirm(client, headers, batch).json()["data"]["status"] == "IMPORTING"
    assert confirm(client, headers, batch, key="other").status_code == 409
    assert process_next_confirmation(db, settings)
    assert confirm(client, headers, batch).json()["data"]["status"] == "SUCCEEDED"
    assert not process_next_confirmation(db, settings)
    records = db.scalars(select(StoreDaily).where(StoreDaily.store_id == int(store["id"]))).all()
    assert len(records) == 1 and records[0].batch_id == int(batch["id"])


@mysql
@pytest.mark.parametrize("stage", ["before_confirm", "before_execute"])
def test_new_key_taken_rejects_whole_batch(upload_system, stage):
    client, db, settings, headers, store, row, batch = prepare_daily(upload_system)
    if stage == "before_execute":
        assert confirm(client, headers, batch).status_code == 202
    create_daily(client, headers, daily_payload(store))
    if stage == "before_confirm":
        response = confirm(client, headers, batch)
        assert (
            response.status_code == 409
            and response.json()["error"]["code"] == "IMPORT_STALE_PREVIEW"
        )
    else:
        assert process_next_confirmation(db, settings)
        result = client.get(f"/api/v1/imports/{batch['id']}").json()["data"]
        assert result["status"] == "FAILED" and result["error_code"] == "IMPORT_STALE_PREVIEW"
    assert db.scalar(select(StoreDaily).where(StoreDaily.store_id == int(store["id"]))).version == 1


@mysql
def test_expiry_and_explicit_overwrite(upload_system):
    client, db, settings, headers, store, row, batch = prepare_daily(upload_system)
    tracked = db.get(ImportBatch, int(batch["id"]))
    tracked.preview_expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert confirm(client, headers, batch).status_code == 409
    create_daily(client, headers, daily_payload(store))
    row["paid_amount"] = "50"
    batch = process(client, db, settings, upload(client, headers, "store", store, [row]))
    assert confirm(client, headers, batch).status_code == 409
    assert confirm(client, headers, batch, overwrite=True).status_code == 202
    assert process_next_confirmation(db, settings)
    record = db.scalar(select(StoreDaily).where(StoreDaily.store_id == int(store["id"])))
    assert str(record.paid_amount) == "50.00" and record.version == 2


@mysql
def test_failure_after_write_rolls_back_and_expired_lease_recovers(upload_system, monkeypatch):
    import app.import_execution as execution

    client, db, settings, headers, store, row, batch = prepare_daily(upload_system)
    assert confirm(client, headers, batch).status_code == 202
    tracked = db.get(ImportBatch, int(batch["id"]))
    tracked.lease_owner, tracked.lease_until = "old", utcnow() - timedelta(seconds=1)
    db.commit()
    original = execution.write_rows

    def fail_after_flush(*args):
        original(*args)
        raise RuntimeError("模拟业务写入之后的事务失败")

    monkeypatch.setattr(execution, "write_rows", fail_after_flush)
    assert process_next_confirmation(db, settings)
    assert db.scalar(select(StoreDaily).where(StoreDaily.store_id == int(store["id"]))) is None
    assert client.get(f"/api/v1/imports/{batch['id']}").json()["data"]["status"] == "FAILED"


@mysql
def test_product_partial_skus_preserve_existing(upload_system):
    client, db, people, settings = upload_system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    product = create_product(client, headers, product_payload(store))
    row = {k: product[k] for k in ("code", "name", "category", "selling_points")}
    row.update(sku_code="CSV-NEW", spec_name="新增规格", price="2")
    batch = process(client, db, settings, upload(client, headers, "products", store, [row]))
    assert confirm(client, headers, batch, overwrite=True).status_code == 202
    assert process_next_confirmation(db, settings)
    after = client.get(f"/api/v1/products/{product['id']}").json()["data"]
    assert after["version"] == 2
    assert {s["id"] for s in product["skus"]} < {s["id"] for s in after["skus"]}


@mysql
def test_same_key_cannot_confirm_another_batch(upload_system):
    client, db, settings, headers, store, row, batch = prepare_daily(upload_system)
    second = process(
        client, db, settings, upload(client, headers, "store", store, [row], allow_duplicate=True)
    )
    assert confirm(client, headers, batch).status_code == 202
    assert confirm(client, headers, second).status_code == 409
    assert client.get(f"/api/v1/imports/{second['id']}").json()["data"]["status"] == "PREVIEWED"


@mysql
@pytest.mark.parametrize("change", ["revoked", "disabled"])
def test_permission_changes_before_execution(upload_system, change):
    client, db, people, settings = upload_system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    grant = StoreGrant(
        user_id=people["op"]["id"], store_id=int(store["id"]), granted_by=people["admin"]["id"]
    )
    db.add(grant)
    db.commit()
    headers = sign_in(client, people["op"])
    row = {k: v for k, v in daily_payload(store).items() if k != "store_id"}
    batch = process(client, db, settings, upload(client, headers, "store", store, [row]))
    assert confirm(client, headers, batch).status_code == 202
    if change == "revoked":
        db.delete(grant)
    else:
        db.get(User, people["op"]["id"]).status = "DISABLED"
    db.commit()
    assert process_next_confirmation(db, settings)
    tracked = db.get(ImportBatch, int(batch["id"]))
    db.refresh(tracked)
    assert tracked.status == "FAILED"
    assert db.scalar(select(StoreDaily).where(StoreDaily.store_id == int(store["id"]))) is None
