"""M3 产出审核集成验证脚本。

用途：验证不可变修订、条件写入、审核权限、幂等、历史正式导出和撤权。
命令：RUN_MYSQL_TESTS=1 uv run pytest tests/test_artifacts.py -q。
前置：独立MySQL迁移至head；复用事务回滚夹具，模型响应受控，不调用真实供应商。
"""

import copy
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from test_ai_tasks import controlled, execute, submit
from test_ai_tasks import scenario as scenario
from test_identity import sign_in
from test_identity import system as system
from test_model_calls import configured as configured

from app.models import Artifact, ArtifactReview, ArtifactVersion, StoreGrant


@pytest.fixture
def draft(scenario, monkeypatch):
    client, db, people, headers, body = scenario
    controlled(monkeypatch, db)
    response = submit(client, headers, body)
    assert response.status_code == 202, response.text
    execute(db, client.app.state.settings)
    artifact = db.scalar(
        select(Artifact).where(Artifact.task_id == int(response.json()["data"]["id"]))
    )
    response = client.get(f"/api/v1/artifacts/{artifact.id}")
    assert response.status_code == 200, response.text
    return client, db, people, headers, response.json()["data"]


def act(client, headers, version, action, body=None, key=None):
    return client.post(
        f"/api/v1/artifact-versions/{version['id']}/{action}",
        headers={
            **headers,
            "If-Match": f'"{version["version"]}"',
            "Idempotency-Key": key or uuid4().hex,
        },
        json=body,
    )


def save(client, headers, artifact, content=None):
    return client.post(
        f"/api/v1/artifacts/{artifact['id']}/versions",
        headers={**headers, "If-Match": f'"{artifact["version"]}"'},
        json={
            "base_version_id": artifact["latest"]["id"],
            "title": "修订后的分析",
            "content": content or artifact["latest"]["content"],
        },
    )


def get(client, artifact):
    return client.get(f"/api/v1/artifacts/{artifact['id']}").json()["data"]


def test_revision_is_immutable_and_conflict_never_overwrites(draft):
    client, db, _, headers, artifact = draft
    old = copy.deepcopy(artifact["latest"])
    response = save(client, headers, artifact)
    assert response.status_code == 201, response.text
    assert response.json()["data"]["version_no"] == 2
    assert save(client, headers, artifact).status_code == 409
    history = client.get(f"/api/v1/artifacts/{artifact['id']}/versions").json()["data"]
    assert history["total"] == 2
    db.expire_all()
    assert db.get(ArtifactVersion, int(old["id"])).content == old["content"]


def test_pending_lock_withdraw_and_self_review_forbidden(draft):
    client, _, _, headers, artifact = draft
    pending = act(client, headers, artifact["latest"], "submit")
    assert pending.status_code == 200, pending.text
    version = pending.json()["data"]
    assert save(client, headers, get(client, artifact)).status_code == 409
    assert act(client, headers, version, "reviews", {"decision": "APPROVE"}).status_code == 403
    response = act(client, headers, version, "withdraw")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["review_status"] == "DRAFT"
    assert save(client, headers, get(client, artifact)).status_code == 201


def test_approval_idempotency_and_historical_export(draft):
    client, db, people, headers, artifact = draft
    pending = act(client, headers, artifact["latest"], "submit").json()["data"]
    review_headers = sign_in(client, people["supervisor"])
    key = uuid4().hex
    response = act(client, review_headers, pending, "reviews", {"decision": "APPROVE"}, key)
    assert response.status_code == 200, response.text
    approved = response.json()["data"]
    replay = act(client, review_headers, pending, "reviews", {"decision": "APPROVE"}, key)
    assert replay.json()["data"] == approved
    assert (
        act(
            client,
            review_headers,
            pending,
            "reviews",
            {"decision": "REJECT", "comment": "修改"},
            key,
        ).status_code
        == 409
    )
    assert db.scalar(select(func.count()).select_from(ArtifactReview)) == 1
    headers = sign_in(client, people["admin"])
    assert save(client, headers, get(client, artifact)).status_code == 201
    url = f"/api/v1/artifact-versions/{approved['id']}/export?mode=official"
    response = client.get(url)
    assert response.status_code == 200, response.text
    assert "非最新修订" in response.text and "AI辅助生成" in response.text
    assert response.headers["cache-control"] == "no-store"
    newest = get(client, artifact)["latest"]
    assert newest["review_status"] == "DRAFT"
    assert (
        client.get(f"/api/v1/artifact-versions/{newest['id']}/export?mode=official").status_code
        == 409
    )


def test_rejection_requires_comment_and_new_revision(draft):
    client, _, people, headers, artifact = draft
    pending = act(client, headers, artifact["latest"], "submit").json()["data"]
    review_headers = sign_in(client, people["supervisor"])
    assert (
        act(
            client, review_headers, pending, "reviews", {"decision": "REJECT", "comment": " "}
        ).status_code
        == 422
    )
    response = act(
        client, review_headers, pending, "reviews", {"decision": "REJECT", "comment": "补充建议"}
    )
    assert response.status_code == 200, response.text
    headers = sign_in(client, people["admin"])
    rejected = response.json()["data"]
    assert act(client, headers, rejected, "submit").status_code == 409
    assert save(client, headers, get(client, artifact)).status_code == 201


def test_operator_scope_and_revocation(draft):
    client, db, people, headers, artifact = draft
    db.add(
        StoreGrant(
            user_id=people["op"]["id"],
            store_id=int(artifact["store_id"]),
            granted_by=people["admin"]["id"],
        )
    )
    db.commit()
    sign_in(client, people["op"])
    assert client.get(f"/api/v1/artifacts/{artifact['id']}").status_code == 404
    assert client.get("/api/v1/artifacts").json()["data"]["total"] == 0
    # 把测试产出归于运营人员，以真实读取范围检查撤权后下载失效。
    row = db.get(Artifact, int(artifact["id"]))
    row.creator_id = people["op"]["id"]
    db.commit()
    url = f"/api/v1/artifact-versions/{artifact['latest']['id']}/export"
    assert client.get(url).status_code == 200
    grant = db.scalar(select(StoreGrant).where(StoreGrant.user_id == people["op"]["id"]))
    db.delete(grant)
    db.commit()
    assert client.get(url).status_code == 404


def test_edit_cannot_forge_facts_and_export_escapes_html(draft):
    client, _, _, headers, artifact = draft
    content = copy.deepcopy(artifact["latest"]["content"])
    content["facts"][0]["text"] = "伪造事实"
    assert save(client, headers, artifact, content).status_code == 422
    content = copy.deepcopy(artifact["latest"]["content"])
    content["payload"]["actions"] = ['<script>alert("x")</script> ![图](https://example.com)']
    response = save(client, headers, artifact, content)
    assert response.status_code == 201, response.text
    exported = client.get(f"/api/v1/artifact-versions/{response.json()['data']['id']}/export")
    assert "<script>" not in exported.text
    assert "&lt;script&gt;" in exported.text and "未审核草稿副本" in exported.text
