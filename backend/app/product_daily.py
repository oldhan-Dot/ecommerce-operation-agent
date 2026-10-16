"""商品日经营：独立于店铺汇总，严格校验本品归属及历史数据版本。"""

from datetime import date

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import Field
from sqlalchemy import func, select

from app.catalog import writable_store
from app.core.errors import AppError
from app.daily_data import StoreDailyInput, daily_view
from app.dependencies import current_user, get_db, store_scope
from app.identity import audit, check_version, result
from app.models import Product, ProductDaily, Store
from app.schemas import EntityId

router = APIRouter(prefix="/api/v1/daily-data/product", tags=["daily-data"])


class ProductDailyInput(StoreDailyInput):
    product_id: EntityId


class ProductDailyUpdate(ProductDailyInput):
    overwrite: bool = Field(default=False, strict=True)


def product_daily_scope(p):
    """联表一次取得商品名称和状态，归档后保留历史查询，不逐行额外查库。"""
    return (
        select(ProductDaily, Product.name, Product.code, Product.status)
        .join(
            Product,
            (ProductDaily.product_id == Product.id) & (ProductDaily.store_id == Product.store_id),
        )
        .where(ProductDaily.store_id.in_(store_scope(p).with_only_columns(Store.id)))
    )


def product_daily_view(item, name, code, status):
    return {
        **daily_view(item),
        "product_id": str(item.product_id),
        "product_name": name,
        "product_code": code,
        "product_status": status,
    }


def writable_product(db, p, body):
    """先锁授权店铺，再锁同店商品；与商品/店铺归档使用一致锁顺序。"""
    store = writable_store(db, p, body.store_id)
    product = db.scalar(
        select(Product)
        .where(Product.id == int(body.product_id), Product.store_id == store.id)
        .with_for_update()
    )
    if not product:
        raise AppError(422, "DATA_INVALID", "商品必须属于所选店铺")
    if product.status != "ACTIVE":
        raise AppError(409, "PRODUCT_ARCHIVED", "已归档商品的经营数据仅可查看")
    return product


@router.get("")
def list_daily(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    store_id: int | None = Query(None, gt=0),
    product_id: int | None = Query(None, gt=0),
    start_date: date | None = None,
    end_date: date | None = None,
    p=Depends(current_user),
    db=Depends(get_db),
):
    if start_date and end_date and start_date > end_date:
        raise AppError(422, "DATA_INVALID", "开始日期不能晚于结束日期")
    query = product_daily_scope(p)
    if store_id is not None:
        query = query.where(ProductDaily.store_id == store_id)
    if product_id is not None:
        query = query.where(ProductDaily.product_id == product_id)
    if start_date:
        query = query.where(ProductDaily.business_date >= start_date)
    if end_date:
        query = query.where(ProductDaily.business_date <= end_date)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.execute(
        query.order_by(ProductDaily.business_date.desc(), ProductDaily.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return result(
        request,
        {
            "items": [product_daily_view(*row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        },
    )


@router.get("/{record_id}")
def detail(record_id: int, request: Request, p=Depends(current_user), db=Depends(get_db)):
    row = db.execute(product_daily_scope(p).where(ProductDaily.id == record_id)).first()
    if not row:
        raise AppError(404, "RESOURCE_NOT_FOUND", "经营记录不存在")
    return result(request, product_daily_view(*row))


@router.post("", status_code=201)
def create(body: ProductDailyInput, request: Request, p=Depends(current_user), db=Depends(get_db)):
    product = writable_product(db, p, body)
    existing = db.scalar(
        select(ProductDaily.id).where(
            ProductDaily.product_id == product.id, ProductDaily.business_date == body.business_date
        )
    )
    if existing:
        raise AppError(409, "DATA_DUPLICATE_KEY", "同商品同日已有记录，请查询并确认更新")
    item = ProductDaily(
        store_id=product.store_id,
        product_id=product.id,
        created_by=p.user.id,
        **body.model_dump(exclude={"store_id", "product_id"}),
    )
    db.add(item)
    db.flush()
    view = product_daily_view(item, product.name, product.code, product.status)
    audit(db, request, p.user, "PRODUCT_DAILY_CREATED", item, changes={"after": view})
    db.commit()
    return result(request, view)


@router.patch("")
def update(
    body: ProductDailyUpdate,
    request: Request,
    if_match: str | None = Header(None),
    p=Depends(current_user),
    db=Depends(get_db),
):
    product = writable_product(db, p, body)
    item = db.scalar(
        select(ProductDaily)
        .where(
            ProductDaily.product_id == product.id, ProductDaily.business_date == body.business_date
        )
        .with_for_update()
    )
    if not item:
        raise AppError(404, "RESOURCE_NOT_FOUND", "经营记录不存在，请刷新后重试")
    check_version(item, if_match)
    if not body.overwrite:
        raise AppError(409, "DATA_OVERWRITE_CONFIRMATION_REQUIRED", "请先确认覆盖已有经营数据")
    before = product_daily_view(item, product.name, product.code, product.status)
    for key in ("paid_amount", "paid_orders", "visitors", "refund_amount"):
        setattr(item, key, getattr(body, key))
    item.version += 1
    db.flush()
    view = product_daily_view(item, product.name, product.code, product.status)
    audit(
        db,
        request,
        p.user,
        "PRODUCT_DAILY_UPDATED",
        item,
        changes={"before": before, "after": view},
    )
    db.commit()
    return result(request, view)
