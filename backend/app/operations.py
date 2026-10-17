"""运行观测：不记录正文或凭据，按企业聚合调用并生成节流运维审计事件。"""

import json
import threading
from collections import deque
from datetime import timedelta
from math import ceil
from time import monotonic
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.dependencies import administrator, get_db
from app.identity import result, stamp
from app.model_calls import lock_enterprise
from app.models import (
    AiTask,
    AuditEvent,
    Enterprise,
    ImportBatch,
    ModelCall,
    ServiceHeartbeat,
    utcnow,
)

router = APIRouter(prefix="/api/v1/operations", tags=["operations"])


class HttpMetrics:
    """有界进程内5分钟窗口；多API副本由生产日志采集器汇总，不能误当全局指标。"""

    def __init__(self):
        self.items = deque(maxlen=10000)
        self.lock = threading.Lock()

    def record(self, status, elapsed_ms):
        with self.lock:
            self.items.append((monotonic(), status, elapsed_ms))

    def view(self):
        with self.lock:
            items = [i for i in self.items if i[0] > monotonic() - 300]
        latencies = sorted(i[2] for i in items)
        return {
            "sample_count": len(items),
            "error_count": sum(i[1] >= 500 for i in items),
            "p95_ms": round(latencies[ceil(len(items) * 0.95) - 1], 2) if items else None,
            "scope": "CURRENT_API_PROCESS_LAST_5_MIN_MAX_10000",
        }


def model_summary(db, eid, now):
    query = select(ModelCall).where(
        ModelCall.enterprise_id == eid,
        ModelCall.finished_at >= now - timedelta(minutes=5),
        ModelCall.status.in_(["SUCCEEDED", "FAILED", "UNKNOWN"]),
    )
    rows = db.scalars(query).all()
    failures = sum(c.status != "SUCCEEDED" for c in rows)
    queued, oldest = db.execute(
        select(func.count(), func.min(AiTask.queued_at)).where(
            AiTask.enterprise_id == eid, AiTask.status == "QUEUED"
        )
    ).one()
    latencies = sorted(c.latency_ms for c in rows if c.latency_ms is not None)
    return {
        "completed": len(rows),
        "failed": failures,
        "failure_rate": failures / len(rows) if rows else None,
        "timeouts": sum(c.error_code == "MODEL_TIMEOUT" for c in rows),
        "invalid_outputs": sum(c.error_code == "AI_OUTPUT_INVALID" for c in rows),
        "p95_ms": latencies[ceil(len(latencies) * 0.95) - 1] if latencies else None,
        "tokens": sum((c.usage_tokens or {}).get("total_tokens", 0) or 0 for c in rows),
        "queue_length": queued,
        "oldest_wait_seconds": max(0, int((now - oldest).total_seconds())) if oldest else 0,
        "running_calls": db.scalar(
            select(func.count())
            .select_from(ModelCall)
            .where(ModelCall.enterprise_id == eid, ModelCall.status == "STARTED")
        ),
    }


def evaluate_alerts(db, eid, now):
    """企业锁保护多监测器去重；相同异常5分钟最多记录一条，不发送外部消息。"""
    lock_enterprise(db, eid)
    data = model_summary(db, eid, now)
    actions = []
    if data["completed"] >= 10 and data["failed"] * 5 >= data["completed"]:
        actions.append("OPS_MODEL_FAILURE_RATE")
    if data["oldest_wait_seconds"] >= 540:
        actions.append("OPS_QUEUE_WAIT")
    for action in actions:
        recent = db.scalar(
            select(AuditEvent.id)
            .where(
                AuditEvent.enterprise_id == eid,
                AuditEvent.action == action,
                AuditEvent.created_at >= now - timedelta(minutes=5),
            )
            .limit(1)
        )
        if not recent:
            db.add(
                AuditEvent(
                    enterprise_id=eid,
                    action=action,
                    target_type="operations",
                    request_id=uuid4().hex,
                    result="WARNING",
                    redacted_diff=data,
                    created_at=now,
                )
            )
            print(json.dumps({"event": action, "enterprise_id": str(eid), **data}), flush=True)
    db.commit()


def watch_worker(engine, stopped, instance):
    """独立短连接心跳，不受模型HTTP等待阻塞；数据库故障仅输出脱敏运维事件。"""
    previous = None
    while not stopped.is_set():
        try:
            with Session(engine) as db:
                now = utcnow()
                statement = insert(ServiceHeartbeat).values(
                    instance_id=instance, service="worker", seen_at=now
                )
                db.execute(statement.on_duplicate_key_update(seen_at=now))
                db.execute(
                    delete(ServiceHeartbeat).where(
                        ServiceHeartbeat.seen_at < now - timedelta(days=1)
                    )
                )
                db.commit()
                ids = db.scalars(select(Enterprise.id)).all()
                db.commit()
                for eid in ids:
                    evaluate_alerts(db, eid, now)
            previous = None
        except SQLAlchemyError:
            if previous != "DB_UNAVAILABLE":
                print(
                    json.dumps({"event": "OPS_DB_UNAVAILABLE", "service": "worker_monitor"}),
                    flush=True,
                )
            previous = "DB_UNAVAILABLE"
        stopped.wait(10)


@router.get("/summary")
def summary(request: Request, p=Depends(administrator), db=Depends(get_db)):
    now = utcnow()
    heartbeat = db.scalar(
        select(func.max(ServiceHeartbeat.seen_at)).where(ServiceHeartbeat.service == "worker")
    )
    data = model_summary(db, p.user.enterprise_id, now)
    imports = db.scalars(
        select(ImportBatch).where(
            ImportBatch.enterprise_id == p.user.enterprise_id,
            ImportBatch.finished_at >= now - timedelta(minutes=5),
        )
    ).all()
    alerts = db.scalars(
        select(AuditEvent)
        .where(AuditEvent.enterprise_id == p.user.enterprise_id, AuditEvent.action.like("OPS_%"))
        .order_by(AuditEvent.id.desc())
        .limit(20)
    ).all()
    return result(
        request,
        {
            "model_last_5_minutes": data,
            "imports_last_5_minutes": {
                "completed": len(imports),
                "max_duration_seconds": max(
                    ((b.finished_at - b.created_at).total_seconds() for b in imports), default=None
                ),
            },
            "http": request.app.state.http_metrics.view(),
            "worker": {
                "last_seen": stamp(heartbeat),
                "healthy": bool(heartbeat and heartbeat >= now - timedelta(seconds=30)),
            },
            "alerts": [
                {"code": e.action, "created_at": stamp(e.created_at), "request_id": e.request_id}
                for e in alerts
            ],
            "pool": {
                "checked_out": db.get_bind().engine.pool.checkedout(),
                "size": db.get_bind().engine.pool.size(),
            },
        },
    )
