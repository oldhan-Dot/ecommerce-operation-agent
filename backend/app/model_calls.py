"""模型调用调度：企业锁串行预留额度，发送前落账，不自动重放不确定请求。"""

import asyncio
import hashlib
import re
from datetime import UTC, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import Field
from sqlalchemy import func, select

from app.core.errors import AppError
from app.dependencies import administrator, get_db
from app.identity import audit, result, stamp
from app.model_provider import probe
from app.model_settings import decrypt_secret
from app.models import (
    DailyCallUsage,
    Enterprise,
    ModelCall,
    ModelConfig,
    SchedulerState,
    User,
    utcnow,
)
from app.schemas import Input

router = APIRouter(prefix="/api/v1/settings/model", tags=["model-calls"])
ACTIVE = ("RESERVED", "STARTED")


class TestInput(Input):
    config_version: int = Field(ge=1, strict=True)


def shanghai_date(now):
    return now.replace(tzinfo=UTC).astimezone(ZoneInfo("Asia/Shanghai")).date()


def lock_enterprise(db, enterprise_id):
    """与配置及账号写入保持企业→调度行→账号的锁序，避免跨进程超发。"""
    enterprise = db.scalar(
        select(Enterprise)
        .where(Enterprise.id == enterprise_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    state = db.get(SchedulerState, enterprise_id)
    if state is None:
        db.add(SchedulerState(enterprise_id=enterprise_id))
        db.flush()
    db.scalar(
        select(SchedulerState)
        .where(SchedulerState.enterprise_id == enterprise_id)
        .with_for_update()
    )
    return enterprise


def daily_usage(db, enterprise_id, day):
    usage = db.scalar(
        select(DailyCallUsage).where(
            DailyCallUsage.enterprise_id == enterprise_id, DailyCallUsage.usage_date == day
        )
    )
    if usage is None:
        usage = DailyCallUsage(enterprise_id=enterprise_id, usage_date=day, consumed=0, reserved=0)
        db.add(usage)
        db.flush()
    return usage


def recover_locked(db, enterprise_id, now):
    """未发出的预留到期归还；已落账调用到期标为 UNKNOWN，保留消耗且禁止重发。"""
    calls = db.scalars(
        select(ModelCall)
        .where(
            ModelCall.enterprise_id == enterprise_id,
            ModelCall.status.in_(ACTIVE),
            ModelCall.deadline_at <= now,
        )
        .with_for_update()
    ).all()
    for call in calls:
        if call.status == "RESERVED":
            daily_usage(db, enterprise_id, call.usage_date).reserved -= 1
            call.status = "RELEASED"
            call.error_code = "MODEL_QUEUE_EXPIRED"
        else:
            call.status = "UNKNOWN"
            call.error_code = "MODEL_RESULT_UNKNOWN"
        call.finished_at = now
        call.fence_token += 1
    db.flush()


def call_view(call):
    return {
        "call_id": str(call.id),
        "config_id": str(call.config_id),
        "status": call.status,
        "ok": call.status == "SUCCEEDED",
        "error_code": call.error_code,
        "latency_ms": call.latency_ms,
        "usage_tokens": call.usage_tokens,
        "created_at": stamp(call.created_at),
        "finished_at": stamp(call.finished_at),
    }


@router.post("/test", status_code=202)
def request_test(
    body: TestInput,
    request: Request,
    response: Response,
    idempotency_key: str = Header(...),
    p=Depends(administrator),
    db=Depends(get_db),
):
    """异步接收测试；同一用户的同一键只能绑定一个配置版本，无隐式网络重试。"""
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", idempotency_key):
        raise AppError(422, "IDEMPOTENCY_KEY_INVALID", "请提供 16–128 位有效请求键")
    enterprise = lock_enterprise(db, p.user.enterprise_id)
    actor = db.scalar(
        select(User)
        .where(User.id == p.user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        actor.role != "ADMIN"
        or actor.status != "ACTIVE"
        or actor.must_change_password
        or actor.enterprise_id != enterprise.id
    ):
        raise AppError(403, "AUTH_FORBIDDEN", "当前账号无权测试模型")
    key = hashlib.sha256(
        f"{enterprise.id}:{actor.id}:CONNECTION_TEST:{idempotency_key}".encode()
    ).hexdigest()
    existing = db.scalar(select(ModelCall).where(ModelCall.request_key == key))
    if existing:
        frozen = db.get(ModelConfig, existing.config_id)
        if frozen.config_version != body.config_version:
            raise AppError(409, "IDEMPOTENCY_CONFLICT", "该请求键已用于其他配置版本")
        response.status_code = 200
        db.commit()
        return result(request, call_view(existing))
    config = (
        db.get(ModelConfig, enterprise.active_model_config_id)
        if enterprise.active_model_config_id
        else None
    )
    if not config or config.revoked_at:
        raise AppError(422, "MODEL_NOT_CONFIGURED", "请先保存有效模型配置")
    if config.config_version != body.config_version:
        raise AppError(409, "DATA_VERSION_CONFLICT", "模型配置已变化，请重新读取后测试")
    # 在预留前检查解密和白名单，避免明知配置不可用仍占用额度。
    decrypt_secret(config, request.app.state.settings)
    if "api.deepseek.com" not in {
        h.lower() for h in request.app.state.settings.allowed_model_hosts
    }:
        raise AppError(422, "MODEL_HOST_NOT_ALLOWED", "模型域名未加入服务端允许集合")
    now = utcnow()
    recover_locked(db, enterprise.id, now)
    usage = daily_usage(db, enterprise.id, shanghai_date(now))
    if usage.consumed + usage.reserved >= config.daily_limit:
        raise AppError(429, "AI_DAILY_LIMIT", "企业今日调用额度已用完")
    count = db.scalar(
        select(func.count())
        .select_from(ModelCall)
        .where(ModelCall.enterprise_id == enterprise.id, ModelCall.status.in_(ACTIVE))
    )
    actor_count = db.scalar(
        select(func.count())
        .select_from(ModelCall)
        .where(ModelCall.actor_id == actor.id, ModelCall.status.in_(ACTIVE))
    )
    if count >= 5 or actor_count >= 2:
        raise AppError(429, "AI_CONCURRENCY_LIMIT", "当前调用过多，请等待已有调用结束")
    usage.reserved += 1
    call = ModelCall(
        enterprise_id=enterprise.id,
        actor_id=actor.id,
        config_id=config.id,
        usage_date=usage.usage_date,
        request_key=key,
        deadline_at=now + timedelta(minutes=5),
    )
    db.add(call)
    db.flush()
    audit(
        db,
        request,
        actor,
        "MODEL_TEST_REQUESTED",
        call,
        changes={"config_id": str(config.id), "call_id": str(call.id)},
    )
    db.commit()
    return result(request, call_view(call))


@router.get("/calls")
def list_calls(request: Request, p=Depends(administrator), db=Depends(get_db)):
    calls = db.scalars(
        select(ModelCall)
        .where(ModelCall.enterprise_id == p.user.enterprise_id)
        .order_by(ModelCall.id.desc())
        .limit(20)
    ).all()
    day = shanghai_date(utcnow())
    usage = db.scalar(
        select(DailyCallUsage).where(
            DailyCallUsage.enterprise_id == p.user.enterprise_id, DailyCallUsage.usage_date == day
        )
    )
    enterprise = db.get(Enterprise, p.user.enterprise_id)
    config = (
        db.get(ModelConfig, enterprise.active_model_config_id)
        if enterprise.active_model_config_id
        else None
    )
    return result(
        request,
        {
            "items": [call_view(c) for c in calls],
            "usage_date": str(day),
            "consumed": usage.consumed if usage else 0,
            "reserved": usage.reserved if usage else 0,
            "daily_limit": config.daily_limit if config else None,
        },
    )


@router.get("/calls/{call_id}")
def get_call(call_id: int, request: Request, p=Depends(administrator), db=Depends(get_db)):
    call = db.scalar(
        select(ModelCall).where(
            ModelCall.id == call_id, ModelCall.enterprise_id == p.user.enterprise_id
        )
    )
    if not call:
        raise AppError(404, "DATA_NOT_FOUND", "调用记录不存在")
    return result(request, call_view(call))


def process_model_call(db, settings):
    """短事务认领并落账→释放数据库连接→单次请求→带围栏确认；宕机不自动重发。"""
    now = utcnow()
    candidate = db.scalar(
        select(ModelCall)
        .where(
            ModelCall.status.in_(ACTIVE),
            (ModelCall.status == "RESERVED") | (ModelCall.deadline_at <= now),
        )
        .order_by(ModelCall.id)
        .limit(1)
    )
    if candidate is None:
        db.rollback()
        return False
    enterprise = lock_enterprise(db, candidate.enterprise_id)
    now = utcnow()
    recover_locked(db, enterprise.id, now)
    db.refresh(candidate)
    if candidate.status != "RESERVED":
        db.commit()
        return True
    config = db.get(ModelConfig, candidate.config_id)
    current = (
        db.get(ModelConfig, enterprise.active_model_config_id)
        if enterprise.active_model_config_id
        else None
    )
    actor = db.scalar(
        select(User)
        .where(User.id == candidate.actor_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    usage = daily_usage(db, enterprise.id, candidate.usage_date)
    usage.reserved -= 1
    error = None
    if (
        not current
        or config.revoked_at
        or current.revoked_at
        or actor.status != "ACTIVE"
        or actor.role != "ADMIN"
        or actor.must_change_password
        or actor.enterprise_id != enterprise.id
    ):
        error = "MODEL_CALL_REVOKED"
    day = shanghai_date(now)
    today = daily_usage(db, enterprise.id, day)
    if current and today.consumed + today.reserved >= current.daily_limit:
        error = "AI_DAILY_LIMIT"
    try:
        secret = decrypt_secret(config, settings) if not error else None
    except AppError as exc:
        error = exc.code
    if error:
        candidate.status, candidate.error_code, candidate.finished_at = "RELEASED", error, now
        db.commit()
        return True
    today.consumed += 1
    candidate.usage_date = day
    candidate.status, candidate.started_at = "STARTED", now
    candidate.fence_token += 1
    candidate.deadline_at = now + timedelta(seconds=config.timeout_seconds + 10)
    call_id, enterprise_id, fence = candidate.id, enterprise.id, candidate.fence_token
    # 脱离 Session 使用冻结配置；commit 后网络阶段不持有行锁或连接。
    db.expunge(config)
    db.commit()
    outcome, error = None, None
    try:
        outcome = asyncio.run(probe(config, secret, settings.allowed_model_hosts))
    except AppError as exc:
        error = exc.code
    except Exception:
        # 未预料错误也不记录原异常，已落账请求不可隐式重放。
        error = "MODEL_RESULT_UNKNOWN"
    lock_enterprise(db, enterprise_id)
    recover_locked(db, enterprise_id, utcnow())
    call = db.scalar(
        select(ModelCall)
        .where(ModelCall.id == call_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if call.status == "STARTED" and call.fence_token == fence:
        call.status = (
            "SUCCEEDED"
            if outcome
            else "UNKNOWN"
            if error in {"MODEL_TIMEOUT", "MODEL_RESULT_UNKNOWN"}
            else "FAILED"
        )
        call.error_code, call.finished_at = error, utcnow()
        if outcome:
            call.latency_ms, call.usage_tokens = outcome.latency_ms, outcome.usage_tokens
    db.commit()
    return True
