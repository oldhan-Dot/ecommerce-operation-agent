"""请求级数据库会话与实时授权，权限不依赖浏览器传回的角色。"""

import hmac
from dataclasses import dataclass
from datetime import timedelta

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.security import digest
from app.models import AuthSession, Store, StoreGrant, User, utcnow


def get_db(request: Request):
    """每请求独立 Session，异常由关闭会话回滚，写操作显式提交。"""
    engine = request.app.state.database.engine
    if engine is None:
        request.app.state.database.check(require_migration=False)
        engine = request.app.state.database.engine
    if engine is None:
        raise AppError(503, "SYS_DB_UNAVAILABLE", "数据库暂不可用")
    with Session(engine, expire_on_commit=False) as db:
        yield db


def check_origin(request: Request) -> None:
    """所有写请求（含登录）检查精确来源，拒绝跨站和缺失来源的浏览器写入。"""
    if request.headers.get("origin") not in request.app.state.settings.trusted_origins:
        raise AppError(403, "AUTH_CSRF_INVALID", "请求来源校验失败")


@dataclass
class Principal:
    user: User
    session: AuthSession
    token: str


def current_user(request: Request, db: Session = Depends(get_db)) -> Principal:
    """会话、账号和店铺权限每次查库；密码重置、停用、过期均即时失效。"""
    token = request.cookies.get("ea_session", "")
    if not token or len(token) > 128:
        raise AppError(401, "AUTH_SESSION_EXPIRED", "请先登录")
    pepper = request.app.state.settings.session_pepper.get_secret_value()
    auth = db.scalar(select(AuthSession).where(AuthSession.token_digest == digest(token, pepper)))
    now = utcnow()
    user = db.get(User, auth.user_id) if auth else None
    if (
        not auth
        or not user
        or user.status != "ACTIVE"
        or auth.revoked_at is not None
        or auth.expires_at <= now
        or auth.last_seen_at <= now - timedelta(minutes=30)
        or auth.password_version != user.password_version
    ):
        raise AppError(401, "AUTH_SESSION_EXPIRED", "登录已失效，请重新登录")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        check_origin(request)
        csrf = request.headers.get("x-csrf-token", "")
        if len(csrf) > 128 or not hmac.compare_digest(auth.csrf_digest, digest(csrf, pepper)):
            raise AppError(403, "AUTH_CSRF_INVALID", "安全令牌已失效，请刷新后重试")
    if user.must_change_password and request.url.path not in {
        "/api/v1/auth/session",
        "/api/v1/auth/change-password",
        "/api/v1/auth/logout",
    }:
        raise AppError(403, "AUTH_PASSWORD_CHANGE_REQUIRED", "请先修改临时密码")
    auth.last_seen_at = now
    # 活动时间单独持久化；后续失败的业务不会回滚已经通过校验的登录心跳。
    db.commit()
    return Principal(user, auth, token)


def administrator(principal: Principal = Depends(current_user)) -> Principal:
    if principal.user.role != "ADMIN":
        raise AppError(403, "AUTH_FORBIDDEN", "仅管理员可执行此操作")
    return principal


def store_scope(principal: Principal):
    """同时适用于列表与详情；运营人员仅可访问实时授权的本企业店铺。"""
    query = select(Store).where(Store.enterprise_id == principal.user.enterprise_id)
    if principal.user.role == "OPERATOR":
        query = query.where(
            Store.id.in_(select(StoreGrant.store_id).where(StoreGrant.user_id == principal.user.id))
        )
    return query
