"""M2 多连接并发验证脚本。

用途：真实 MySQL 独立事务验证同键双提交、额度争抢和双 Worker 认领只发送一次。
运行：backend 下 RUN_MYSQL_TESTS=1 uv run pytest tests/test_model_call_concurrency.py -q。
前置：专属 eoa_test_* 本机13306隔离库已迁移到head，APP_ENV=test；拒绝云端/生产配置。
隔离：创建独立企业后提交供线程访问，finally 删除该企业测试数据；供应商调用全部模拟。
"""

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session
from test_identity import ORIGIN, PASSWORD, sign_in
from test_model_settings import body, configure_app

from app import model_calls
from app.core.config import get_settings
from app.core.db import make_engine
from app.core.security import hasher
from app.main import create_app
from app.model_provider import ProbeResult
from app.models import (
    AiTask,
    Artifact,
    ArtifactVersion,
    AuditEvent,
    AuthSession,
    DailyCallUsage,
    Enterprise,
    InputSnapshot,
    ModelCall,
    ModelConfig,
    SchedulerState,
    User,
    utcnow,
)

pytestmark = pytest.mark.skipif(os.getenv("RUN_MYSQL_TESTS") != "1", reason="需隔离MySQL")


@pytest.fixture
def isolated():
    settings = get_settings()
    assert settings.app_env == "test" and settings.db_host == "127.0.0.1"
    assert settings.db_port == 13306 and settings.db_name.startswith("eoa_test_")
    engine = make_engine(settings)
    with Session(engine, expire_on_commit=False) as db:
        enterprise = Enterprise(name="concurrent-" + uuid4().hex)
        db.add(enterprise)
        db.flush()
        user = User(
            enterprise_id=enterprise.id,
            login_name=uuid4().hex,
            role="ADMIN",
            password_hash=hasher.hash(PASSWORD),
            must_change_password=False,
        )
        db.add(user)
        db.commit()
        eid, uid, login = enterprise.id, user.id, user.login_name
    app = create_app(settings)
    with TestClient(app, headers={"Origin": ORIGIN}) as client:
        configure_app(client)
        headers = sign_in(client, {"login_name": login})
        response = client.put(
            "/api/v1/settings/model",
            headers={**headers, "If-Match": '"0"'},
            json=body(daily_limit=1),
        )
        assert response.status_code == 200, response.text
        try:
            yield client, headers, engine, eid
        finally:
            # 仅删除本夹具创建的企业及账号，绝不清空整表。
            with Session(engine) as db:
                db.execute(delete(AuditEvent).where(AuditEvent.enterprise_id == eid))
                db.execute(delete(ModelCall).where(ModelCall.enterprise_id == eid))
                artifact_ids = select(Artifact.id).where(Artifact.enterprise_id == eid)
                db.execute(
                    delete(ArtifactVersion).where(ArtifactVersion.artifact_id.in_(artifact_ids))
                )
                db.execute(delete(Artifact).where(Artifact.enterprise_id == eid))
                task_ids = select(AiTask.id).where(AiTask.enterprise_id == eid)
                db.execute(delete(InputSnapshot).where(InputSnapshot.task_id.in_(task_ids)))
                db.execute(
                    update(AiTask)
                    .where(AiTask.enterprise_id == eid)
                    .values(root_task_id=None, parent_task_id=None)
                )
                db.execute(delete(AiTask).where(AiTask.enterprise_id == eid))
                db.execute(delete(DailyCallUsage).where(DailyCallUsage.enterprise_id == eid))
                db.execute(delete(SchedulerState).where(SchedulerState.enterprise_id == eid))
                db.get(Enterprise, eid).active_model_config_id = None
                db.flush()
                db.execute(delete(ModelConfig).where(ModelConfig.enterprise_id == eid))
                db.execute(delete(AuthSession).where(AuthSession.user_id == uid))
                db.execute(delete(User).where(User.id == uid))
                db.execute(delete(Enterprise).where(Enterprise.id == eid))
                db.commit()
            engine.dispose()


@pytest.mark.parametrize("same_key", [True, False])
def test_two_real_transactions_reserve_once(isolated, same_key):
    client, headers, engine, eid = isolated
    barrier = threading.Barrier(2)
    key = uuid4().hex

    def submit(index):
        barrier.wait(timeout=5)
        return client.post(
            "/api/v1/settings/model/test",
            json={"config_version": 1},
            headers={**headers, "Idempotency-Key": key if same_key else uuid4().hex},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, range(2)))
    assert sorted(r.status_code for r in responses) == ([200, 202] if same_key else [202, 429])
    with Session(engine) as db:
        calls = db.scalars(select(ModelCall).where(ModelCall.enterprise_id == eid)).all()
        usage = db.scalar(select(DailyCallUsage).where(DailyCallUsage.enterprise_id == eid))
        assert len(calls) == 1 and usage.reserved == 1 and usage.consumed == 0


@pytest.mark.parametrize("expire", [False, True])
def test_two_workers_only_one_provider_send(isolated, monkeypatch, expire):
    client, headers, engine, eid = isolated
    assert (
        client.post(
            "/api/v1/settings/model/test",
            json={"config_version": 1},
            headers={**headers, "Idempotency-Key": uuid4().hex},
        ).status_code
        == 202
    )
    started, release = threading.Event(), threading.Event()
    sends = []

    async def fake(*args):
        sends.append(1)
        started.set()
        assert release.wait(timeout=10)
        return ProbeResult(1, {})

    monkeypatch.setattr(model_calls, "probe", fake)

    def worker():
        with Session(engine, expire_on_commit=False) as db:
            return model_calls.process_model_call(db, client.app.state.settings)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(worker)
        try:
            assert started.wait(timeout=10)
            # 第一条调用仍在网络阶段，第二个Worker必须看见STARTED并且不重发。
            if expire:
                with Session(engine) as db:
                    call = db.scalar(select(ModelCall).where(ModelCall.enterprise_id == eid))
                    call.deadline_at = utcnow() - timedelta(seconds=1)
                    db.commit()
            assert pool.submit(worker).result(timeout=5) is expire
        finally:
            release.set()
        assert first.result(timeout=10)
    assert len(sends) == 1
    with Session(engine) as db:
        call = db.scalar(select(ModelCall).where(ModelCall.enterprise_id == eid))
        # 过期后已被另一个Worker恢复为UNKNOWN，迟到的成功不得覆盖围栏。
        assert call.status == ("UNKNOWN" if expire else "SUCCEEDED")
