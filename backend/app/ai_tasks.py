"""商品资料分析任务：实时权限、不可变快照、显式重试及取消。"""

import hashlib
import json
import re
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import Field, StringConstraints, field_validator, model_validator
from sqlalchemy import func, select

from app.ai_inputs import AdTask, CompetitorTask, ReviewTask, ThemeTask, VideoTask
from app.core.errors import AppError
from app.daily_data import business_today
from app.dependencies import current_user, get_db, store_scope
from app.generator_snapshot import freeze_generator
from app.identity import audit, result, stamp
from app.model_calls import ACTIVE, daily_usage, lock_enterprise, recover_locked, shanghai_date
from app.model_settings import decrypt_secret
from app.models import (
    AiTask,
    Artifact,
    ArtifactVersion,
    AuditEvent,
    InputSnapshot,
    ModelCall,
    ModelConfig,
    Product,
    Store,
    StoreGrant,
    User,
    utcnow,
)
from app.product_snapshot import build_product_snapshot
from app.schemas import Input

router = APIRouter(prefix="/api/v1", tags=["ai-tasks"])
Identifier = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,18}$")]


class ProductAnalysisInput(Input):
    product_id: Identifier
    goal: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    start_date: date | None = None
    end_date: date | None = None

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def iso_date(cls, value):
        if (
            value is not None
            and type(value) is not date
            and (
                not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value)
            )
        ):
            raise ValueError("日期须为YYYY-MM-DD格式")
        return value

    @model_validator(mode="after")
    def valid_period(self):
        """日期成对提供，最长90天；允许当日但不将其视作完整日。"""
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("开始和结束日期须同时提供")
        if self.start_date is not None:
            length = (self.end_date - self.start_date).days + 1
            if (
                not 1 <= length <= 90
                or self.end_date > business_today()
                or self.start_date.toordinal() <= length
            ):
                raise ValueError("请选择不含未来日期且不超过90天的有效区间")
        return self


class CreateTask(Input):
    task_type: Literal["PRODUCT_ANALYSIS"]
    store_id: Identifier
    input: ProductAnalysisInput


AnyTask = Annotated[
    CreateTask | CompetitorTask | ThemeTask | VideoTask | AdTask | ReviewTask,
    Field(discriminator="task_type"),
]


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).digest()


def snapshot_view(snapshot):
    return {
        name: getattr(snapshot, name)
        for name in (
            "input_params",
            "source_refs",
            "data_copy",
            "metrics",
            "missing_items",
            "schema_version",
            "metric_version",
            "prompt_version",
        )
    }


def task_scope(p):
    query = select(AiTask).where(
        AiTask.enterprise_id == p.user.enterprise_id,
        AiTask.store_id.in_(store_scope(p).with_only_columns(Store.id)),
    )
    if p.user.role == "OPERATOR":
        query = query.where(AiTask.creator_id == p.user.id)
    return query


def live_actor(db, principal):
    # populate_existing会更新同一Session中的principal.user，必须先保留校验前的企业ID。
    expected_enterprise_id = principal.user.enterprise_id
    actor = db.scalar(
        select(User)
        .where(User.id == principal.user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        actor is None
        or actor.enterprise_id != expected_enterprise_id
        or actor.status != "ACTIVE"
        or actor.must_change_password
    ):
        raise AppError(403, "AUTH_FORBIDDEN", "当前账号不可执行此操作")
    return SimpleNamespace(user=actor)


def visible_task(db, p, task_id):
    task = db.scalar(
        task_scope(p)
        .where(AiTask.id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if task is None:
        raise AppError(404, "RESOURCE_NOT_FOUND", "任务不存在或当前无权访问")
    return task


def task_view(db, task, *, detail=False):
    snapshot = db.scalar(select(InputSnapshot).where(InputSnapshot.task_id == task.id))
    artifact = db.scalar(select(Artifact).where(Artifact.task_id == task.id))
    root = db.get(AiTask, task.root_task_id) if task.root_task_id else task
    view = {
        "id": str(task.id),
        "store_id": str(task.store_id),
        "task_type": task.task_type,
        "status": task.status,
        "snapshot_id": str(snapshot.id),
        "parent_task_id": str(task.parent_task_id) if task.parent_task_id else None,
        "retry_count": task.retry_count,
        "retries_remaining": max(0, 2 - root.retry_count),
        "error_code": task.error_code,
        "queued_at": stamp(task.queued_at),
        "started_at": stamp(task.started_at),
        "finished_at": stamp(task.finished_at),
        "artifact_id": str(artifact.id) if artifact else None,
    }
    if detail:
        view["snapshot"] = {**snapshot_view(snapshot), "sha256": snapshot.sha256.hex()}
        version = (
            db.scalar(
                select(ArtifactVersion)
                .where(ArtifactVersion.artifact_id == artifact.id)
                .order_by(ArtifactVersion.version_no.desc())
                .limit(1)
            )
            if artifact
            else None
        )
        view["draft"] = (
            {
                "id": str(version.id),
                "title": version.title,
                "content": version.content,
                "review_status": version.review_status,
            }
            if version
            else None
        )
    return view


def validate_key(key):
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", key):
        raise AppError(422, "IDEMPOTENCY_KEY_INVALID", "请提供16–64位有效请求键")


def available_config(db, enterprise, settings):
    config = (
        db.get(ModelConfig, enterprise.active_model_config_id)
        if enterprise.active_model_config_id
        else None
    )
    if not config or config.revoked_at:
        raise AppError(409, "MODEL_NOT_CONFIGURED", "请联系管理员配置有效模型")
    decrypt_secret(config, settings)
    if "api.deepseek.com" not in {h.lower() for h in settings.allowed_model_hosts}:
        raise AppError(422, "MODEL_HOST_NOT_ALLOWED", "模型域名未加入服务端允许集合")
    usage = daily_usage(db, enterprise.id, shanghai_date(utcnow()))
    if usage.consumed + usage.reserved >= config.daily_limit:
        raise AppError(429, "AI_DAILY_LIMIT", "企业今日模型调用额度已用完")
    return config


@router.post("/ai-tasks", status_code=202)
def create_task(
    body: AnyTask,
    request: Request,
    response: Response,
    idempotency_key: str = Header(...),
    p=Depends(current_user),
    db=Depends(get_db),
):
    """同一事务写任务/完整输入快照/审计；相同请求键不重复冻结和排队。"""
    validate_key(idempotency_key)
    enterprise = lock_enterprise(db, p.user.enterprise_id)
    p = live_actor(db, p)
    request_hash = digest(body.model_dump(mode="json", exclude_none=True))
    old = db.scalar(
        select(AiTask).where(
            AiTask.creator_id == p.user.id, AiTask.idempotency_key == idempotency_key
        )
    )
    if old:
        old = visible_task(db, p, old.id)
        if old.request_hash != request_hash:
            raise AppError(409, "IDEMPOTENCY_CONFLICT", "请求键已用于不同输入")
        response.status_code = 200
        view = task_view(db, old)
        db.commit()
        return result(request, view)
    store = db.scalar(store_scope(p).where(Store.id == int(body.store_id)).with_for_update())
    if not store:
        raise AppError(404, "RESOURCE_NOT_FOUND", "店铺不存在或未授权")
    if store.status != "ACTIVE":
        raise AppError(409, "STORE_ARCHIVED", "已归档店铺不能创建任务")
    product = (
        db.scalar(
            select(Product)
            .where(Product.id == int(body.input.product_id), Product.store_id == store.id)
            .with_for_update()
        )
        if body.task_type == "PRODUCT_ANALYSIS"
        else None
    )
    if body.task_type == "PRODUCT_ANALYSIS" and not product:
        raise AppError(404, "RESOURCE_NOT_FOUND", "商品不存在或不属于当前店铺")
    if product is not None and product.status != "ACTIVE":
        raise AppError(409, "PRODUCT_ARCHIVED", "已归档商品不能创建任务")
    config = available_config(db, enterprise, request.app.state.settings)
    data = (
        build_product_snapshot(db, product, body.input)
        if product
        else freeze_generator(db, store, body)
    )
    task = AiTask(
        enterprise_id=enterprise.id,
        store_id=store.id,
        creator_id=p.user.id,
        created_by=p.user.id,
        task_type=body.task_type,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        model_config_id=config.id,
    )
    db.add(task)
    db.flush()
    db.add(InputSnapshot(task_id=task.id, sha256=digest(data), **data))
    db.flush()
    audit(
        db,
        request,
        p.user,
        "AI_TASK_CREATED",
        task,
        changes={"task_type": task.task_type, "model_config_id": str(config.id)},
    )
    view = task_view(db, task)
    db.commit()
    return result(request, view)


@router.get("/ai-tasks")
def list_tasks(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    p=Depends(current_user),
    db=Depends(get_db),
):
    query = task_scope(p)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    tasks = db.scalars(
        query.order_by(AiTask.id.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return result(
        request,
        {
            "items": [task_view(db, t) for t in tasks],
            "total": total,
            "page": page,
            "page_size": page_size,
        },
    )


@router.get("/ai-tasks/{task_id}")
def get_task(task_id: int, request: Request, p=Depends(current_user), db=Depends(get_db)):
    task = db.scalar(task_scope(p).where(AiTask.id == task_id))
    if not task:
        raise AppError(404, "RESOURCE_NOT_FOUND", "任务不存在或当前无权访问")
    return result(request, task_view(db, task, detail=True))


@router.post("/ai-tasks/{task_id}/cancel")
def cancel_task(task_id: int, request: Request, p=Depends(current_user), db=Depends(get_db)):
    """取消递增围栏；已发送调用保留占位和消耗，禁止迟到响应生成产出。"""
    lock_enterprise(db, p.user.enterprise_id)
    p = live_actor(db, p)
    task = visible_task(db, p, task_id)
    if task.status in {"QUEUED", "RUNNING"}:
        task.status, task.finished_at = "CANCELLED", utcnow()
        task.fence_token += 1
        calls = db.scalars(
            select(ModelCall)
            .where(ModelCall.task_id == task.id, ModelCall.status == "RESERVED")
            .with_for_update()
        ).all()
        for call in calls:
            daily_usage(db, task.enterprise_id, call.usage_date).reserved -= 1
            call.status, call.finished_at, call.error_code = "RELEASED", utcnow(), "AI_CANCELLED"
        audit(db, request, p.user, "AI_TASK_CANCELLED", task)
    view = task_view(db, task)
    db.commit()
    return result(request, view)


@router.post("/ai-tasks/{task_id}/retries", status_code=202)
def retry_task(
    task_id: int,
    request: Request,
    response: Response,
    idempotency_key: str = Header(...),
    p=Depends(current_user),
    db=Depends(get_db),
):
    """只有显式失败重试才能新建任务；保留原快照/模型配置，根任务累计最多两次。"""
    validate_key(idempotency_key)
    enterprise = lock_enterprise(db, p.user.enterprise_id)
    p = live_actor(db, p)
    parent = visible_task(db, p, task_id)
    fingerprint = digest({"retry_of": str(parent.id)})
    existing = db.scalar(
        select(AiTask).where(
            AiTask.creator_id == p.user.id, AiTask.idempotency_key == idempotency_key
        )
    )
    if existing:
        if existing.request_hash != fingerprint:
            raise AppError(409, "IDEMPOTENCY_CONFLICT", "请求键已用于不同操作")
        existing = visible_task(db, p, existing.id)
        response.status_code = 200
        view = task_view(db, existing)
        db.commit()
        return result(request, view)
    if parent.status != "FAILED":
        raise AppError(409, "AI_INVALID_STATE", "只有失败任务可重试")
    root = db.get(AiTask, parent.root_task_id) if parent.root_task_id else parent
    if root.retry_count >= 2:
        raise AppError(409, "AI_RETRY_EXHAUSTED", "该任务累计重试次数已达两次")
    store = db.scalar(store_scope(p).where(Store.id == parent.store_id).with_for_update())
    if store.status != "ACTIVE":
        raise AppError(409, "STORE_ARCHIVED", "已归档店铺不能重试")
    available_config(db, enterprise, request.app.state.settings)
    frozen = db.get(ModelConfig, parent.model_config_id)
    if frozen.revoked_at:
        raise AppError(409, "MODEL_CONFIG_REVOKED", "原任务模型已撤销，请重新创建任务")
    original = db.scalar(select(InputSnapshot).where(InputSnapshot.task_id == parent.id))
    data = snapshot_view(original)
    if digest(data) != original.sha256:
        raise AppError(409, "AI_SNAPSHOT_INVALID", "原任务快照校验失败")
    root.retry_count += 1
    child = AiTask(
        enterprise_id=parent.enterprise_id,
        store_id=parent.store_id,
        creator_id=p.user.id,
        created_by=p.user.id,
        task_type=parent.task_type,
        idempotency_key=idempotency_key,
        request_hash=fingerprint,
        parent_task_id=parent.id,
        root_task_id=root.id,
        retry_count=root.retry_count,
        model_config_id=parent.model_config_id,
    )
    db.add(child)
    db.flush()
    db.add(InputSnapshot(task_id=child.id, sha256=original.sha256, **data))
    db.flush()
    audit(db, request, p.user, "AI_TASK_RETRIED", child, changes={"parent_task_id": str(parent.id)})
    view = task_view(db, child)
    db.commit()
    return result(request, view)


def authorized_actor(db, task):
    """Worker开始和最终提交都重新检查账号、店铺和授权；商品内容则继续使用快照。"""
    actor = db.scalar(
        select(User)
        .where(User.id == task.creator_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    store = db.scalar(
        select(Store)
        .where(Store.id == task.store_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    allowed = (
        actor is not None
        and store is not None
        and actor.role in {"ADMIN", "SUPERVISOR", "OPERATOR"}
        and actor.status == "ACTIVE"
        and not actor.must_change_password
        and actor.enterprise_id == task.enterprise_id
        and store.enterprise_id == task.enterprise_id
        and store.status == "ACTIVE"
    )
    if allowed and actor.role == "OPERATOR":
        allowed = (
            db.scalar(
                select(StoreGrant.id).where(
                    StoreGrant.user_id == actor.id, StoreGrant.store_id == store.id
                )
            )
            is not None
        )
    return actor if allowed else None


def fail_task(db, task, code):
    if task.status in {"QUEUED", "RUNNING"}:
        task.status, task.error_code, task.finished_at = "FAILED", code, utcnow()
        task.fence_token += 1
        db.add(
            AuditEvent(
                enterprise_id=task.enterprise_id,
                actor_id=None,
                store_id=task.store_id,
                action="AI_TASK_FAILED",
                target_type="ai_tasks",
                target_id=str(task.id),
                request_id=f"ai-task-{task.id}",
                result="FAILED",
                redacted_diff={"error_code": code},
            )
        )


def dispatch_task(db, settings):
    """短事务派发最早的可运行任务；与连接测试共用企业锁、并发槽和日额度。"""
    now = utcnow()
    candidates = db.scalars(
        select(AiTask)
        .where(AiTask.status.in_(("QUEUED", "RUNNING")))
        .order_by(AiTask.queued_at, AiTask.id)
        .limit(100)
    ).all()
    for candidate in candidates:
        enterprise = lock_enterprise(db, candidate.enterprise_id)
        db.refresh(candidate)
        recover_locked(db, enterprise.id, now)
        if candidate.status == "RUNNING":
            if candidate.lease_until and candidate.lease_until <= now:
                fail_task(db, candidate, "AI_INTERRUPTED")
                db.commit()
                return True
            db.commit()
            continue
        if candidate.status != "QUEUED":
            db.commit()
            continue
        if candidate.queued_at <= now - timedelta(minutes=10):
            fail_task(db, candidate, "AI_QUEUE_TIMEOUT")
            db.commit()
            return True
        config = db.get(ModelConfig, candidate.model_config_id)
        current = (
            db.get(ModelConfig, enterprise.active_model_config_id)
            if enterprise.active_model_config_id
            else None
        )
        if (
            not authorized_actor(db, candidate)
            or config.revoked_at
            or not current
            or current.revoked_at
        ):
            fail_task(db, candidate, "AI_PERMISSION_OR_CONFIG_REVOKED")
            db.commit()
            return True
        usage = daily_usage(db, enterprise.id, shanghai_date(now))
        if usage.consumed + usage.reserved >= current.daily_limit:
            fail_task(db, candidate, "AI_DAILY_LIMIT")
            db.commit()
            return True
        active = list(
            db.scalars(
                select(ModelCall).where(
                    ModelCall.enterprise_id == enterprise.id, ModelCall.status.in_(ACTIVE)
                )
            )
        )
        if len(active) >= 5 or sum(c.actor_id == candidate.creator_id for c in active) >= 2:
            db.commit()
            continue
        usage.reserved += 1
        candidate.status, candidate.lease_until = "RUNNING", now + timedelta(seconds=30)
        candidate.fence_token += 1
        db.add(
            AuditEvent(
                enterprise_id=candidate.enterprise_id,
                actor_id=None,
                store_id=candidate.store_id,
                action="AI_TASK_STARTED",
                target_type="ai_tasks",
                target_id=str(candidate.id),
                request_id=f"ai-task-{candidate.id}",
                result="SUCCEEDED",
                redacted_diff={"status": "RUNNING"},
            )
        )
        db.add(
            ModelCall(
                enterprise_id=enterprise.id,
                config_id=config.id,
                actor_id=candidate.creator_id,
                task_id=candidate.id,
                purpose="AI_GENERATION",
                usage_date=usage.usage_date,
                request_key=hashlib.sha256(f"AI_TASK:{candidate.id}".encode()).hexdigest(),
                deadline_at=now + timedelta(seconds=30),
            )
        )
        db.commit()
        return True
    db.rollback()
    return False


def finish_task(db, call, outcome, fence):
    """同事务成功写任务、唯一产出、v1草稿及审计；取消/失权/迟到均不落正文。"""
    task = db.scalar(
        select(AiTask)
        .where(AiTask.id == call.task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if task.status != "RUNNING" or task.fence_token != fence:
        return
    if task.deadline_at <= utcnow() or task.lease_until <= utcnow():
        fail_task(db, task, "AI_INTERRUPTED")
        return
    if not authorized_actor(db, task):
        fail_task(db, task, "AI_PERMISSION_REVOKED")
        return
    if call.status != "SUCCEEDED" or not outcome:
        fail_task(db, task, call.error_code or "AI_OUTPUT_INVALID")
        return
    artifact = Artifact(
        task_id=task.id,
        enterprise_id=task.enterprise_id,
        store_id=task.store_id,
        creator_id=task.creator_id,
        created_by=task.creator_id,
        artifact_type=task.task_type,
    )
    db.add(artifact)
    db.flush()
    db.add(
        ArtifactVersion(
            artifact_id=artifact.id,
            title=outcome.content["title"],
            content=outcome.content,
            created_by=task.creator_id,
        )
    )
    task.status, task.finished_at = "SUCCEEDED", utcnow()
    db.add(
        AuditEvent(
            enterprise_id=task.enterprise_id,
            actor_id=task.creator_id,
            store_id=task.store_id,
            action="AI_TASK_SUCCEEDED",
            target_type="ai_tasks",
            target_id=str(task.id),
            request_id=f"ai-task-{task.id}",
            result="SUCCEEDED",
            redacted_diff={"artifact_id": str(artifact.id)},
        )
    )
