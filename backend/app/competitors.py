"""人工竞品资料 API：保留来源与时点，只存储文本，不联网采集。"""

from fastapi import APIRouter, Depends, Header, Query, Request
from sqlalchemy import func, select

from app.catalog import writable_store
from app.core.errors import AppError
from app.dependencies import current_user, get_db, store_scope
from app.identity import audit, check_version, result, stamp
from app.models import Competitor, Product, Store
from app.schemas import CompetitorInput

router = APIRouter(prefix="/api/v1/competitors", tags=["competitors"])


def competitor_scope(p):
    """列表、详情和写入共用授权范围，隐藏其他店铺及企业的记录。"""
    return select(Competitor).where(
        Competitor.store_id.in_(store_scope(p).with_only_columns(Store.id))
    )


def competitor_view(db, item):
    product = db.get(Product, item.product_id) if item.product_id is not None else None
    return {
        **{
            key: getattr(item, key)
            for key in (
                "code",
                "name",
                "platform",
                "brand",
                "spec",
                "selling_points",
                "excerpt",
                "source_note",
                "source_url",
                "status",
                "version",
            )
        },
        "id": str(item.id),
        "store_id": str(item.store_id),
        "product_id": str(item.product_id) if item.product_id is not None else None,
        "product_name": product.name if product else None,
        "price": format(item.price, ".2f") if item.price is not None else None,
        "collected_at": item.collected_at.isoformat(),
        "updated_at": stamp(item.updated_at),
    }


def validate_product(db, body, previous_id=None):
    """只能关联同店本品；已归档本品可保留原关联，不能成为新关联。"""
    if body.product_id is None:
        return
    product = db.scalar(
        select(Product).where(
            Product.id == int(body.product_id), Product.store_id == int(body.store_id)
        )
    )
    if not product:
        raise AppError(422, "DATA_INVALID", "关联本品必须属于当前店铺")
    if product.status != "ACTIVE" and product.id != previous_id:
        raise AppError(409, "PRODUCT_ARCHIVED", "不能新关联已归档商品")


def attributes(body):
    values = body.model_dump(exclude={"store_id"})
    values["product_id"] = int(body.product_id) if body.product_id is not None else None
    return values


@router.get("")
def list_competitors(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    store_id: int | None = Query(None, gt=0),
    keyword: str = Query("", max_length=200),
    status: str | None = Query(None, pattern="^(ACTIVE|ARCHIVED)$"),
    p=Depends(current_user),
    db=Depends(get_db),
):
    query = competitor_scope(p)
    if store_id is not None:
        query = query.where(Competitor.store_id == store_id)
    if status:
        query = query.where(Competitor.status == status)
    if keyword:
        query = query.where(
            Competitor.name.contains(keyword, autoescape=True)
            | Competitor.code.contains(keyword, autoescape=True)
        )
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.scalars(
        query.order_by(Competitor.collected_at.desc(), Competitor.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return result(
        request,
        {
            "items": [competitor_view(db, x) for x in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        },
    )


@router.get("/{competitor_id}")
def detail(competitor_id: int, request: Request, p=Depends(current_user), db=Depends(get_db)):
    item = db.scalar(competitor_scope(p).where(Competitor.id == competitor_id))
    if not item:
        raise AppError(404, "RESOURCE_NOT_FOUND", "竞品不存在")
    return result(request, competitor_view(db, item))


@router.post("", status_code=201)
def create(body: CompetitorInput, request: Request, p=Depends(current_user), db=Depends(get_db)):
    store = writable_store(db, p, body.store_id)
    validate_product(db, body)
    item = Competitor(store_id=store.id, created_by=p.user.id, **attributes(body))
    db.add(item)
    db.flush()
    view = competitor_view(db, item)
    audit(db, request, p.user, "COMPETITOR_CREATED", item, changes={"after": view})
    db.commit()
    return result(request, view)


def locked_competitor(db, p, competitor_id):
    """与商品/店铺归档遵循先店铺后资料的锁顺序，避免归档后仍新建关联。"""
    item = db.scalar(competitor_scope(p).where(Competitor.id == competitor_id))
    if not item:
        raise AppError(404, "RESOURCE_NOT_FOUND", "竞品不存在")
    writable_store(db, p, item.store_id)
    return db.scalar(
        select(Competitor)
        .where(Competitor.id == competitor_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


@router.patch("/{competitor_id}")
def edit(
    competitor_id: int,
    body: CompetitorInput,
    request: Request,
    if_match: str | None = Header(None),
    p=Depends(current_user),
    db=Depends(get_db),
):
    item = locked_competitor(db, p, competitor_id)
    check_version(item, if_match)
    if item.status != "ACTIVE":
        raise AppError(409, "COMPETITOR_ARCHIVED", "已归档竞品仅可查看历史")
    if int(body.store_id) != item.store_id or body.code != item.code:
        raise AppError(409, "COMPETITOR_IDENTITY_IMMUTABLE", "所属店铺和竞品编码不可修改")
    validate_product(db, body, item.product_id)
    before = competitor_view(db, item)
    for key, value in attributes(body).items():
        setattr(item, key, value)
    item.version += 1
    db.flush()
    view = competitor_view(db, item)
    audit(
        db, request, p.user, "COMPETITOR_UPDATED", item, changes={"before": before, "after": view}
    )
    db.commit()
    return result(request, view)


@router.post("/{competitor_id}/archive")
def archive(
    competitor_id: int,
    request: Request,
    if_match: str | None = Header(None),
    p=Depends(current_user),
    db=Depends(get_db),
):
    item = locked_competitor(db, p, competitor_id)
    check_version(item, if_match)
    if item.status != "ACTIVE":
        raise AppError(409, "COMPETITOR_ARCHIVED", "竞品已经归档")
    before = competitor_view(db, item)
    item.status = "ARCHIVED"
    item.version += 1
    db.flush()
    view = competitor_view(db, item)
    audit(
        db, request, p.user, "COMPETITOR_ARCHIVED", item, changes={"before": before, "after": view}
    )
    db.commit()
    return result(request, view)
