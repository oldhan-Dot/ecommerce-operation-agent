"""M3 多连接审核竞争验证脚本。

用途：验证两个独立请求竞争审核只能成功一次，审核记录和状态不会被覆盖。
命令：RUN_MYSQL_TESTS=1 uv run pytest tests/test_artifact_concurrency.py -q。
前置：本机13306/eoa_test_*独立库迁移到head；测试模型受控，专属企业数据结束清理。
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from test_ai_task_concurrency import queued as queued
from test_ai_tasks import OUTPUT
from test_identity import PASSWORD, sign_in
from test_model_call_concurrency import isolated as isolated

from app import ai_tasks, model_calls, product_generator
from app.core.security import hasher
from app.models import (
    Artifact,
    ArtifactAction,
    ArtifactReview,
    ArtifactVersion,
    AuditEvent,
    AuthSession,
    User,
)


def test_two_reviewers_cannot_override_decision(queued, monkeypatch):
    client, headers, engine, eid, tid = queued

    async def fake(config, secret, hosts, snapshot):
        return product_generator.Generated(
            1, {}, product_generator.validate_content(OUTPUT, snapshot)
        )

    monkeypatch.setattr(product_generator, "generate", fake)
    with Session(engine, expire_on_commit=False) as db:
        assert ai_tasks.dispatch_task(db, client.app.state.settings)
        assert model_calls.process_model_call(db, client.app.state.settings)
        artifact = db.scalar(select(Artifact).where(Artifact.task_id == tid))
        version = db.scalar(
            select(ArtifactVersion).where(ArtifactVersion.artifact_id == artifact.id)
        )
        vid = version.id
        person = User(
            enterprise_id=eid,
            login_name=uuid4().hex,
            role="SUPERVISOR",
            password_hash=hasher.hash(PASSWORD),
            must_change_password=False,
        )
        db.add(person)
        db.commit()
        uid, login = person.id, person.login_name
    try:
        response = client.post(
            f"/api/v1/artifact-versions/{vid}/submit",
            headers={**headers, "If-Match": '"1"', "Idempotency-Key": uuid4().hex},
        )
        assert response.status_code == 200, response.text
        reviewer_headers = sign_in(client, {"login_name": login})
        barrier = threading.Barrier(2)

        def review(decision):
            barrier.wait(timeout=5)
            return client.post(
                f"/api/v1/artifact-versions/{vid}/reviews",
                headers={**reviewer_headers, "If-Match": '"2"', "Idempotency-Key": uuid4().hex},
                json={"decision": decision, "comment": "并发验证"},
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(review, ["APPROVE", "REJECT"]))
        assert sorted(r.status_code for r in responses) == [200, 409]
        with Session(engine) as db:
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(ArtifactReview)
                    .where(ArtifactReview.version_id == vid)
                )
                == 1
            )
    finally:
        with Session(engine) as db:
            db.execute(delete(AuditEvent).where(AuditEvent.actor_id == uid))
            db.execute(delete(ArtifactAction).where(ArtifactAction.version_id == vid))
            db.execute(delete(ArtifactReview).where(ArtifactReview.version_id == vid))
            db.get(ArtifactVersion, vid).reviewer_id = None
            db.flush()
            db.execute(delete(AuthSession).where(AuthSession.user_id == uid))
            db.execute(delete(User).where(User.id == uid))
            db.commit()
