"""身份、店铺与审计 API；所有写操作和审计同事务提交。"""

import secrets
import threading
from collections import defaultdict, deque
from datetime import UTC, timedelta
from time import monotonic

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.security import DUMMY_HASH, csrf_token, digest, hasher, verify_password
from app.dependencies import (
    Principal,
    administrator,
    check_origin,
    current_user,
    get_db,
    store_scope,
)
from app.models import AuditEvent, AuthSession, Enterprise, Store, StoreGrant, User, utcnow
from app.schemas import (
    ChangePassword,
    CreateUser,
    Login,
    ResetPassword,
    StoreInput,
    UpdateGrants,
    UpdateUser,
)

router = APIRouter(prefix="/api/v1")


def result(request: Request, data):
    return {"data": data, "request_id": request.state.request_id}


def stamp(value):
    return value.replace(tzinfo=UTC).isoformat() if value else None


def audit(db: Session, request: Request, user: User, action: str, target, *, changes=None):
    """只记录字段白名单，不记录任何密码、哈希、Cookie 或完整请求体。"""
    db.add(
        AuditEvent(
            enterprise_id=user.enterprise_id,
            actor_id=user.id,
            action=action,
            target_type=target.__tablename__,
            target_id=str(target.id),
            store_id=target.id if isinstance(target, Store) else getattr(target, "store_id", None),
            request_id=request.state.request_id,
            result="SUCCEEDED",
            redacted_diff=changes or {},
        )
    )


def user_view(db: Session, user: User):
    if user.role == "OPERATOR":
        ids = db.scalars(select(StoreGrant.store_id).where(StoreGrant.user_id == user.id)).all()
    else:
        ids = db.scalars(select(Store.id).where(Store.enterprise_id == user.enterprise_id)).all()
    return {
        "id": str(user.id),
        "login_name": user.login_name,
        "role": user.role,
        "status": user.status,
        "version": user.version,
        "store_ids": [str(x) for x in ids],
        "must_change_password": user.must_change_password,
    }


def session_view(request: Request, db: Session, user: User, auth: AuthSession, token: str):
    return result(
        request,
        {
            "user": user_view(db, user),
            "csrf_token": csrf_token(
                token, request.app.state.settings.session_pepper.get_secret_value()
            ),
            "expires_at": stamp(auth.expires_at),
        },
    )


def check_version(target, supplied: str | None):
    if supplied is None:
        raise AppError(428, "PRECONDITION_REQUIRED", "更新时需要 If-Match 版本条件")
    if supplied != f'"{target.version}"':
        raise AppError(409, "DATA_VERSION_CONFLICT", "记录已被修改，请刷新后重试")


def revoke_sessions(db: Session, user_id: int):
    db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )


def managed_user(db: Session, actor: User, user_id: int):
    # 企业行串行化账号管理，防止两个管理员同时停用对方导致企业没有管理员。
    db.scalar(select(Enterprise).where(Enterprise.id == actor.enterprise_id).with_for_update())
    target = db.scalar(
        select(User)
        .where(User.id == user_id, User.enterprise_id == actor.enterprise_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not target:
        raise AppError(404, "RESOURCE_NOT_FOUND", "账号不存在")
    return target


@router.post("/auth/login", tags=["auth"])
def login(body: Login, request: Request, response: Response, db: Session = Depends(get_db)):
    check_origin(request)
    # 进程内来源限流防止不存在账号的高频尝试；账号锁定仍由数据库跨进程保证。
    address = request.client.host if request.client else "unknown"
    with request.app.state.login_limit_lock:
        buckets = request.app.state.login_limits
        now_clock = monotonic()
        for key in list(buckets):
            while buckets[key] and buckets[key][0] <= now_clock - 60:
                buckets[key].popleft()
            if not buckets[key]:
                del buckets[key]
        if len(buckets[address]) >= 20:
            raise AppError(429, "AUTH_RATE_LIMITED", "登录尝试过于频繁，请稍后重试")
        buckets[address].append(now_clock)
    pepper = request.app.state.settings.session_pepper.get_secret_value()
    digest("configuration-check", pepper)
    user = db.scalar(select(User).where(User.login_name == body.login_name).with_for_update())
    now = utcnow()
    if user and user.status == "ACTIVE" and user.locked_until and user.locked_until > now:
        raise AppError(423, "AUTH_LOCKED", "账号已锁定15分钟，请稍后重试")
    valid = verify_password(body.password, user.password_hash if user else DUMMY_HASH)
    if not user or user.status != "ACTIVE" or not valid:
        if user:
            if user.locked_until and user.locked_until <= now:
                user.failed_count = 0
            user.failed_count += 1
            if user.failed_count >= 5:
                user.locked_until = now + timedelta(minutes=15)
            db.add(
                AuditEvent(
                    enterprise_id=user.enterprise_id,
                    actor_id=user.id,
                    action="LOGIN_FAILED",
                    target_type="users",
                    target_id=str(user.id),
                    request_id=request.state.request_id,
                    result="FAILED",
                    redacted_diff={},
                )
            )
            db.commit()  # 失败计数必须提交，不能随401响应一起回滚。
        raise AppError(401, "AUTH_INVALID_CREDENTIALS", "账号或密码不正确")
    user.failed_count = 0
    user.locked_until = None
    token = secrets.token_urlsafe(32)
    csrf = csrf_token(token, pepper)
    auth = AuthSession(
        user_id=user.id,
        token_digest=digest(token, pepper),
        csrf_digest=digest(csrf, pepper),
        password_version=user.password_version,
        expires_at=now + timedelta(hours=8),
        last_seen_at=now,
        created_by=user.id,
    )
    db.add(auth)
    audit(db, request, user, "LOGIN", user)
    db.commit()
    response.set_cookie(
        "ea_session",
        token,
        max_age=8 * 3600,
        httponly=True,
        samesite="lax",
        path="/",
        secure=request.app.state.settings.app_env in {"staging", "prod"},
    )
    return session_view(request, db, user, auth, token)


@router.get("/auth/session", tags=["auth"])
def read_session(request: Request, p: Principal = Depends(current_user), db=Depends(get_db)):
    return session_view(request, db, p.user, p.session, p.token)


@router.post("/auth/logout", status_code=204, tags=["auth"])
def logout(request: Request, response: Response, p=Depends(current_user), db=Depends(get_db)):
    p.session.revoked_at = utcnow()
    audit(db, request, p.user, "LOGOUT", p.user)
    db.commit()
    response.delete_cookie("ea_session", path="/")


@router.post("/auth/change-password", status_code=204, tags=["auth"])
def change_password(
    body: ChangePassword,
    request: Request,
    response: Response,
    p=Depends(current_user),
    db=Depends(get_db),
):
    user = db.scalar(
        select(User)
        .where(User.id == p.user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not verify_password(body.old_password, user.password_hash):
        raise AppError(400, "AUTH_INVALID_CREDENTIALS", "旧密码不正确")
    if body.old_password == body.new_password:
        raise AppError(422, "DATA_INVALID", "新密码不能与旧密码相同")
    user.password_hash = hasher.hash(body.new_password)
    user.password_version += 1
    user.must_change_password = False
    user.failed_count = 0
    user.locked_until = None
    user.version += 1
    revoke_sessions(db, user.id)
    audit(db, request, user, "PASSWORD_CHANGED", user)
    db.commit()
    response.delete_cookie("ea_session", path="/")


@router.get("/users", tags=["users"])
def list_users(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    p=Depends(administrator),
    db=Depends(get_db),
):
    query = select(User).where(User.enterprise_id == p.user.enterprise_id)
    count = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(
        query.order_by(User.id.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return result(
        request,
        {
            "items": [user_view(db, u) for u in items],
            "total": count,
            "page": page,
            "page_size": page_size,
        },
    )


@router.post("/users", status_code=201, tags=["users"])
def create_user(body: CreateUser, request: Request, p=Depends(administrator), db=Depends(get_db)):
    user = User(
        enterprise_id=p.user.enterprise_id,
        created_by=p.user.id,
        login_name=body.login_name,
        role=body.role,
        password_hash=hasher.hash(body.password),
    )
    db.add(user)
    db.flush()
    audit(db, request, p.user, "USER_CREATED", user, changes={"role": user.role})
    db.commit()
    return result(request, user_view(db, user))


@router.patch("/users/{user_id}", tags=["users"])
def update_user(
    user_id: int,
    body: UpdateUser,
    request: Request,
    if_match: str | None = Header(None),
    p=Depends(administrator),
    db=Depends(get_db),
):
    target = managed_user(db, p.user, user_id)
    check_version(target, if_match)
    before = {"role": target.role, "status": target.status}
    new_role, new_status = body.role or target.role, body.status or target.status
    if (
        target.role == "ADMIN"
        and target.status == "ACTIVE"
        and (new_role != "ADMIN" or new_status != "ACTIVE")
    ):
        admins = db.scalar(
            select(func.count())
            .select_from(User)
            .where(
                User.enterprise_id == target.enterprise_id,
                User.role == "ADMIN",
                User.status == "ACTIVE",
            )
        )
        if admins <= 1:
            raise AppError(409, "AUTH_LAST_ADMIN", "不能停用或降级最后一个有效管理员")
    target.role, target.status = new_role, new_status
    target.version += 1
    if before != {"role": new_role, "status": new_status}:
        revoke_sessions(db, target.id)
    audit(
        db,
        request,
        p.user,
        "USER_UPDATED",
        target,
        changes={"before": before, "after": {"role": new_role, "status": new_status}},
    )
    db.commit()
    return result(request, user_view(db, target))


@router.post("/users/{user_id}/password-reset", status_code=204, tags=["users"])
def reset_password(
    user_id: int,
    body: ResetPassword,
    request: Request,
    if_match: str | None = Header(None),
    p=Depends(administrator),
    db=Depends(get_db),
):
    target = managed_user(db, p.user, user_id)
    check_version(target, if_match)
    target.password_hash = hasher.hash(body.new_password)
    target.password_version += 1
    target.must_change_password = True
    target.failed_count = 0
    target.locked_until = None
    target.version += 1
    revoke_sessions(db, target.id)
    audit(db, request, p.user, "PASSWORD_RESET", target)
    db.commit()


@router.put("/users/{user_id}/store-grants", tags=["users"])
def update_grants(
    user_id: int,
    body: UpdateGrants,
    request: Request,
    if_match: str | None = Header(None),
    p=Depends(administrator),
    db=Depends(get_db),
):
    target = managed_user(db, p.user, user_id)
    check_version(target, if_match)
    if target.role != "OPERATOR":
        raise AppError(422, "DATA_INVALID", "管理员和主管按企业范围访问，无需店铺授权")
    ids = [int(x) for x in body.store_ids]
    available = list(
        db.scalars(
            select(Store.id).where(Store.enterprise_id == p.user.enterprise_id, Store.id.in_(ids))
        )
    )
    if set(available) != set(ids):
        raise AppError(404, "RESOURCE_NOT_FOUND", "店铺不存在或不可授权")
    before = list(db.scalars(select(StoreGrant.store_id).where(StoreGrant.user_id == target.id)))
    db.execute(delete(StoreGrant).where(StoreGrant.user_id == target.id))
    db.add_all(
        [
            StoreGrant(user_id=target.id, store_id=x, granted_by=p.user.id, created_by=p.user.id)
            for x in ids
        ]
    )
    target.version += 1
    audit(
        db,
        request,
        p.user,
        "STORE_GRANTS_UPDATED",
        target,
        changes={"before": [str(x) for x in before], "after": body.store_ids},
    )
    db.commit()
    return result(request, user_view(db, target))


def store_view(store: Store):
    return {
        "id": str(store.id),
        "code": store.code,
        "name": store.name,
        "platform": store.platform,
        "external_id": store.external_id,
        "category": store.category,
        "notes": store.notes,
        "status": store.status,
        "version": store.version,
        "updated_at": stamp(store.updated_at),
    }


@router.get("/stores", tags=["stores"])
def list_stores(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    keyword: str = Query("", max_length=100),
    p=Depends(current_user),
    db=Depends(get_db),
):
    query = store_scope(p)
    if keyword:
        query = query.where(
            Store.name.contains(keyword, autoescape=True)
            | Store.code.contains(keyword, autoescape=True)
        )
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(
        query.order_by(Store.id.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return result(
        request,
        {
            "items": [store_view(s) for s in items],
            "total": total,
            "page": page,
            "page_size": page_size,
        },
    )


@router.get("/stores/{store_id}", tags=["stores"])
def store_detail(store_id: int, request: Request, p=Depends(current_user), db=Depends(get_db)):
    store = db.scalar(store_scope(p).where(Store.id == store_id))
    if not store:
        raise AppError(404, "RESOURCE_NOT_FOUND", "店铺不存在")
    return result(request, store_view(store))


@router.post("/stores", status_code=201, tags=["stores"])
def create_store(body: StoreInput, request: Request, p=Depends(administrator), db=Depends(get_db)):
    store = Store(**body.model_dump(), enterprise_id=p.user.enterprise_id, created_by=p.user.id)
    db.add(store)
    db.flush()
    audit(db, request, p.user, "STORE_CREATED", store, changes=body.model_dump())
    db.commit()
    return result(request, store_view(store))


@router.patch("/stores/{store_id}", tags=["stores"])
def edit_store(
    store_id: int,
    body: StoreInput,
    request: Request,
    if_match: str | None = Header(None),
    p=Depends(administrator),
    db=Depends(get_db),
):
    store = db.scalar(store_scope(p).where(Store.id == store_id).with_for_update())
    if not store:
        raise AppError(404, "RESOURCE_NOT_FOUND", "店铺不存在")
    check_version(store, if_match)
    if store.status != "ACTIVE":
        raise AppError(409, "STORE_ARCHIVED", "已归档店铺仅可查看历史")
    # 首版直接固定编码，避免后续经营事实引用后仍允许更换业务键。
    if body.code != store.code:
        raise AppError(409, "STORE_CODE_IMMUTABLE", "店铺编码不可修改")
    before = store_view(store)
    for key, value in body.model_dump().items():
        setattr(store, key, value)
    store.version += 1
    audit(
        db,
        request,
        p.user,
        "STORE_UPDATED",
        store,
        changes={"before": before, "after": body.model_dump()},
    )
    db.commit()
    return result(request, store_view(store))


@router.post("/stores/{store_id}/archive", tags=["stores"])
def archive_store(
    store_id: int,
    request: Request,
    if_match: str | None = Header(None),
    p=Depends(administrator),
    db=Depends(get_db),
):
    store = db.scalar(store_scope(p).where(Store.id == store_id).with_for_update())
    if not store:
        raise AppError(404, "RESOURCE_NOT_FOUND", "店铺不存在")
    check_version(store, if_match)
    if store.status != "ACTIVE":
        raise AppError(409, "STORE_ARCHIVED", "店铺已经归档")
    store.status = "ARCHIVED"
    store.version += 1
    audit(db, request, p.user, "STORE_ARCHIVED", store)
    db.commit()
    return result(request, store_view(store))


@router.get("/audit-events", tags=["audit"])
def list_audits(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    request_id: str | None = Query(None, max_length=64),
    action: str | None = Query(None, max_length=64),
    target_type: str | None = Query(None, max_length=64),
    target_id: str | None = Query(None, max_length=64),
    p=Depends(administrator),
    db=Depends(get_db),
):
    query = select(AuditEvent).where(AuditEvent.enterprise_id == p.user.enterprise_id)
    for column, value in (
        (AuditEvent.request_id, request_id),
        (AuditEvent.action, action),
        (AuditEvent.target_type, target_type),
        (AuditEvent.target_id, target_id),
    ):
        if value:
            query = query.where(column == value)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.scalars(
        query.order_by(AuditEvent.id.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return result(
        request,
        {
            "items": [
                {
                    "id": str(x.id),
                    "actor_id": str(x.actor_id) if x.actor_id else None,
                    "action": x.action,
                    "target_type": x.target_type,
                    "target_id": x.target_id,
                    "result": x.result,
                    "changes": x.redacted_diff,
                    "created_at": stamp(x.created_at),
                    "request_id": x.request_id,
                }
                for x in rows
            ],
            "total": total,
            "page": page,
            "page_size": page_size,
        },
    )


def init_rate_limit(app):
    app.state.login_limits = defaultdict(deque)
    app.state.login_limit_lock = threading.Lock()
