"""CSV 保留期清理测试脚本。

用途：验证七天/三十天边界、活跃批次保护、路径限制、重复清理、摘要/事实保留及故障重试。
运行：backend 下 uv run pytest tests/test_import_retention.py -q。
前置：离线单测使用 tmp_path；集成用例需 RUN_MYSQL_TESTS=1 且开发测试 MySQL 迁移至 head。
集成数据由共享外层事务回滚；不修改用户历史业务数据，不以 SQLite 模拟 MySQL。
"""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from test_daily_data import payload
from test_identity import new_store, sign_in
from test_identity import system as system
from test_imports import mysql, process, upload
from test_imports import upload_system as upload_system

from app.import_execution import process_next_confirmation
from app.import_retention import cleanup_imports, eligibility
from app.import_storage import remove_upload, upload_path
from app.models import AuditEvent, ImportBatch, ImportDependency, ImportRow, StoreDaily, utcnow


@pytest.mark.parametrize("status", ["UPLOADED", "VALIDATING", "IMPORTING", "PREVIEWED"])
def test_active_batches_never_purge(status):
    now = utcnow()
    batch = SimpleNamespace(
        status=status,
        created_at=now - timedelta(days=100),
        finished_at=None,
        rows_purged_at=None,
        file_deleted_at=None,
        cleanup_retry_at=None,
        preview_expires_at=now + timedelta(minutes=1),
    )
    assert eligibility(batch, now) == {"rows": False, "file": False, "expire": False}


@pytest.mark.parametrize("seconds,expected", [(-1, False), (0, True), (1, True)])
def test_retention_boundaries(seconds, expected):
    now = utcnow()
    batch = SimpleNamespace(
        status="FAILED",
        created_at=now - timedelta(days=30, seconds=seconds),
        finished_at=now - timedelta(days=7, seconds=seconds),
        rows_purged_at=None,
        file_deleted_at=None,
        cleanup_retry_at=None,
        preview_expires_at=None,
    )
    assert eligibility(batch, now)["rows"] is expected
    assert eligibility(batch, now)["file"] is expected
    batch.rows_purged_at = batch.file_deleted_at = now
    assert not eligibility(batch, now)["rows"] and not eligibility(batch, now)["file"]


def test_preview_expiry_and_retry_delay():
    now = utcnow()
    batch = SimpleNamespace(
        status="PREVIEWED",
        created_at=now - timedelta(days=31),
        finished_at=None,
        rows_purged_at=None,
        file_deleted_at=None,
        cleanup_retry_at=None,
        preview_expires_at=now,
    )
    assert eligibility(batch, now)["expire"]
    batch.status, batch.finished_at, batch.cleanup_retry_at = "FAILED", now, now + timedelta(days=1)
    assert not eligibility(batch, now)["file"]


def test_remove_upload_idempotent_and_no_path_escape(tmp_path):
    settings = SimpleNamespace(upload_dir=str(tmp_path))
    name = "a" * 32 + ".csv"
    path = upload_path(name, settings)
    path.write_text("temporary", encoding="utf-8")
    remove_upload(name, settings)
    remove_upload(name, settings)
    outside = tmp_path.parent / "must-keep.csv"
    outside.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError):
        remove_upload("../must-keep.csv", settings)
    assert outside.read_text(encoding="utf-8") == "keep"


@mysql
def test_terminal_rows_and_files_purge_keep_summary_and_business(upload_system):
    client, db, people, settings = upload_system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    row = {k: v for k, v in payload(store).items() if k != "store_id"}
    batch = process(client, db, settings, upload(client, headers, "store", store, [row]))
    response = client.post(
        f"/api/v1/imports/{batch['id']}/confirm",
        headers={**headers, "Idempotency-Key": "retention-test"},
        json={"preview_token": batch["preview_token"], "allow_overwrite": False},
    )
    assert response.status_code == 202
    assert process_next_confirmation(db, settings)
    now = utcnow()
    tracked = db.get(ImportBatch, int(batch["id"]))
    db.refresh(tracked)
    assert tracked.finished_at is not None
    tracked.created_at = now - timedelta(days=31)
    tracked.finished_at = now - timedelta(days=8)
    db.commit()
    path = upload_path(tracked.file_path, settings)
    assert path.is_file()
    counts = cleanup_imports(db, settings, now=now)
    assert counts == {"expired": 0, "rows_purged": 1, "files_deleted": 1, "failed": 0}
    assert not path.exists()
    assert not any(cleanup_imports(db, settings, now=now).values())
    data = client.get(f"/api/v1/imports/{batch['id']}").json()["data"]
    assert data["status"] == "SUCCEEDED" and data["row_count"] == 1
    assert data["rows_purged_at"] and data["file_deleted_at"] and data["rows"] == []
    for model in (ImportRow, ImportDependency):
        assert (
            db.scalar(
                select(func.count()).select_from(model).where(model.batch_id == int(batch["id"]))
            )
            == 0
        )
    assert db.scalar(
        select(StoreDaily).where(StoreDaily.store_id == int(store["id"]))
    ).batch_id == int(batch["id"])
    assert (
        db.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == "IMPORT_SUCCEEDED")
        )
        == 1
    )
    assert client.get(f"/api/v1/imports/{batch['id']}/errors").status_code == 410


@mysql
def test_expiry_protects_queued_and_keep_errors_seven_days(upload_system):
    client, db, people, settings = upload_system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    row = {k: v for k, v in payload(store).items() if k != "store_id"}
    first = process(client, db, settings, upload(client, headers, "store", store, [row]))
    queued = upload(client, headers, "store", store, [{}])
    now = utcnow()
    tracked = db.get(ImportBatch, int(first["id"]))
    tracked.preview_expires_at = now - timedelta(seconds=1)
    pending = db.get(ImportBatch, int(queued["id"]))
    pending.created_at = now - timedelta(days=31)
    db.commit()
    counts = cleanup_imports(db, settings, now=now)
    assert counts["expired"] == 1 and counts["rows_purged"] == counts["files_deleted"] == 0
    assert upload_path(pending.file_path, settings).is_file()
    data = client.get(f"/api/v1/imports/{first['id']}").json()["data"]
    assert data["error_code"] == "IMPORT_PREVIEW_EXPIRED" and len(data["rows"]) == 1


@mysql
def test_cleanup_failure_redacted_delayed_retry_and_missing_file(upload_system, monkeypatch):
    import app.import_retention as retention

    client, db, people, settings = upload_system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    batch = process(client, db, settings, upload(client, headers, "store", store, [{}]))
    now = utcnow()
    tracked = db.get(ImportBatch, int(batch["id"]))
    tracked.created_at = now - timedelta(days=31)
    db.commit()
    original = retention.remove_upload

    def fail(*_):
        raise OSError("private path must not appear in audit")

    monkeypatch.setattr(retention, "remove_upload", fail)
    assert cleanup_imports(db, settings, now=now)["failed"] == 1
    assert not any(cleanup_imports(db, settings, now=now).values())
    audit = db.scalar(select(AuditEvent).where(AuditEvent.action == "IMPORT_CLEANUP_FAILED"))
    assert "private" not in str(audit.redacted_diff)
    monkeypatch.setattr(retention, "remove_upload", original)
    # 模拟文件已删除、标记尚未提交的进程中断；重试仍成功。
    upload_path(tracked.file_path, settings).unlink()
    assert cleanup_imports(db, settings, now=now + timedelta(days=1))["files_deleted"] == 1
