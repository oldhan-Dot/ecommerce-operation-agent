"""M2商品资料分析任务验证脚本。

用途：验证快照冻结、幂等、权限、取消、有限重试、租约恢复及草稿原子落库。
命令：backend下 RUN_MYSQL_TESTS=1 uv run pytest tests/test_ai_tasks.py -q。
前置：独立开发/测试MySQL迁移至head；数据库用例外层事务回滚，供应商全部受控。
离线Schema/生成契约用例不需要数据库；不发送真实模型请求或使用真实API Key。
"""

import json
import os
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from test_identity import new_store, sign_in
from test_identity import system as system
from test_model_calls import configured as configured

from app import ai_tasks, model_calls, product_generator
from app.core.errors import AppError
from app.models import (
    AiTask,
    Artifact,
    ArtifactVersion,
    DailyCallUsage,
    InputSnapshot,
    ModelCall,
    Product,
    Store,
    StoreGrant,
    utcnow,
)

mysql = pytest.mark.skipif(os.getenv("RUN_MYSQL_TESTS") != "1", reason="需独立MySQL测试库")
OUTPUT = {
    "title": "商品资料分析",
    "strengths": ["包装诉求可能具有吸引力"],
    "issues": ["目标人群描述需要验证"],
    "actions": ["建议开展用户访谈"],
}


@pytest.fixture
def scenario(configured):
    client, db, people, headers = configured
    store = new_store(client, headers)
    product = Product(
        store_id=int(store["id"]),
        code=uuid4().hex,
        name="原始商品",
        category="食品",
        selling_points="500克包装",
        audience="家庭用户",
    )
    db.add(product)
    db.commit()
    body = {
        "store_id": store["id"],
        "task_type": "PRODUCT_ANALYSIS",
        "input": {"product_id": str(product.id), "goal": "改进商品信息表达"},
    }
    return client, db, people, headers, body


def submit(client, headers, body, key=None):
    return client.post(
        "/api/v1/ai-tasks", headers={**headers, "Idempotency-Key": key or uuid4().hex}, json=body
    )


def detail(client, task_id):
    response = client.get(f"/api/v1/ai-tasks/{task_id}")
    assert response.status_code == 200, response.text
    return response.json()["data"]


def controlled(monkeypatch, db, callback=None):
    async def fake(config, secret, hosts, snapshot):
        assert not db.in_transaction(), "等待供应商时不应持有数据库事务"
        if callback:
            callback()
        return product_generator.Generated(
            12, {"total_tokens": 8}, product_generator.validate_content(OUTPUT, snapshot)
        )

    monkeypatch.setattr(product_generator, "generate", fake)


def execute(db, settings):
    assert ai_tasks.dispatch_task(db, settings)
    assert model_calls.process_model_call(db, settings)


def test_task_schema_rejects_future_types_and_foreign_fields():
    for changes in (
        {"task_type": "VIDEO_SCRIPT"},
        {"input": {"product_id": "1", "goal": "x", "budget": 1}},
    ):
        with pytest.raises(ValidationError):
            ai_tasks.CreateTask.model_validate(
                {
                    "store_id": "1",
                    "task_type": "PRODUCT_ANALYSIS",
                    "input": {"product_id": "1", "goal": "x"},
                    **changes,
                }
            )


@pytest.mark.parametrize(
    "output",
    [{}, {**OUTPUT, "facts": ["销量100万"]}, {**OUTPUT, "actions": []}, {**OUTPUT, "title": " "}],
)
def test_generated_output_strict_schema(output):
    with pytest.raises(AppError) as error:
        product_generator.validate_content(output, {})
    assert error.value.code == "AI_OUTPUT_INVALID"


def test_facts_are_deterministic_snapshot_fields():
    content = product_generator.validate_content(
        OUTPUT,
        {
            "data_copy": {"product": {"name": "保温杯", "selling_points": "容量500毫升"}},
            "missing_items": ["无经营数据"],
        },
    )
    assert content["analysis_mode"] == "PROFILE_ONLY"
    assert content["facts"][1] == {
        "text": "容量500毫升",
        "source_refs": ["D1"],
        "field": "selling_points",
    }
    assert all(item["evidence_type"] == "HYPOTHESIS" for item in content["hypotheses"])
    assert content["metric_refs"] == []


@mysql
def test_immutable_snapshot_and_idempotency(scenario):
    client, db, _, headers, body = scenario
    key = uuid4().hex
    first = submit(client, headers, body, key)
    assert first.status_code == 202, first.text
    task_id = first.json()["data"]["id"]
    db.get(Product, int(body["input"]["product_id"])).name = "后来修改的名称"
    db.commit()
    repeated = submit(client, headers, body, key)
    assert repeated.status_code == 200 and repeated.json()["data"]["id"] == task_id
    state = detail(client, task_id)
    assert state["snapshot"]["data_copy"]["product"]["name"] == "原始商品"
    assert "api_key" not in json.dumps(state)
    changed = {**body, "input": {**body["input"], "goal": "另一个目标"}}
    assert submit(client, headers, changed, key).status_code == 409


@mysql
def test_success_one_draft_and_consumed_call(scenario, monkeypatch):
    client, db, _, headers, body = scenario
    task_id = submit(client, headers, body).json()["data"]["id"]
    controlled(monkeypatch, db)
    execute(db, client.app.state.settings)
    state = detail(client, task_id)
    assert state["status"] == "SUCCEEDED" and state["draft"]["review_status"] == "DRAFT"
    assert state["draft"]["content"]["analysis_mode"] == "PROFILE_ONLY"
    assert not model_calls.process_model_call(db, client.app.state.settings)
    assert not ai_tasks.dispatch_task(db, client.app.state.settings)
    assert (
        db.scalar(
            select(func.count()).select_from(Artifact).where(Artifact.task_id == int(task_id))
        )
        == 1
    )
    assert db.scalar(select(func.count()).select_from(ArtifactVersion)) >= 1
    call = db.scalar(select(ModelCall).where(ModelCall.task_id == int(task_id)))
    usage = db.scalar(
        select(DailyCallUsage).where(DailyCallUsage.enterprise_id == call.enterprise_id)
    )
    assert usage.consumed == 1 and usage.reserved == 0


@mysql
@pytest.mark.parametrize("claimed", [False, True])
def test_cancel_before_send_never_consumes(scenario, monkeypatch, claimed):
    client, db, _, headers, body = scenario
    task_id = submit(client, headers, body).json()["data"]["id"]
    if claimed:
        ai_tasks.dispatch_task(db, client.app.state.settings)
    response = client.post(f"/api/v1/ai-tasks/{task_id}/cancel", headers=headers)
    assert response.status_code == 200 and response.json()["data"]["status"] == "CANCELLED"
    controlled(monkeypatch, db, lambda: pytest.fail("取消的任务不能调用模型"))
    assert not model_calls.process_model_call(db, client.app.state.settings)
    enterprise_id = db.get(Store, int(body["store_id"])).enterprise_id
    counters = db.scalars(
        select(DailyCallUsage).where(DailyCallUsage.enterprise_id == enterprise_id)
    ).all()
    assert all(c.consumed == c.reserved == 0 for c in counters)


@mysql
def test_cancel_during_response_keeps_charge_and_discards_content(scenario, monkeypatch):
    client, db, _, headers, body = scenario
    task_id = submit(client, headers, body).json()["data"]["id"]
    controlled(
        monkeypatch, db, lambda: client.post(f"/api/v1/ai-tasks/{task_id}/cancel", headers=headers)
    )
    execute(db, client.app.state.settings)
    state = detail(client, task_id)
    assert state["status"] == "CANCELLED" and state["artifact_id"] is None
    call = db.scalar(select(ModelCall).where(ModelCall.task_id == int(task_id)))
    assert call.status == "SUCCEEDED"
    usage = db.scalar(
        select(DailyCallUsage).where(DailyCallUsage.enterprise_id == call.enterprise_id)
    )
    assert usage.consumed == 1


@mysql
def test_permission_revoked_before_final_commit_discards(scenario, monkeypatch):
    client, db, _, headers, body = scenario
    task_id = submit(client, headers, body).json()["data"]["id"]

    def revoke():
        db.get(Store, int(body["store_id"])).status = "ARCHIVED"
        db.commit()

    controlled(monkeypatch, db, revoke)
    execute(db, client.app.state.settings)
    state = detail(client, task_id)
    assert state["status"] == "FAILED" and state["error_code"] == "AI_PERMISSION_REVOKED"
    assert state["artifact_id"] is None


@mysql
@pytest.mark.parametrize("claimed", [False, True])
def test_queue_and_lease_recovery(scenario, claimed):
    client, db, _, headers, body = scenario
    task_id = submit(client, headers, body).json()["data"]["id"]
    if claimed:
        ai_tasks.dispatch_task(db, client.app.state.settings)
    task = db.get(AiTask, int(task_id))
    if claimed:
        task.lease_until = utcnow() - timedelta(seconds=1)
    else:
        task.queued_at = utcnow() - timedelta(minutes=11)
    db.commit()
    assert ai_tasks.dispatch_task(db, client.app.state.settings)
    state = detail(client, task_id)
    assert state["status"] == "FAILED"
    assert state["error_code"] == ("AI_INTERRUPTED" if claimed else "AI_QUEUE_TIMEOUT")
    if claimed:
        assert model_calls.process_model_call(db, client.app.state.settings)
        usage = db.scalar(
            select(DailyCallUsage).where(DailyCallUsage.enterprise_id == task.enterprise_id)
        )
        assert usage.consumed == usage.reserved == 0


@mysql
def test_explicit_retry_root_limit_and_snapshot(scenario, monkeypatch):
    client, db, _, headers, body = scenario
    original = submit(client, headers, body).json()["data"]["id"]

    async def fail(*args):
        raise AppError(502, "AI_OUTPUT_INVALID", "controlled")

    monkeypatch.setattr(product_generator, "generate", fail)
    execute(db, client.app.state.settings)
    assert detail(client, original)["status"] == "FAILED"
    for expected in (1, 2):
        key = uuid4().hex
        response = client.post(
            f"/api/v1/ai-tasks/{original}/retries", headers={**headers, "Idempotency-Key": key}
        )
        assert response.status_code == 202, response.text
        child_id = response.json()["data"]["id"]
        repeated = client.post(
            f"/api/v1/ai-tasks/{original}/retries", headers={**headers, "Idempotency-Key": key}
        )
        assert repeated.status_code == 200 and repeated.json()["data"]["id"] == child_id
        assert response.json()["data"]["retry_count"] == expected
        assert detail(client, child_id)["snapshot"] == detail(client, original)["snapshot"]
    exhausted = client.post(
        f"/api/v1/ai-tasks/{original}/retries", headers={**headers, "Idempotency-Key": uuid4().hex}
    )
    assert (
        exhausted.status_code == 409 and exhausted.json()["error"]["code"] == "AI_RETRY_EXHAUSTED"
    )


@mysql
def test_snapshot_tamper_fails_before_network(scenario, monkeypatch):
    client, db, _, headers, body = scenario
    task_id = submit(client, headers, body).json()["data"]["id"]
    snapshot = db.scalar(select(InputSnapshot).where(InputSnapshot.task_id == int(task_id)))
    snapshot.data_copy = {"product": {"name": "篡改"}}
    db.commit()
    controlled(monkeypatch, db, lambda: pytest.fail("篡改快照不能发送"))
    execute(db, client.app.state.settings)
    state = detail(client, task_id)
    assert state["status"] == "FAILED" and state["error_code"] == "AI_SNAPSHOT_INVALID"


@mysql
def test_operator_author_and_store_scope(scenario):
    client, db, people, headers, body = scenario
    admin_task = submit(client, headers, body).json()["data"]["id"]
    op_headers = sign_in(client, people["op"])
    assert client.get(f"/api/v1/ai-tasks/{admin_task}").status_code == 404
    assert submit(client, op_headers, body).status_code == 404
    db.add(
        StoreGrant(
            user_id=people["op"]["id"],
            store_id=int(body["store_id"]),
            granted_by=people["admin"]["id"],
        )
    )
    db.commit()
    created = submit(client, op_headers, body)
    assert created.status_code == 202
    assert client.get(f"/api/v1/ai-tasks/{admin_task}").status_code == 404
    other_headers = sign_in(client, people["supervisor"])
    assert client.get(f"/api/v1/ai-tasks/{admin_task}").status_code == 200
    assert (
        client.post(f"/api/v1/ai-tasks/{admin_task}/cancel", headers=other_headers).status_code
        == 200
    )


@mysql
def test_final_write_failure_rolls_back_draft_and_never_resends(scenario, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    client, db, _, headers, body = scenario
    task_id = int(submit(client, headers, body).json()["data"]["id"])
    controlled(monkeypatch, db)
    original = ai_tasks.finish_task

    def failed_commit(*args):
        original(*args)
        raise SQLAlchemyError("controlled final transaction failure")

    monkeypatch.setattr(ai_tasks, "finish_task", failed_commit)
    assert ai_tasks.dispatch_task(db, client.app.state.settings)
    with pytest.raises(SQLAlchemyError):
        model_calls.process_model_call(db, client.app.state.settings)
    db.rollback()
    assert db.scalar(select(Artifact).where(Artifact.task_id == task_id)) is None
    call = db.scalar(select(ModelCall).where(ModelCall.task_id == task_id))
    assert call.status == "STARTED"
    call.deadline_at = utcnow() - timedelta(seconds=1)
    db.commit()
    controlled(monkeypatch, db, lambda: pytest.fail("最终写入失败后不能自动重发"))
    assert model_calls.process_model_call(db, client.app.state.settings)
    assert detail(client, task_id)["status"] == "FAILED"
    db.refresh(call)
    assert call.status == "UNKNOWN"


@mysql
def test_heartbeat_requires_live_fence(scenario):
    client, db, _, headers, body = scenario
    task_id = int(submit(client, headers, body).json()["data"]["id"])
    ai_tasks.dispatch_task(db, client.app.state.settings)
    task = db.get(AiTask, task_id)
    task.deadline_at = utcnow() + timedelta(seconds=60)
    task.lease_until = utcnow() + timedelta(seconds=15)
    db.commit()
    assert model_calls.renew_task(db.get_bind(), task_id, task.enterprise_id, task.fence_token)
    db.refresh(task)
    assert task.lease_until > utcnow() + timedelta(seconds=20)
    assert not model_calls.renew_task(
        db.get_bind(), task_id, task.enterprise_id, task.fence_token + 1
    )
