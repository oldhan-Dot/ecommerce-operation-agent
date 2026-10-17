"""CSV 模板、持久预检批次与分页错误；本阶段不提供确认入库接口。"""

import csv
import hashlib
import io
import secrets
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.catalog import writable_store
from app.core.errors import AppError
from app.csv_preview import MAX_BYTES, TEMPLATES, CsvFormatError, preview
from app.dependencies import Principal, current_user, get_db, store_scope
from app.identity import audit, result, stamp
from app.import_storage import read_upload, upload_path, upload_root
from app.models import AuditEvent, ImportBatch, ImportDependency, ImportRow, Store, User, utcnow

ImportType = Literal["stores", "products", "competitors", "store", "product", "ad"]
router = APIRouter(prefix="/api/v1", tags=["imports"])


def authorize_kind(kind, p):
    if kind == "stores" and p.user.role != "ADMIN":
        raise AppError(403, "AUTH_FORBIDDEN", "仅管理员可导入店铺")


@router.get("/import-templates/{kind}")
def template(kind: ImportType, p=Depends(current_user)):
    authorize_kind(kind, p)
    output = io.StringIO(newline="")
    csv.writer(output).writerow(TEMPLATES[kind])
    return Response(
        output.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{kind}-template.csv"'},
    )


def batch_scope(p):
    query = select(ImportBatch).where(ImportBatch.enterprise_id == p.user.enterprise_id)
    if p.user.role == "OPERATOR":
        query = query.where(ImportBatch.creator_id == p.user.id)
    # 店铺导入没有单个店铺范围，只向管理员开放；其他批次每次校验当前店铺授权。
    return query.where(
        or_(
            ImportBatch.store_id.in_(store_scope(p).with_only_columns(Store.id)),
            ImportBatch.store_id.is_(None) if p.user.role == "ADMIN" else False,
        )
    )


def visible_batch(db, p, batch_id):
    batch = db.scalar(batch_scope(p).where(ImportBatch.id == batch_id))
    if not batch:
        raise AppError(404, "RESOURCE_NOT_FOUND", "导入批次不存在")
    return batch


def batch_view(batch):
    return {
        "id": str(batch.id),
        "type": batch.import_type,
        "store_id": str(batch.store_id) if batch.store_id else None,
        "status": batch.status,
        "row_count": batch.row_count,
        "new_count": batch.new_count,
        "overwrite_count": batch.overwrite_count,
        "error_count": batch.error_count,
        "error_code": batch.error_code,
        "error_message": batch.error_message,
        "created_at": stamp(batch.created_at),
        "preview_expires_at": stamp(batch.preview_expires_at),
        "can_confirm": False,
    }


@router.post("/imports", status_code=202)
def upload(
    request: Request,
    type: ImportType = Form(...),
    store_id: str | None = Form(None),
    allow_duplicate: bool = Form(False),
    file: UploadFile = File(...),
    p=Depends(current_user),
    db=Depends(get_db),
):
    authorize_kind(type, p)
    if type == "stores":
        if store_id:
            raise AppError(422, "DATA_INVALID", "店铺模板不指定现有店铺")
        store = None
    else:
        if (
            not store_id
            or not store_id.isascii()
            or not store_id.isdigit()
            or not 0 < int(store_id) < 2**64
        ):
            raise AppError(422, "DATA_INVALID", "请选择有效店铺")
        store = writable_store(db, p, store_id)
    if not (file.filename or "").lower().endswith(".csv"):
        raise AppError(422, "IMPORT_FILE_INVALID", "仅支持 UTF-8 CSV 文件，不支持 Excel 文件")
    raw = file.file.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise AppError(413, "IMPORT_FILE_TOO_LARGE", "文件不能超过 10 MB")
    fingerprint = hashlib.sha256(raw).digest()
    duplicate = db.scalar(
        select(ImportBatch.id)
        .where(
            ImportBatch.creator_id == p.user.id,
            ImportBatch.import_type == type,
            ImportBatch.store_id == (store.id if store else None),
            ImportBatch.file_sha256 == fingerprint,
        )
        .limit(1)
    )
    if duplicate and not allow_duplicate:
        raise AppError(
            409, "IMPORT_DUPLICATE_FILE", "同类同店文件已上传，请查看已有批次或明确选择再次上传"
        )
    name = secrets.token_hex(16) + ".csv"
    settings = request.app.state.settings
    upload_root(settings).mkdir(parents=True, exist_ok=True)
    path = upload_path(name, settings)
    try:
        with path.open("xb") as handle:
            handle.write(raw)
        batch = ImportBatch(
            enterprise_id=p.user.enterprise_id,
            creator_id=p.user.id,
            store_id=store.id if store else None,
            import_type=type,
            file_sha256=fingerprint,
            file_path=name,
            created_by=p.user.id,
        )
        db.add(batch)
        db.flush()
        audit(db, request, p.user, "IMPORT_UPLOADED", batch, changes={"type": type})
        db.commit()
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return result(request, batch_view(batch))


@router.get("/imports")
def list_batches(
    request: Request, page: int = Query(1, ge=1), p=Depends(current_user), db=Depends(get_db)
):
    query = batch_scope(p)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.scalars(query.order_by(ImportBatch.id.desc()).offset((page - 1) * 20).limit(20)).all()
    return result(
        request,
        {"items": [batch_view(b) for b in rows], "total": total, "page": page, "page_size": 20},
    )


@router.get("/imports/{batch_id}")
def detail(
    batch_id: int,
    request: Request,
    page: int = Query(1, ge=1),
    p=Depends(current_user),
    db=Depends(get_db),
):
    batch = visible_batch(db, p, batch_id)
    rows = db.scalars(
        select(ImportRow)
        .where(ImportRow.batch_id == batch.id)
        .order_by(ImportRow.row_no)
        .offset((page - 1) * 20)
        .limit(20)
    ).all()
    return result(
        request,
        {
            **batch_view(batch),
            "page": page,
            "page_size": 20,
            "rows": [
                {
                    "row_no": r.row_no,
                    "business_key": r.business_key,
                    "action": r.action,
                    "after": r.normalized_payload,
                    "before": r.before_payload,
                    "errors": r.errors,
                    "warnings": r.warnings,
                }
                for r in rows
            ],
        },
    )


def csv_safe(value):
    """错误下载也可能含用户编码，防止 Excel 将单元格识别为公式。"""
    text = str(value)
    return (
        "'" + text
        if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n"))
        else text
    )


@router.get("/imports/{batch_id}/errors")
def errors_csv(batch_id: int, p=Depends(current_user), db=Depends(get_db)):
    visible_batch(db, p, batch_id)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["row_no", "business_key", "field", "message"])
    for row in db.scalars(
        select(ImportRow)
        .where(ImportRow.batch_id == batch_id, ImportRow.error_count > 0)
        .order_by(ImportRow.row_no)
    ):
        for error in row.errors:
            writer.writerow(
                [
                    row.row_no,
                    csv_safe(row.business_key),
                    csv_safe(error["field"]),
                    csv_safe(error["message"]),
                ]
            )
    return Response(
        output.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="import-{batch_id}-errors.csv"'},
    )


def process_next_import(db: Session, settings):
    """抢占上传批次或过期租约；结果和租约所有权同事务复核，迟到 Worker 不覆盖新结果。"""
    now = utcnow()
    batch = db.scalar(
        select(ImportBatch)
        .where(
            or_(
                ImportBatch.status == "UPLOADED",
                (ImportBatch.status == "VALIDATING") & (ImportBatch.lease_until < now),
            )
        )
        .order_by(ImportBatch.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if not batch:
        db.rollback()
        return False
    owner = secrets.token_hex(16)
    batch.status, batch.lease_owner, batch.lease_until = (
        "VALIDATING",
        owner,
        now + timedelta(minutes=5),
    )
    batch_id = batch.id
    db.commit()
    try:
        creator = db.get(User, batch.creator_id)
        if not creator or creator.status != "ACTIVE" or creator.must_change_password:
            raise CsvFormatError("上传账号当前不可用，请重新登录后上传")
        p = Principal(creator, None, "")
        authorize_kind(batch.import_type, p)
        store = None
        if batch.store_id:
            store = db.scalar(store_scope(p).where(Store.id == batch.store_id))
            if not store or store.status != "ACTIVE":
                raise CsvFormatError("店铺授权或状态已变化，请重新上传")
        raw = read_upload(batch.file_path, settings)
        if not secrets.compare_digest(hashlib.sha256(raw).digest(), batch.file_sha256):
            raise CsvFormatError("上传文件摘要已变化，请重新上传")
        rows, dependencies = preview(db, batch, store, raw)
        db.rollback()
        batch = db.scalar(
            select(ImportBatch)
            .where(ImportBatch.id == batch_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if batch.lease_owner != owner or batch.status != "VALIDATING":
            db.rollback()
            return True
        db.execute(delete(ImportRow).where(ImportRow.batch_id == batch_id))
        db.execute(delete(ImportDependency).where(ImportDependency.batch_id == batch_id))
        for start in range(0, len(rows), 500):
            db.bulk_insert_mappings(
                ImportRow,
                [
                    {
                        "batch_id": batch_id,
                        "row_no": r.row_no,
                        "business_key": r.business_key,
                        "normalized_payload": r.payload,
                        "before_payload": r.before,
                        "expected_target_version": r.expected_version,
                        "errors": r.errors,
                        "warnings": r.warnings,
                        "error_count": len(r.errors),
                        "action": "ERROR" if r.errors else r.action,
                    }
                    for r in rows[start : start + 500]
                ],
            )
        if dependencies:
            db.bulk_insert_mappings(
                ImportDependency,
                [
                    {"batch_id": batch_id, "entity_type": t, "entity_id": i, "expected_version": v}
                    for (t, i), v in dependencies.items()
                ],
            )
        batch.row_count = len(rows)
        batch.error_count = sum(bool(r.errors) for r in rows)
        batch.new_count = sum(not r.errors and r.action == "NEW" for r in rows)
        batch.overwrite_count = sum(not r.errors and r.action == "OVERWRITE" for r in rows)
        batch.status = "INVALID" if batch.error_count else "PREVIEWED"
        batch.preview_token = secrets.token_hex(32) if not batch.error_count else None
        batch.preview_expires_at = (
            utcnow() + timedelta(minutes=30) if not batch.error_count else None
        )
        batch.lease_owner = batch.lease_until = None
        db.add(
            AuditEvent(
                enterprise_id=batch.enterprise_id,
                actor_id=batch.creator_id,
                store_id=batch.store_id,
                action="IMPORT_PREVIEWED",
                target_type="import_batches",
                target_id=str(batch.id),
                request_id=owner,
                result="FAILED" if batch.error_count else "SUCCEEDED",
                redacted_diff={
                    "row_count": batch.row_count,
                    "new_count": batch.new_count,
                    "overwrite_count": batch.overwrite_count,
                    "error_count": batch.error_count,
                },
            )
        )
        db.commit()
    except (CsvFormatError, AppError, OSError, ValueError) as exc:
        db.rollback()
        fail_import(
            db,
            batch_id,
            owner,
            "IMPORT_INVALID_FILE",
            str(exc)
            if isinstance(exc, CsvFormatError)
            else "预检无法完成，请检查授权或文件后重新上传",
        )
    except Exception:
        db.rollback()
        fail_import(db, batch_id, owner, "IMPORT_PREVIEW_FAILED", "预检暂时失败，请稍后重新上传")
    return True


def fail_import(db, batch_id, owner, code, message):
    batch = db.scalar(select(ImportBatch).where(ImportBatch.id == batch_id).with_for_update())
    if batch and batch.lease_owner == owner and batch.status == "VALIDATING":
        batch.status, batch.error_code, batch.error_message = "FAILED", code, message
        batch.lease_owner = batch.lease_until = None
        db.add(
            AuditEvent(
                enterprise_id=batch.enterprise_id,
                actor_id=batch.creator_id,
                store_id=batch.store_id,
                action="IMPORT_PREVIEW_FAILED",
                target_type="import_batches",
                target_id=str(batch.id),
                request_id=owner,
                result="FAILED",
                redacted_diff={"code": code},
            )
        )
        db.commit()
    else:
        db.rollback()
