"""店铺日经营手工维护：同业务键唯一，显式确认并使用版本条件覆盖。"""

import re
from datetime import date, datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BeforeValidator, Field, field_validator
from sqlalchemy import func, select

from app.catalog import writable_store
from app.core.errors import AppError
from app.dependencies import current_user, get_db, store_scope
from app.identity import audit, check_version, result, stamp
from app.models import Store, StoreDaily
from app.schemas import EntityId, Input, Money

router = APIRouter(prefix="/api/v1/daily-data/store", tags=["daily-data"])


def business_today():
    """首版固定上海业务日，不依赖服务器系统时区或本机日期。"""
    return datetime.now(timezone(timedelta(hours=8))).date()


def integer_count(value):
    """允许整数或十进制整数字符串，拒绝布尔、小数及指数写法。"""
    if type(value) is int or isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value):
        return value
    raise ValueError("计数字段必须为非负整数")


Count = Annotated[int, BeforeValidator(integer_count), Field(ge=0, le=2**63 - 1)]


class StoreDailyInput(Input):
    store_id: EntityId
    business_date: date
    paid_amount: Money
    paid_orders: Count
    visitors: Count
    refund_amount: Money

    @field_validator("business_date", mode="before")
    @classmethod
    def date_format(cls, value):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError("业务日期须使用 YYYY-MM-DD")
        return value

    @field_validator("business_date")
    @classmethod
    def completed_or_current_day(cls, value):
        if value > business_today():
            raise ValueError("不能录入未来业务日期")
        return value


class StoreDailyUpdate(StoreDailyInput):
    overwrite: bool = Field(default=False, strict=True)


def daily_scope(p):
    return select(StoreDaily).where(
        StoreDaily.store_id.in_(store_scope(p).with_only_columns(Store.id))
    )


def daily_view(item):
    """金额和计数均使用字符串，避免浏览器大整数丢失精度；缺日不生成记录。"""
    return {
        "id": str(item.id),
        "store_id": str(item.store_id),
        "business_date": item.business_date.isoformat(),
        "paid_amount": format(item.paid_amount, ".2f"),
        "paid_orders": str(item.paid_orders),
        "visitors": str(item.visitors),
        "refund_amount": format(item.refund_amount, ".2f"),
        "version": item.version,
        "updated_at": stamp(item.updated_at),
        "is_current_day": item.business_date == business_today(),
    }


@router.get("")
def list_daily(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    store_id: int | None = Query(None, gt=0),
    start_date: date | None = None,
    end_date: date | None = None,
    p=Depends(current_user),
    db=Depends(get_db),
):
    if start_date and end_date and start_date > end_date:
        raise AppError(422, "DATA_INVALID", "开始日期不能晚于结束日期")
    query = daily_scope(p)
    if store_id is not None:
        query = query.where(StoreDaily.store_id == store_id)
    if start_date:
        query = query.where(StoreDaily.business_date >= start_date)
    if end_date:
        query = query.where(StoreDaily.business_date <= end_date)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.scalars(
        query.order_by(StoreDaily.business_date.desc(), StoreDaily.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return result(
        request,
        {
            "items": [daily_view(x) for x in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        },
    )


@router.get("/{record_id}")
def detail(record_id: int, request: Request, p=Depends(current_user), db=Depends(get_db)):
    item = db.scalar(daily_scope(p).where(StoreDaily.id == record_id))
    if not item:
        raise AppError(404, "RESOURCE_NOT_FOUND", "经营记录不存在")
    return result(request, daily_view(item))


@router.post("", status_code=201)
def create(body: StoreDailyInput, request: Request, p=Depends(current_user), db=Depends(get_db)):
    store = writable_store(db, p, body.store_id)
    existing = db.scalar(
        select(StoreDaily.id).where(
            StoreDaily.store_id == store.id, StoreDaily.business_date == body.business_date
        )
    )
    if existing:
        raise AppError(409, "DATA_DUPLICATE_KEY", "同店同日已有记录，请查询并确认更新")
    item = StoreDaily(
        store_id=store.id, created_by=p.user.id, **body.model_dump(exclude={"store_id"})
    )
    db.add(item)
    db.flush()
    view = daily_view(item)
    audit(db, request, p.user, "STORE_DAILY_CREATED", item, changes={"after": view})
    db.commit()
    return result(request, view)


@router.patch("")
def update(
    body: StoreDailyUpdate,
    request: Request,
    if_match: str | None = Header(None),
    p=Depends(current_user),
    db=Depends(get_db),
):
    # 与店铺归档统一锁顺序；业务键不改变，更新只覆盖明确确认的四项指标。
    store = writable_store(db, p, body.store_id)
    item = db.scalar(
        select(StoreDaily)
        .where(StoreDaily.store_id == store.id, StoreDaily.business_date == body.business_date)
        .with_for_update()
    )
    if not item:
        raise AppError(404, "RESOURCE_NOT_FOUND", "经营记录不存在，请刷新后重试")
    check_version(item, if_match)
    if not body.overwrite:
        raise AppError(409, "DATA_OVERWRITE_CONFIRMATION_REQUIRED", "请先确认覆盖已有经营数据")
    before = daily_view(item)
    for key in ("paid_amount", "paid_orders", "visitors", "refund_amount"):
        setattr(item, key, getattr(body, key))
    item.version += 1
    db.flush()
    view = daily_view(item)
    audit(
        db, request, p.user, "STORE_DAILY_UPDATED", item, changes={"before": before, "after": view}
    )
    db.commit()
    return result(request, view)
