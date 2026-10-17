"""产出版本、非作者审核与指定版本Markdown导出；所有入口重新执行企业/店铺/作者范围校验。"""

import hashlib
import re
from datetime import date, datetime, time, timedelta
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import Field, StringConstraints
from sqlalchemy import func, select

from app.ai_tasks import digest, live_actor, snapshot_view, validate_key
from app.artifact_content import edited_content, markdown_report
from app.core.errors import AppError
from app.dependencies import current_user, get_db, store_scope
from app.identity import audit, result, stamp
from app.model_calls import lock_enterprise
from app.models import (
    AiTask,
    Artifact,
    ArtifactAction,
    ArtifactReview,
    ArtifactVersion,
    InputSnapshot,
    ModelConfig,
    Store,
    User,
    utcnow,
)
from app.schemas import EntityId, Input

router = APIRouter(prefix="/api/v1", tags=["artifacts"])


class VersionInput(Input):
    base_version_id: EntityId
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    content: dict


class ReviewInput(Input):
    decision: Literal["APPROVE", "REJECT"]
    comment: str | None = Field(default=None, max_length=1000)


def scope(p):
    query = select(Artifact).where(
        Artifact.enterprise_id == p.user.enterprise_id,
        Artifact.store_id.in_(store_scope(p).with_only_columns(Store.id)),
    )
    return query.where(Artifact.creator_id == p.user.id) if p.user.role == "OPERATOR" else query


def latest(db, artifact):
    return db.scalar(
        select(ArtifactVersion)
        .where(ArtifactVersion.artifact_id == artifact.id)
        .order_by(ArtifactVersion.version_no.desc())
        .limit(1)
    )


def version_view(version, artifact, *, content=False):
    data = {
        "id": str(version.id),
        "artifact_id": str(artifact.id),
        "version_no": version.version_no,
        "title": version.title,
        "review_status": version.review_status,
        "version": version.version,
        "artifact_version": artifact.version,
        "author_id": str(artifact.creator_id),
        "created_at": stamp(version.created_at),
        "submitted_at": stamp(version.submitted_at),
        "reviewed_at": stamp(version.reviewed_at),
        "reviewer_id": str(version.reviewer_id) if version.reviewer_id else None,
        "review_comment": version.review_comment,
    }
    if content:
        data["content"] = version.content
    return data


def accessible(db, p, artifact_id):
    artifact = db.scalar(scope(p).where(Artifact.id == artifact_id))
    if not artifact:
        raise AppError(404, "RESOURCE_NOT_FOUND", "产出不存在或无权访问")
    return artifact


def write_context(db, p, artifact_id):
    lock_enterprise(db, p.user.enterprise_id)
    p = live_actor(db, p)
    artifact = accessible(db, p, artifact_id)
    store = db.scalar(select(Store).where(Store.id == artifact.store_id).with_for_update())
    if store.status != "ACTIVE":
        raise AppError(409, "STORE_ARCHIVED", "已归档店铺产出只可查看和导出")
    artifact = db.scalar(
        select(Artifact)
        .where(Artifact.id == artifact.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return p, artifact


def require_match(header, version):
    if header is None:
        raise AppError(428, "PRECONDITION_REQUIRED", "操作需要If-Match版本条件")
    if header != f'"{version}"':
        raise AppError(409, "ARTIFACT_VERSION_CONFLICT", "版本已变化，请刷新后重试")


@router.get("/artifacts")
def list_artifacts(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    store_id: int | None = Query(None, gt=0),
    author_id: int | None = Query(None, gt=0),
    task_type: str | None = None,
    review_status: str | None = Query(None, pattern="^(DRAFT|PENDING_REVIEW|APPROVED|REJECTED)$"),
    keyword: str = Query("", max_length=200),
    start_date: date | None = None,
    end_date: date | None = None,
    p=Depends(current_user),
    db=Depends(get_db),
):
    newest = (
        select(func.max(ArtifactVersion.version_no))
        .where(ArtifactVersion.artifact_id == Artifact.id)
        .correlate(Artifact)
        .scalar_subquery()
    )
    query = scope(p).join(
        ArtifactVersion,
        (ArtifactVersion.artifact_id == Artifact.id) & (ArtifactVersion.version_no == newest),
    )
    if store_id:
        query = query.where(Artifact.store_id == store_id)
    if author_id:
        query = query.where(Artifact.creator_id == author_id)
    if task_type:
        query = query.where(Artifact.artifact_type == task_type)
    if review_status:
        query = query.where(ArtifactVersion.review_status == review_status)
    if keyword:
        query = query.where(ArtifactVersion.title.contains(keyword, autoescape=True))
    if start_date and end_date and start_date > end_date:
        raise AppError(422, "DATA_INVALID", "日期范围无效")
    if start_date:
        query = query.where(Artifact.created_at >= datetime.combine(start_date, time.min))
    if end_date:
        if end_date == date.max:
            raise AppError(422, "DATA_INVALID", "结束日期超出支持范围")
        query = query.where(
            Artifact.created_at < datetime.combine(end_date + timedelta(days=1), time.min)
        )
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(
        query.order_by(Artifact.created_at.desc(), Artifact.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return result(
        request,
        {
            "items": [
                {
                    "id": str(a.id),
                    "store_id": str(a.store_id),
                    "author_id": str(a.creator_id),
                    "task_type": a.artifact_type,
                    "version": a.version,
                    "latest": version_view(latest(db, a), a),
                }
                for a in items
            ],
            "total": total,
            "page": page,
            "page_size": page_size,
        },
    )


@router.get("/artifacts/{artifact_id}")
def get_artifact(artifact_id: int, request: Request, p=Depends(current_user), db=Depends(get_db)):
    artifact = accessible(db, p, artifact_id)
    snapshot = db.scalar(select(InputSnapshot).where(InputSnapshot.task_id == artifact.task_id))
    return result(
        request,
        {
            "id": str(artifact.id),
            "store_id": str(artifact.store_id),
            "author_id": str(artifact.creator_id),
            "task_type": artifact.artifact_type,
            "version": artifact.version,
            "latest": version_view(latest(db, artifact), artifact, content=True),
            "snapshot": snapshot_view(snapshot),
        },
    )


@router.get("/artifacts/{artifact_id}/versions")
def list_versions(
    artifact_id: int,
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    p=Depends(current_user),
    db=Depends(get_db),
):
    artifact = accessible(db, p, artifact_id)
    query = select(ArtifactVersion).where(ArtifactVersion.artifact_id == artifact.id)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(
        query.order_by(ArtifactVersion.version_no.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return result(
        request,
        {
            "items": [version_view(v, artifact) for v in items],
            "total": total,
            "page": page,
            "page_size": page_size,
        },
    )


@router.get("/artifact-versions/{version_id}")
def get_version(version_id: int, request: Request, p=Depends(current_user), db=Depends(get_db)):
    version = db.get(ArtifactVersion, version_id)
    if not version:
        raise AppError(404, "RESOURCE_NOT_FOUND", "版本不存在")
    artifact = accessible(db, p, version.artifact_id)
    return result(request, version_view(version, artifact, content=True))


@router.post("/artifacts/{artifact_id}/versions", status_code=201)
def save_version(
    artifact_id: int,
    body: VersionInput,
    request: Request,
    if_match: str | None = Header(None),
    p=Depends(current_user),
    db=Depends(get_db),
):
    """新建不可变正文版本；作者、最新基础版本、乐观锁及同Schema约束全部通过才保存。"""
    p, artifact = write_context(db, p, artifact_id)
    if artifact.creator_id != p.user.id:
        raise AppError(403, "AUTH_FORBIDDEN", "只有作者可修订产出")
    require_match(if_match, artifact.version)
    previous = latest(db, artifact)
    if str(previous.id) != body.base_version_id:
        raise AppError(409, "ARTIFACT_VERSION_CONFLICT", "只能基于最新版本创建修订")
    if previous.review_status == "PENDING_REVIEW":
        raise AppError(409, "REVIEW_INVALID_STATE", "待审版本请先撤回再修订")
    snapshot = db.scalar(select(InputSnapshot).where(InputSnapshot.task_id == artifact.task_id))
    saved = snapshot_view(snapshot)
    if digest(saved) != snapshot.sha256:
        raise AppError(409, "AI_SNAPSHOT_INVALID", "输入快照校验失败")
    content = edited_content(body.title, body.content, saved, artifact.artifact_type)
    version = ArtifactVersion(
        artifact_id=artifact.id,
        version_no=previous.version_no + 1,
        title=body.title,
        content=content,
        created_by=p.user.id,
    )
    db.add(version)
    artifact.version += 1
    db.flush()
    audit(
        db,
        request,
        p.user,
        "ARTIFACT_VERSION_CREATED",
        artifact,
        changes={"version_id": str(version.id), "version_no": version.version_no},
    )
    response = version_view(version, artifact, content=True)
    db.commit()
    return result(request, response)


def transition(db, p, version_id, action, body, request, key, if_match):
    """企业→账号→店铺→产出→版本短锁；同键重放不重复审核，异负载拒绝。"""
    validate_key(key)
    version = db.get(ArtifactVersion, version_id)
    if not version:
        raise AppError(404, "RESOURCE_NOT_FOUND", "版本不存在")
    p, artifact = write_context(db, p, version.artifact_id)
    version = db.scalar(
        select(ArtifactVersion)
        .where(ArtifactVersion.id == version_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if action in {"SUBMIT", "WITHDRAW"} and artifact.creator_id != p.user.id:
        raise AppError(403, "AUTH_FORBIDDEN", "只有作者可提交或撤回")
    if action == "REVIEW":
        if p.user.role not in {"ADMIN", "SUPERVISOR"}:
            raise AppError(403, "AUTH_FORBIDDEN", "当前角色不能审核")
        if artifact.creator_id == p.user.id:
            raise AppError(403, "REVIEW_SELF_FORBIDDEN", "不得审核本人产出")
    request_key = hashlib.sha256(f"{p.user.id}:{key}".encode()).hexdigest()
    fingerprint = digest({"version_id": str(version_id), "action": action, "body": body})
    existing = db.scalar(select(ArtifactAction).where(ArtifactAction.request_key == request_key))
    if existing:
        if existing.request_hash != fingerprint:
            raise AppError(409, "IDEMPOTENCY_CONFLICT", "请求键已用于不同审核操作")
        response = existing.response
        db.commit()
        return response
    require_match(if_match, version.version)
    if latest(db, artifact).id != version.id:
        raise AppError(409, "ARTIFACT_VERSION_CONFLICT", "旧版本不可提交或审核，请刷新最新修订")
    if action == "SUBMIT":
        if version.review_status != "DRAFT":
            raise AppError(409, "REVIEW_INVALID_STATE", "只有最新草稿可提交")
        version.review_status = "PENDING_REVIEW"
        version.submitted_at = utcnow()
    elif action == "WITHDRAW":
        if version.review_status != "PENDING_REVIEW":
            raise AppError(409, "REVIEW_INVALID_STATE", "只有待审版本可撤回")
        version.review_status = "DRAFT"
        version.submitted_at = None
    else:
        if version.review_status != "PENDING_REVIEW":
            raise AppError(409, "REVIEW_INVALID_STATE", "该版本已非待审状态")
        comment = (body.get("comment") or "").strip()
        if body["decision"] == "REJECT" and not comment:
            raise AppError(422, "REVIEW_COMMENT_REQUIRED", "驳回必须填写原因")
        version.review_status = "APPROVED" if body["decision"] == "APPROVE" else "REJECTED"
        version.reviewer_id = p.user.id
        version.reviewed_at = utcnow()
        version.review_comment = comment or None
        db.add(
            ArtifactReview(
                version_id=version.id,
                reviewer_id=p.user.id,
                decision=body["decision"],
                comment=comment or None,
            )
        )
    version.version += 1
    artifact.version += 1
    db.flush()
    response = version_view(version, artifact)
    db.add(
        ArtifactAction(
            version_id=version.id,
            actor_id=p.user.id,
            action=action,
            request_key=request_key,
            request_hash=fingerprint,
            response=response,
        )
    )
    audit(
        db,
        request,
        p.user,
        "ARTIFACT_" + action,
        artifact,
        changes={"version_id": str(version.id), "review_status": version.review_status},
    )
    db.commit()
    return response


@router.post("/artifact-versions/{version_id}/submit")
def submit_version(
    version_id: int,
    request: Request,
    idempotency_key: str = Header(...),
    if_match: str | None = Header(None),
    p=Depends(current_user),
    db=Depends(get_db),
):
    return result(
        request, transition(db, p, version_id, "SUBMIT", {}, request, idempotency_key, if_match)
    )


@router.post("/artifact-versions/{version_id}/withdraw")
def withdraw_version(
    version_id: int,
    request: Request,
    idempotency_key: str = Header(...),
    if_match: str | None = Header(None),
    p=Depends(current_user),
    db=Depends(get_db),
):
    return result(
        request, transition(db, p, version_id, "WITHDRAW", {}, request, idempotency_key, if_match)
    )


@router.post("/artifact-versions/{version_id}/reviews")
def review_version(
    version_id: int,
    body: ReviewInput,
    request: Request,
    idempotency_key: str = Header(...),
    if_match: str | None = Header(None),
    p=Depends(current_user),
    db=Depends(get_db),
):
    return result(
        request,
        transition(
            db, p, version_id, "REVIEW", body.model_dump(), request, idempotency_key, if_match
        ),
    )


@router.get("/artifact-versions/{version_id}/export")
def export_version(
    version_id: int,
    request: Request,
    mode: Literal["draft", "official"] = "draft",
    p=Depends(current_user),
    db=Depends(get_db),
):
    """每次下载重新鉴权；仅选中已通过版本可正式导出，旧通过版保留独立可用性。"""
    version = db.get(ArtifactVersion, version_id)
    if not version:
        raise AppError(404, "RESOURCE_NOT_FOUND", "版本不存在")
    artifact = accessible(db, p, version.artifact_id)
    if mode == "official" and version.review_status != "APPROVED":
        raise AppError(409, "EXPORT_NOT_APPROVED", "正式导出须选择已通过的具体版本")
    snapshot = db.scalar(select(InputSnapshot).where(InputSnapshot.task_id == artifact.task_id))
    task = db.get(AiTask, artifact.task_id)
    config = db.get(ModelConfig, task.model_config_id)
    author = db.get(User, artifact.creator_id)
    reviewer = db.get(User, version.reviewer_id) if version.reviewer_id else None
    text = markdown_report(
        artifact,
        version,
        snapshot,
        db.get(Store, artifact.store_id),
        config.model_name,
        author,
        reviewer,
    )
    if mode == "draft":
        text = "> 未审核草稿副本 · 不作为正式批准版本使用\n\n" + text
    if latest(db, artifact).id != version.id:
        text = "> 非最新修订：本文件对应明确选择的历史版本。\n\n" + text
    audit(
        db,
        request,
        p.user,
        "ARTIFACT_EXPORTED",
        artifact,
        changes={"version_id": str(version.id), "mode": mode},
    )
    db.commit()
    filename = (
        re.sub(r"[^\w\u4e00-\u9fff.-]", "_", version.title)[:80]
        + f"-v{version.version_no}-{mode}.md"
    )
    return Response(
        text,
        media_type="text/markdown",
        headers={
            "Content-Disposition": (
                f"attachment; filename=artifact-{artifact.id}-v{version.version_no}.md; "
                f"filename*=UTF-8''{quote(filename)}"
            ),
            "Cache-Control": "no-store",
        },
    )
