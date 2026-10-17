"""CSV 保留期清理：只处理终态批次，不删除批次、审计或已入库业务记录。"""

import secrets
from datetime import timedelta

from sqlalchemy import delete, or_, select

from app.import_execution import event
from app.import_storage import remove_upload
from app.models import ImportBatch, ImportDependency, ImportRow, utcnow

TERMINAL = ("SUCCEEDED", "FAILED", "INVALID")


def eligibility(batch, now):
    """使用独立完成时间计算明细保留期，清理操作更新 updated_at 不会重置截止日。"""
    terminal = batch.status in TERMINAL and batch.finished_at is not None
    return {
        "rows": terminal
        and batch.rows_purged_at is None
        and batch.finished_at <= now - timedelta(days=7),
        "file": terminal
        and batch.file_deleted_at is None
        and batch.created_at <= now - timedelta(days=30)
        and (batch.cleanup_retry_at is None or batch.cleanup_retry_at <= now),
        "expire": batch.status == "PREVIEWED"
        and batch.preview_expires_at is not None
        and batch.preview_expires_at <= now,
    }


def cleanup_imports(db, settings, *, now=None, limit=20):
    """每类最多处理 limit 个批次；逐批短事务并跳过并发锁定项，文件删除可安全重试。"""
    now = now or utcnow()
    counts = {"expired": 0, "rows_purged": 0, "files_deleted": 0, "failed": 0}
    conditions = {
        "expire": (ImportBatch.status == "PREVIEWED") & (ImportBatch.preview_expires_at <= now),
        "rows": ImportBatch.status.in_(TERMINAL)
        & ImportBatch.rows_purged_at.is_(None)
        & (ImportBatch.finished_at <= now - timedelta(days=7)),
        "file": ImportBatch.status.in_(TERMINAL)
        & ImportBatch.file_deleted_at.is_(None)
        & ImportBatch.finished_at.is_not(None)
        & (ImportBatch.created_at <= now - timedelta(days=30))
        & or_(ImportBatch.cleanup_retry_at.is_(None), ImportBatch.cleanup_retry_at <= now),
    }
    for step, condition in conditions.items():
        # 每次从队首取一个，成功会移出候选集；失败设下次重试时间，避免卡住后续批次。
        for _ in range(limit):
            batch = db.scalar(
                select(ImportBatch)
                .where(condition)
                .order_by(ImportBatch.id)
                .with_for_update(skip_locked=True)
                .limit(1)
                .execution_options(populate_existing=True)
            )
            if not batch:
                db.rollback()
                break
            if not eligibility(batch, now)[step]:
                db.rollback()
                break
            owner = secrets.token_hex(16)
            batch_id = batch.id
            try:
                if step == "expire":
                    batch.status = "FAILED"
                    batch.error_code = "IMPORT_PREVIEW_EXPIRED"
                    batch.error_message = "预检已过期，请重新上传并核对"
                    batch.finished_at = now
                    batch.lease_owner = batch.lease_until = None
                    event(db, batch, owner, "IMPORT_PREVIEW_EXPIRED", batch, {}, outcome="FAILED")
                    counter = "expired"
                elif step == "rows":
                    db.execute(delete(ImportRow).where(ImportRow.batch_id == batch.id))
                    db.execute(
                        delete(ImportDependency).where(ImportDependency.batch_id == batch.id)
                    )
                    batch.rows_purged_at = now
                    event(
                        db,
                        batch,
                        owner,
                        "IMPORT_DETAILS_PURGED",
                        batch,
                        {"row_count": batch.row_count},
                    )
                    counter = "rows_purged"
                else:
                    remove_upload(batch.file_path, settings)
                    batch.file_deleted_at = now
                    batch.cleanup_retry_at = None
                    event(db, batch, owner, "IMPORT_FILE_DELETED", batch, {})
                    counter = "files_deleted"
                db.commit()
                counts[counter] += 1
            except (OSError, ValueError):
                # 文件与数据库不能共享事务：先删文件再记标记；提交失败后重试允许文件已不存在。
                db.rollback()
                batch = db.scalar(
                    select(ImportBatch)
                    .where(ImportBatch.id == batch_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if not batch or not eligibility(batch, now)[step]:
                    db.rollback()
                    continue
                batch.cleanup_retry_at = now + timedelta(days=1)
                event(
                    db,
                    batch,
                    owner,
                    "IMPORT_CLEANUP_FAILED",
                    batch,
                    {"step": step, "code": "IMPORT_FILE_CLEANUP_FAILED"},
                    outcome="FAILED",
                )
                db.commit()
                counts["failed"] += 1
    return counts
