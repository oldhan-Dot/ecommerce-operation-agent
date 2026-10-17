"""M2任务多连接故障验证脚本。

用途：真实MySQL独立连接验证双派发、发送后取消和失租围栏，确保单次发送及无迟到草稿。
命令：backend下 RUN_MYSQL_TESTS=1 uv run pytest tests/test_ai_task_concurrency.py -q。
前置：APP_ENV=test，本机13306/eoa_test_*隔离库已迁移；复用带严格边界及企业清理的isolated夹具。
只有供应商响应受控，API/事务真实执行，结束后只清理该测试企业的数据，不访问云库。
"""

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_ai_tasks import OUTPUT
from test_identity import new_store
from test_model_call_concurrency import isolated as isolated

from app import ai_tasks, model_calls, product_generator
from app.models import AiTask, Artifact, ModelCall, Product, utcnow

pytestmark = pytest.mark.skipif(os.getenv("RUN_MYSQL_TESTS") != "1", reason="需独立MySQL")


@pytest.fixture
def queued(isolated):
    client, headers, engine, eid = isolated
    store = new_store(client, headers)
    with Session(engine, expire_on_commit=False) as db:
        product = Product(
            store_id=int(store["id"]),
            code=uuid4().hex,
            name="并发商品",
            category="食品",
            selling_points="商品卖点",
        )
        db.add(product)
        db.commit()
        pid = product.id
    response = client.post(
        "/api/v1/ai-tasks",
        headers={**headers, "Idempotency-Key": uuid4().hex},
        json={
            "task_type": "PRODUCT_ANALYSIS",
            "store_id": store["id"],
            "input": {"product_id": str(pid), "goal": "改进资料"},
        },
    )
    assert response.status_code == 202, response.text
    try:
        yield client, headers, engine, eid, int(response.json()["data"]["id"])
    finally:
        # 先删除本夹具的任务/来源引用，再让isolated夹具删除企业主记录。
        from sqlalchemy import delete

        from app.models import ArtifactVersion, AuditEvent, InputSnapshot, Store

        with Session(engine) as db:
            db.execute(delete(AuditEvent).where(AuditEvent.enterprise_id == eid))
            db.execute(delete(ModelCall).where(ModelCall.enterprise_id == eid))
            db.execute(
                delete(ArtifactVersion).where(
                    ArtifactVersion.artifact_id.in_(
                        select(Artifact.id).where(Artifact.enterprise_id == eid)
                    )
                )
            )
            db.execute(delete(Artifact).where(Artifact.enterprise_id == eid))
            db.execute(
                delete(InputSnapshot).where(
                    InputSnapshot.task_id.in_(select(AiTask.id).where(AiTask.enterprise_id == eid))
                )
            )
            db.execute(delete(AiTask).where(AiTask.enterprise_id == eid))
            db.execute(delete(Product).where(Product.store_id == int(store["id"])))
            db.execute(delete(Store).where(Store.id == int(store["id"])))
            db.commit()


def test_two_dispatchers_reserve_once(queued):
    client, _, engine, eid, task_id = queued
    barrier = threading.Barrier(2)

    def dispatch(index):
        barrier.wait(timeout=5)
        with Session(engine, expire_on_commit=False) as db:
            return ai_tasks.dispatch_task(db, client.app.state.settings)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(dispatch, range(2)))
    assert sorted(outcomes) == [False, True]
    with Session(engine) as db:
        calls = db.scalars(select(ModelCall).where(ModelCall.task_id == task_id)).all()
        assert len(calls) == 1 and calls[0].status == "RESERVED"


@pytest.mark.parametrize("interrupt", ["cancel", "lease"])
def test_inflight_interruption_holds_slot_and_discards_late_result(queued, monkeypatch, interrupt):
    client, headers, engine, eid, task_id = queued
    with Session(engine, expire_on_commit=False) as db:
        assert ai_tasks.dispatch_task(db, client.app.state.settings)
    started, release = threading.Event(), threading.Event()
    sent = []

    async def fake(config, secret, hosts, snapshot):
        sent.append(1)
        started.set()
        assert release.wait(timeout=10)
        return product_generator.Generated(
            1, {}, product_generator.validate_content(OUTPUT, snapshot)
        )

    monkeypatch.setattr(product_generator, "generate", fake)

    def worker():
        with Session(engine, expire_on_commit=False) as db:
            return model_calls.process_model_call(db, client.app.state.settings)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker)
        try:
            assert started.wait(timeout=10)
            if interrupt == "cancel":
                assert (
                    client.post(f"/api/v1/ai-tasks/{task_id}/cancel", headers=headers).status_code
                    == 200
                )
            else:
                with Session(engine, expire_on_commit=False) as db:
                    db.get(AiTask, task_id).lease_until = utcnow() - timedelta(seconds=1)
                    db.commit()
                    assert ai_tasks.dispatch_task(db, client.app.state.settings)
            with Session(engine) as db:
                call = db.scalar(select(ModelCall).where(ModelCall.task_id == task_id))
                assert call.status == "STARTED", "取消/失租不能提前释放仍在途调用槽"
        finally:
            release.set()
        assert future.result(timeout=10)
    with Session(engine) as db:
        task = db.get(AiTask, task_id)
        assert task.status == ("CANCELLED" if interrupt == "cancel" else "FAILED")
        assert db.scalar(select(Artifact).where(Artifact.task_id == task_id)) is None
        assert (
            db.scalar(select(ModelCall).where(ModelCall.task_id == task_id)).status == "SUCCEEDED"
        )
    assert len(sent) == 1
