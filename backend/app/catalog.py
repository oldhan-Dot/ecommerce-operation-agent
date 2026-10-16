"""商品与 SKU 原子维护；按店铺授权过滤，不提供硬删除入口。"""

from fastapi import APIRouter, Depends, Header, Query, Request
from sqlalchemy import func, select

from app.core.errors import AppError
from app.dependencies import current_user, get_db, store_scope
from app.identity import audit, check_version, result, stamp
from app.models import Product, Sku, Store
from app.schemas import ProductInput

router = APIRouter(prefix="/api/v1", tags=["products"])


def product_scope(p):
    return select(Product).where(Product.store_id.in_(store_scope(p).with_only_columns(Store.id)))


def product_view(db, product):
    """金额以十进制字符串输出，前端不使用二进制浮点进行业务计算。"""
    return {
        **{
            key: getattr(product, key)
            for key in (
                "code",
                "name",
                "category",
                "selling_points",
                "brand",
                "audience",
                "source_url",
                "status",
                "version",
            )
        },
        "id": str(product.id),
        "store_id": str(product.store_id),
        "updated_at": stamp(product.updated_at),
        "skus": [
            {
                "id": str(s.id),
                "code": s.code,
                "spec_name": s.spec_name,
                "price": format(s.price, ".2f"),
                "cost": format(s.cost, ".2f") if s.cost is not None else None,
            }
            for s in db.scalars(select(Sku).where(Sku.product_id == product.id).order_by(Sku.id))
        ],
    }


def writable_store(db, p, store_id):
    """与店铺归档共用行锁，避免归档和新建/修改商品交错提交。"""
    store = db.scalar(store_scope(p).where(Store.id == int(store_id)).with_for_update())
    if not store:
        raise AppError(404, "RESOURCE_NOT_FOUND", "店铺不存在")
    if store.status != "ACTIVE":
        raise AppError(409, "STORE_ARCHIVED", "已归档店铺仅可查看历史")
    return store


@router.get("/products")
def list_products(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    store_id: int | None = Query(None, gt=0),
    keyword: str = Query("", max_length=200),
    status: str | None = Query(None, pattern="^(ACTIVE|ARCHIVED)$"),
    p=Depends(current_user),
    db=Depends(get_db),
):
    query = product_scope(p)
    if store_id is not None:
        query = query.where(Product.store_id == store_id)
    if status:
        query = query.where(Product.status == status)
    if keyword:
        query = query.where(
            Product.name.contains(keyword, autoescape=True)
            | Product.code.contains(keyword, autoescape=True)
        )
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.scalars(
        query.order_by(Product.id.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return result(
        request,
        {
            "items": [product_view(db, x) for x in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        },
    )


@router.get("/products/{product_id}")
def product_detail(product_id: int, request: Request, p=Depends(current_user), db=Depends(get_db)):
    product = db.scalar(product_scope(p).where(Product.id == product_id))
    if not product:
        raise AppError(404, "RESOURCE_NOT_FOUND", "商品不存在")
    return result(request, product_view(db, product))


def save_skus(db, product, inputs, user_id):
    """SKU 以稳定 ID 更新；禁止删除、换编码或跨商品挪用，保留将来历史引用。"""
    existing = {
        str(s.id): s
        for s in db.scalars(select(Sku).where(Sku.product_id == product.id).with_for_update())
    }
    supplied = {s.id for s in inputs if s.id is not None}
    if supplied != set(existing):
        raise AppError(422, "DATA_INVALID", "需保留全部已有 SKU，且不得引用其他商品的 SKU")
    for item in inputs:
        if item.id:
            sku = existing[item.id]
            if item.code != sku.code:
                raise AppError(409, "SKU_CODE_IMMUTABLE", "已有 SKU 编码不可修改")
            sku.spec_name, sku.price, sku.cost = item.spec_name, item.price, item.cost
            sku.version += 1
        else:
            db.add(
                Sku(
                    product_id=product.id,
                    store_id=product.store_id,
                    created_by=user_id,
                    **item.model_dump(exclude={"id"}),
                )
            )
    db.flush()


@router.post("/products", status_code=201)
def create_product(
    body: ProductInput, request: Request, p=Depends(current_user), db=Depends(get_db)
):
    store = writable_store(db, p, body.store_id)
    product = Product(
        store_id=store.id, created_by=p.user.id, **body.model_dump(exclude={"store_id", "skus"})
    )
    db.add(product)
    db.flush()
    save_skus(db, product, body.skus, p.user.id)
    view = product_view(db, product)
    audit(db, request, p.user, "PRODUCT_CREATED", product, changes={"after": view})
    db.commit()
    return result(request, view)


def locked_product(db, p, product_id):
    # 先店铺后商品，统一锁顺序；店铺 ID 只从授权查询结果读取。
    product = db.scalar(product_scope(p).where(Product.id == product_id))
    if not product:
        raise AppError(404, "RESOURCE_NOT_FOUND", "商品不存在")
    writable_store(db, p, product.store_id)
    return db.scalar(
        select(Product)
        .where(Product.id == product_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


@router.patch("/products/{product_id}")
def edit_product(
    product_id: int,
    body: ProductInput,
    request: Request,
    if_match: str | None = Header(None),
    p=Depends(current_user),
    db=Depends(get_db),
):
    product = locked_product(db, p, product_id)
    check_version(product, if_match)
    if product.status != "ACTIVE":
        raise AppError(409, "PRODUCT_ARCHIVED", "已归档商品仅可查看历史")
    if int(body.store_id) != product.store_id or body.code != product.code:
        raise AppError(409, "PRODUCT_IDENTITY_IMMUTABLE", "所属店铺和商品编码不可修改")
    before = product_view(db, product)
    for key, value in body.model_dump(exclude={"store_id", "code", "skus"}).items():
        setattr(product, key, value)
    save_skus(db, product, body.skus, p.user.id)
    product.version += 1
    db.flush()
    view = product_view(db, product)
    audit(
        db, request, p.user, "PRODUCT_UPDATED", product, changes={"before": before, "after": view}
    )
    db.commit()
    return result(request, view)


@router.post("/products/{product_id}/archive")
def archive_product(
    product_id: int,
    request: Request,
    if_match: str | None = Header(None),
    p=Depends(current_user),
    db=Depends(get_db),
):
    product = locked_product(db, p, product_id)
    check_version(product, if_match)
    if product.status != "ACTIVE":
        raise AppError(409, "PRODUCT_ARCHIVED", "商品已经归档")
    before = product_view(db, product)
    product.status = "ARCHIVED"
    product.version += 1
    db.flush()
    view = product_view(db, product)
    audit(
        db, request, p.user, "PRODUCT_ARCHIVED", product, changes={"before": before, "after": view}
    )
    db.commit()
    return result(request, view)
