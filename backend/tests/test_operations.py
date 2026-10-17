"""运行监控验证脚本。

用途：验证企业隔离、管理员鉴权、5分钟失败阈值和重复事件节流、真实Worker心跳。
命令：RUN_MYSQL_TESTS=1 uv run pytest tests/test_operations.py -q。
前置：独立MySQL迁移到head；事务回滚夹具，调用数据为合成记录，不请求真实模型。
"""

import os
import threading
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from test_identity import sign_in
from test_identity import system as system
from test_model_call_concurrency import isolated as isolated
from test_model_calls import configured as configured

from app.models import AuditEvent, Enterprise, ModelCall, ServiceHeartbeat, utcnow
from app.operations import HttpMetrics, evaluate_alerts, watch_worker


def test_http_metrics_bounded_and_p95():
    metrics = HttpMetrics()
    for n in range(20):
        metrics.record(500 if n == 0 else 200, n)
    assert metrics.view()["p95_ms"] == 18
    assert metrics.view()["error_count"] == 1
    assert metrics.view()["sample_count"] == 20


def test_monitor_admin_only_and_missing_heartbeat(configured):
    client, _, people, _ = configured
    response = client.get("/api/v1/operations/summary")
    assert response.status_code == 200, response.text
    assert "scope" in response.json()["data"]["http"]
    sign_in(client, people["op"])
    assert client.get("/api/v1/operations/summary").status_code == 403


def test_threshold_boundary_and_event_throttling(configured):
    client, db, people, _ = configured
    # 精确选取当前测试账号企业，避免其他受控浏览器数据影响断言。
    from app.models import User

    enterprise = db.get(Enterprise, db.get(User, people["admin"]["id"]).enterprise_id)
    db.refresh(enterprise)
    now = utcnow()
    for index in range(9):
        db.add(
            ModelCall(
                enterprise_id=enterprise.id,
                actor_id=people["admin"]["id"],
                config_id=enterprise.active_model_config_id,
                usage_date=now.date(),
                request_key=uuid4().hex,
                status="FAILED" if index < 2 else "SUCCEEDED",
                finished_at=now,
                deadline_at=now,
                latency_ms=100,
            )
        )
    db.commit()
    evaluate_alerts(db, enterprise.id, now)

    def count():
        return db.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.enterprise_id == enterprise.id,
                AuditEvent.action == "OPS_MODEL_FAILURE_RATE",
            )
        )

    assert count() == 0  # 9个样本不足，不告警。
    db.add(
        ModelCall(
            enterprise_id=enterprise.id,
            actor_id=people["admin"]["id"],
            config_id=enterprise.active_model_config_id,
            usage_date=now.date(),
            request_key=uuid4().hex,
            status="SUCCEEDED",
            finished_at=now,
            deadline_at=now,
        )
    )
    db.commit()
    evaluate_alerts(db, enterprise.id, now)
    evaluate_alerts(db, enterprise.id, now)
    assert count() == 1  # 10个样本中2个失败，且重复扫描不会重复记事件。
    response = client.get("/api/v1/operations/summary").json()["data"]
    assert response["model_last_5_minutes"]["completed"] == 10
    assert response["model_last_5_minutes"]["failure_rate"] == 0.2


def test_worker_stale_heartbeat(configured):
    client, db, _, _ = configured
    now = utcnow()
    db.add(ServiceHeartbeat(instance_id=uuid4().hex, service="worker", seen_at=now))
    db.commit()
    assert client.get("/api/v1/operations/summary").json()["data"]["worker"]["healthy"]
    for row in db.scalars(select(ServiceHeartbeat)):
        row.seen_at = now - timedelta(seconds=31)
    db.commit()
    assert not client.get("/api/v1/operations/summary").json()["data"]["worker"]["healthy"]


@pytest.mark.skipif(os.getenv("RUN_MYSQL_TESTS") != "1", reason="需隔离MySQL")
def test_monitor_persists_heartbeat_without_running_tasks(isolated, monkeypatch):
    _, _, engine, _ = isolated
    stopped = threading.Event()
    instance = uuid4().hex
    monkeypatch.setattr(stopped, "wait", lambda seconds: stopped.set())
    try:
        watch_worker(engine, stopped, instance)
        with Session(engine) as db:
            assert db.get(ServiceHeartbeat, instance).service == "worker"
    finally:
        with Session(engine) as db:
            db.execute(delete(ServiceHeartbeat).where(ServiceHeartbeat.instance_id == instance))
            db.commit()


def test_audit_exact_filters_and_no_secret_in_operations(configured):
    client, _, _, _ = configured
    first = client.get("/api/v1/audit-events").json()["data"]["items"][0]
    response = client.get(
        "/api/v1/audit-events",
        params={
            "request_id": first["request_id"],
            "action": first["action"],
            "target_type": first["target_type"],
            "target_id": first["target_id"],
        },
    )
    assert response.status_code == 200
    assert response.json()["data"]["items"][0]["id"] == first["id"]
    assert client.get("/api/v1/audit-events?request_id=not-exists").json()["data"]["total"] == 0
    text = client.get("/api/v1/operations/summary").text
    for key in ("secret_cipher", "nonce", "api_key", "demo-deepseek-key-not-real"):
        assert key not in text
