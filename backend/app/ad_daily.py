"""人工投放日数据：渠道独立记录，归因金额未知时不推断、不替代。"""

from datetime import date

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import Field, model_validator
from sqlalchemy import func, select

from app.catalog import writable_store
from app.core.errors import AppError
from app.daily_data import Count, DailyIdentityInput, business_today
from app.dependencies import current_user, get_db, store_scope
from app.identity import audit, check_version, result, stamp
from app.models import AdDaily, Store
from app.schemas import Money, RequiredText

router = APIRouter(prefix="/api/v1/daily-data/ad", tags=["daily-data"])


class AdDailyInput(DailyIdentityInput):
    channel: RequiredText = Field(max_length=64)
    spend: Money
    impressions: Count
    clicks: Count
    attributed_amount: Money | None = None
    attribution_note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def attribution_basis(self):
        """归因金额为零同样属于已提供金额，必须填写口径；未知金额保留 null。"""
        self.attribution_note = (self.attribution_note or "").strip() or None
        if self.attributed_amount is not None and self.attribution_note is None:
            raise ValueError("提供归因成交金额时必须填写归因口径说明")
        return self


class AdDailyUpdate(AdDailyInput):
    overwrite: bool = Field(default=False, strict=True)


def ad_scope(p):
    return select(AdDaily).where(AdDaily.store_id.in_(store_scope(p).with_only_columns(Store.id)))


def ad_view(item):
    """只返回渠道事实及异常提示，不把多渠道归因相加冒充去重成交。"""
    return {
        "id": str(item.id),
        "batch_id": str(item.batch_id) if item.batch_id else None,
        "store_id": str(item.store_id),
        "channel": item.channel,
        "business_date": item.business_date.isoformat(),
        "spend": format(item.spend, ".2f"),
        "impressions": str(item.impressions),
        "clicks": str(item.clicks),
        "attributed_amount": format(item.attributed_amount, ".2f")
        if item.attributed_amount is not None
        else None,
        "attribution_note": item.attribution_note,
        "version": item.version,
        "updated_at": stamp(item.updated_at),
        "is_current_day": item.business_date == business_today(),
        "warnings": ["点击量大于曝光量，请核对数据口径"] if item.clicks > item.impressions else [],
    }


@router.get("")
def list_daily(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    store_id: int | None = Query(None, gt=0),
    channel: str = Query("", max_length=64),
    start_date: date | None = None,
    end_date: date | None = None,
    p=Depends(current_user),
    db=Depends(get_db),
):
    if start_date and end_date and start_date > end_date:
        raise AppError(422, "DATA_INVALID", "开始日期不能晚于结束日期")
    query = ad_scope(p)
    if store_id is not None:
        query = query.where(AdDaily.store_id == store_id)
    if channel.strip():
        query = query.where(AdDaily.channel == channel.strip())
    if start_date:
        query = query.where(AdDaily.business_date >= start_date)
    if end_date:
        query = query.where(AdDaily.business_date <= end_date)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.scalars(
        query.order_by(AdDaily.business_date.desc(), AdDaily.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return result(
        request,
        {"items": [ad_view(x) for x in rows], "total": total, "page": page, "page_size": page_size},
    )


@router.get("/{record_id}")
def detail(record_id: int, request: Request, p=Depends(current_user), db=Depends(get_db)):
    item = db.scalar(ad_scope(p).where(AdDaily.id == record_id))
    if not item:
        raise AppError(404, "RESOURCE_NOT_FOUND", "投放记录不存在")
    return result(request, ad_view(item))


@router.post("", status_code=201)
def create(body: AdDailyInput, request: Request, p=Depends(current_user), db=Depends(get_db)):
    store = writable_store(db, p, body.store_id)
    existing = db.scalar(
        select(AdDaily.id).where(
            AdDaily.store_id == store.id,
            AdDaily.channel == body.channel,
            AdDaily.business_date == body.business_date,
        )
    )
    if existing:
        raise AppError(409, "DATA_DUPLICATE_KEY", "同店同渠道同日已有记录，请查询并确认更新")
    item = AdDaily(store_id=store.id, created_by=p.user.id, **body.model_dump(exclude={"store_id"}))
    db.add(item)
    db.flush()
    view = ad_view(item)
    audit(db, request, p.user, "AD_DAILY_CREATED", item, changes={"after": view})
    db.commit()
    return result(request, view)


@router.patch("")
def update(
    body: AdDailyUpdate,
    request: Request,
    if_match: str | None = Header(None),
    p=Depends(current_user),
    db=Depends(get_db),
):
    # 店铺行锁与归档共用，业务键不变；覆盖数据和前后值审计同事务提交。
    store = writable_store(db, p, body.store_id)
    item = db.scalar(
        select(AdDaily)
        .where(
            AdDaily.store_id == store.id,
            AdDaily.channel == body.channel,
            AdDaily.business_date == body.business_date,
        )
        .with_for_update()
    )
    if not item:
        raise AppError(404, "RESOURCE_NOT_FOUND", "投放记录不存在，请刷新后重试")
    check_version(item, if_match)
    if not body.overwrite:
        raise AppError(409, "DATA_OVERWRITE_CONFIRMATION_REQUIRED", "请先确认覆盖已有投放数据")
    before = ad_view(item)
    for key in ("spend", "impressions", "clicks", "attributed_amount", "attribution_note"):
        setattr(item, key, getattr(body, key))
    item.version += 1
    db.flush()
    view = ad_view(item)
    audit(db, request, p.user, "AD_DAILY_UPDATED", item, changes={"before": before, "after": view})
    db.commit()
    return result(request, view)
