"""确认前和执行前复核预检；业务写入、逐项审计和成功状态共用一个事务。"""

import hashlib
import secrets
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import or_, select

from app.ad_daily import AdDailyInput
from app.catalog import writable_store
from app.core.errors import AppError
from app.csv_preview import CsvFormatError, preview
from app.daily_data import StoreDailyInput
from app.dependencies import Principal
from app.import_storage import read_upload
from app.models import (
    AdDaily,
    AuditEvent,
    Competitor,
    Enterprise,
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
from app.product_daily import ProductDailyInput
from app.schemas import CompetitorInput, ProductInput, StoreInput


def stale():
    return AppError(409, "IMPORT_STALE_PREVIEW", "预检已过期或业务数据已变化，请重新上传并预检")


def lock_scope(db, batch, p):
    """企业→账号→授权→店铺→批次，防止确认期间角色/授权变更。"""
    db.scalar(select(Enterprise).where(Enterprise.id == batch.enterprise_id).with_for_update())
    user = db.scalar(
        select(User)
        .where(User.id == p.user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        not user
        or user.status != "ACTIVE"
        or user.must_change_password
        or user.enterprise_id != batch.enterprise_id
    ):
        raise AppError(403, "AUTH_FORBIDDEN", "上传账号当前不可用，请重新上传")
    if batch.import_type == "stores":
        if p.user.role != "ADMIN":
            raise AppError(403, "AUTH_FORBIDDEN", "仅管理员可导入店铺")
        return None
    if user.role == "OPERATOR" and not db.scalar(
        select(StoreGrant)
        .where(StoreGrant.user_id == user.id, StoreGrant.store_id == batch.store_id)
        .with_for_update()
    ):
        raise AppError(403, "AUTH_FORBIDDEN", "店铺授权已变化，请重新上传")
    return writable_store(db, p, batch.store_id)


def revalidate(db, batch, store, settings):
    """重新解析原文件并比较规范行及全部版本依赖，也检测预检后抢占的新增键。"""
    try:
        raw = read_upload(batch.file_path, settings)
    except (OSError, ValueError) as exc:
        raise stale() from exc
    if not secrets.compare_digest(hashlib.sha256(raw).digest(), batch.file_sha256):
        raise stale()
    try:
        fresh, dependencies = preview(db, batch, store, raw, lock=True)
    except CsvFormatError as exc:
        raise stale() from exc
    saved = db.scalars(
        select(ImportRow).where(ImportRow.batch_id == batch.id).order_by(ImportRow.row_no)
    ).all()
    expected = {
        (d.entity_type, d.entity_id): d.expected_version
        for d in db.scalars(select(ImportDependency).where(ImportDependency.batch_id == batch.id))
    }
    if dependencies != expected or len(fresh) != len(saved) or not fresh:
        raise stale()
    for row, old in zip(fresh, saved, strict=True):
        if (
            row.errors
            or old.error_count
            or row.row_no != old.row_no
            or row.business_key != old.business_key
            or row.payload != old.normalized_payload
            or row.action != old.action
            or row.expected_version != old.expected_target_version
        ):
            raise stale()
    return fresh


def event(db, batch, owner, action, target, changes, *, outcome="SUCCEEDED"):
    db.add(
        AuditEvent(
            enterprise_id=batch.enterprise_id,
            actor_id=batch.creator_id,
            store_id=batch.store_id,
            action=action,
            target_type=target.__tablename__,
            target_id=str(target.id),
            request_id=owner,
            result=outcome,
            redacted_diff=changes,
        )
    )


def write_rows(db, batch, rows, owner):
    """仅接受复核后的规范行；同商品聚合一次，SKU 只更新出现的项，保留其余 SKU。"""
    kind = batch.import_type
    model, schema = {
        "stores": (Store, StoreInput),
        "products": (Product, ProductInput),
        "competitors": (Competitor, CompetitorInput),
        "store": (StoreDaily, StoreDailyInput),
        "product": (ProductDaily, ProductDailyInput),
        "ad": (AdDaily, AdDailyInput),
    }[kind]
    condition = (
        Store.enterprise_id == batch.enterprise_id
        if kind == "stores"
        else model.store_id == batch.store_id
    )
    existing = db.scalars(select(model).where(condition).order_by(model.id).with_for_update()).all()

    def key(value):
        if kind in {"stores", "products", "competitors"}:
            return value.code
        if kind == "store":
            return value.business_date
        if kind == "product":
            return int(value.product_id), value.business_date
        return value.channel, value.business_date

    targets = {key(item): item for item in existing}
    skus = (
        {
            s.code: s
            for s in db.scalars(
                select(Sku).where(Sku.store_id == batch.store_id).order_by(Sku.id).with_for_update()
            )
        }
        if kind == "products"
        else {}
    )
    groups = defaultdict(list)
    for row in rows:
        value = schema.model_validate(row.payload)
        groups[key(value)].append((row, value))
    for business_key, group in groups.items():
        row, value = group[0]
        target = targets.get(business_key)
        data = value.model_dump(exclude={"skus"} if kind == "products" else set())
        # ID 仅在接口中用字符串传输，模型中保持 BIGINT。
        for name in ("store_id", "product_id"):
            if data.get(name) is not None:
                data[name] = int(data[name])
        if kind == "stores":
            data["enterprise_id"] = batch.enterprise_id
        before = [r.before for r, _ in group] if target else None
        if target:
            for name, item in data.items():
                setattr(target, name, item)
            target.version += 1
        else:
            target = model(created_by=batch.creator_id, **data)
            db.add(target)
        if kind in {"store", "product", "ad"}:
            target.batch_id = batch.id
        db.flush()
        if kind == "products":
            for _, product in group:
                item = product.skus[0]
                sku = skus.get(item.code)
                if sku:
                    sku.spec_name, sku.price, sku.cost = item.spec_name, item.price, item.cost
                    sku.version += 1
                else:
                    db.add(
                        Sku(
                            store_id=batch.store_id,
                            product_id=target.id,
                            created_by=batch.creator_id,
                            **item.model_dump(exclude={"id"}),
                        )
                    )
        event(
            db,
            batch,
            owner,
            "IMPORT_RECORD_WRITTEN",
            target,
            {
                "batch_id": str(batch.id),
                "before": before,
                "after": [r.payload for r, _ in group],
            },
        )
    db.flush()


def process_next_confirmation(db, settings):
    """抢占确认批次；持有批次锁直到成功提交，崩溃则整批回滚，租约到期后可恢复。"""
    now = utcnow()
    batch = db.scalar(
        select(ImportBatch)
        .where(
            ImportBatch.status == "IMPORTING",
            or_(ImportBatch.lease_until.is_(None), ImportBatch.lease_until < now),
        )
        .order_by(ImportBatch.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if not batch:
        db.rollback()
        return False
    owner = secrets.token_hex(16)
    batch.lease_owner, batch.lease_until = owner, now + timedelta(minutes=5)
    batch_id = batch.id
    db.commit()
    try:
        creator = db.get(User, batch.creator_id)
        if not creator or creator.status != "ACTIVE" or creator.must_change_password:
            raise AppError(403, "AUTH_FORBIDDEN", "上传账号当前不可用，请重新上传")
        if creator.enterprise_id != batch.enterprise_id:
            raise AppError(403, "AUTH_FORBIDDEN", "账号企业范围已变化")
        store = lock_scope(db, batch, Principal(creator, None, ""))
        batch = db.scalar(
            select(ImportBatch)
            .where(ImportBatch.id == batch_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if batch.status != "IMPORTING" or batch.lease_owner != owner:
            db.rollback()
            return True
        rows = revalidate(db, batch, store, settings)
        if batch.overwrite_count and not batch.overwrite_confirmed:
            raise AppError(409, "IMPORT_OVERWRITE_REQUIRED", "覆盖已有记录需要明确确认")
        write_rows(db, batch, rows, owner)
        batch.status = "SUCCEEDED"
        batch.finished_at = utcnow()
        batch.lease_owner = batch.lease_until = None
        event(db, batch, owner, "IMPORT_SUCCEEDED", batch, {"row_count": batch.row_count})
        db.commit()
    except Exception as exc:
        db.rollback()
        batch = db.scalar(
            select(ImportBatch)
            .where(ImportBatch.id == batch_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if batch and batch.status == "IMPORTING" and batch.lease_owner == owner:
            batch.status = "FAILED"
            batch.finished_at = utcnow()
            batch.error_code = exc.code if isinstance(exc, AppError) else "IMPORT_EXECUTION_FAILED"
            batch.error_message = (
                exc.message
                if isinstance(exc, AppError)
                else "整批入库失败，未写入任何业务数据，请重新上传"
            )
            batch.lease_owner = batch.lease_until = None
            event(
                db,
                batch,
                owner,
                "IMPORT_FAILED",
                batch,
                {"code": batch.error_code},
                outcome="FAILED",
            )
            db.commit()
        else:
            db.rollback()
    return True
