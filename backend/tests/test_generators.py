"""五类生成器契约和来源验证脚本。

用途：验证数量、来源、时间轴、禁词、预算、渠道、指标引用与跨店铺来源阻断。
命令：backend下 RUN_MYSQL_TESTS=1 uv run pytest tests/test_generators.py -q。
前置：开发/测试MySQL已迁移至head，数据库用例外层事务回滚；模型响应全部受控。
纯输出校验无需数据库；不访问真实模型、不使用真实Key、不产生费用。
"""

import asyncio
import json
import os
from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError
from test_ai_tasks import execute, submit
from test_ai_tasks import scenario as scenario
from test_identity import system as system
from test_model_calls import configured as configured
from test_model_provider import SECRET, config
from test_model_provider import wire as wire

from app import ai_tasks, generators
from app.core.errors import AppError
from app.daily_data import business_today
from app.models import Competitor, StoreDaily

mysql = pytest.mark.skipif(os.getenv("RUN_MYSQL_TESTS") != "1", reason="需独立MySQL")
IDEA = {
    "name": "主题",
    "insight": "人群需求假设",
    "angle": "资料视角",
    "direction": "建议内容",
    "cta": "了解商品",
}
SHOT = {
    "start": 0,
    "end": 15,
    "visual": "展示包装",
    "voiceover": "了解商品",
    "subtitle": "商品资料",
    "props": [],
}


def saved(kind, params=None):
    return {
        "input_params": params or {},
        "data_copy": {
            "task_type": kind,
            "products": [{"ref": "P1", "product": {"name": "商品"}}],
            "competitors": [{"ref": "C1", "price_comparable": False, "name": "竞品"}],
        },
        "metrics": {},
        "missing_items": [],
    }


@pytest.mark.parametrize("count", [1, 5, 10])
def test_theme_exact_count(count):
    output = {"title": "创意", "ideas": [IDEA] * count}
    content = generators.validate_output(output, saved("THEME_IDEAS", {"count": count}))
    assert len(content["payload"]["ideas"]) == count
    with pytest.raises(AppError):
        generators.validate_output(output, saved("THEME_IDEAS", {"count": count + 1}))


@pytest.mark.parametrize(
    "shots",
    [
        [{**SHOT, "start": 1}],
        [{**SHOT, "end": 14}],
        [{**SHOT, "end": 10}, {**SHOT, "start": 9, "end": 15}],
        [{**SHOT, "end": 10}, {**SHOT, "start": 11, "end": 15}],
    ],
)
def test_video_rejects_gaps_overlaps_or_wrong_duration(shots):
    with pytest.raises(AppError):
        generators.validate_output(
            {"title": "脚本", "shots": shots, "closing": "结束"},
            saved("VIDEO_SCRIPT", {"duration_seconds": 15, "forbidden_terms": []}),
        )


def test_video_forbidden_words_casefold_and_valid_timing():
    data = saved("VIDEO_SCRIPT", {"duration_seconds": 15, "forbidden_terms": ["BAD"]})
    output = {"title": "脚本", "shots": [SHOT], "closing": "结束"}
    assert generators.validate_output(output, data)["payload"]["shots"][0]["end"] == 15
    with pytest.raises(AppError):
        generators.validate_output({**output, "closing": "bad内容"}, data)


def ad_output(amounts=None):
    return {
        "title": "投放建议",
        "allocations": amounts
        or [
            {"channel": "搜索", "amount": "33.33"},
            {"channel": "推荐", "amount": "33.33"},
            {"channel": "内容", "amount": "33.34"},
        ],
        "tests": ["建议小范围验证"],
        "stop_rules": ["异常时停止并复核"],
        "assumptions": ["人群匹配待验证"],
    }


def test_budget_exact_and_server_percentages():
    data = saved("AD_STRATEGY", {"budget": "100.00", "channels": ["搜索", "推荐", "内容"]})
    result = generators.validate_output(ad_output(), data)
    assert sum(Decimal(a["amount"]) for a in result["payload"]["allocations"]) == Decimal("100")
    assert sum(Decimal(a["percent"]) for a in result["payload"]["allocations"]) == Decimal("100")
    for invalid in (
        [{"channel": "搜索", "amount": "100"}],
        [{"channel": "搜索", "amount": "100"}, {"channel": "搜索", "amount": "0"}],
        [
            {"channel": "搜索", "amount": "33.33"},
            {"channel": "推荐", "amount": "33.33"},
            {"channel": "内容", "amount": "33.33"},
        ],
    ):
        with pytest.raises(AppError):
            generators.validate_output(ad_output(invalid), data)


def test_competitor_requires_all_known_sources():
    output = {
        "title": "竞品分析",
        "comparison_refs": ["C1"],
        "opportunities": ["建议验证差异诉求"],
        "risks": ["资料需复核"],
    }
    content = generators.validate_output(output, saved("COMPETITOR_ANALYSIS"))
    assert content["payload"]["comparison_rows"][0]["name"] == "竞品"
    for refs in (["C2"], ["C1", "C1"]):
        with pytest.raises(AppError):
            generators.validate_output(
                {**output, "comparison_refs": refs}, saved("COMPETITOR_ANALYSIS")
            )


def test_review_requires_valid_metric_refs():
    data = saved("BUSINESS_REVIEW")
    data["metrics"] = {
        "analysis_mode": "DATA_SUPPORTED",
        "values": {"paid_amount": {"value": "10.00"}},
    }
    output = {
        "title": "复盘",
        "metric_refs": ["paid_amount"],
        "anomalies": [{"metric_ref": "paid_amount", "reasoning": "金额变化需复核"}],
        "assumptions": ["原因尚待验证"],
        "next_actions": ["建议核对资料"],
    }
    assert generators.validate_output(output, data)["metric_refs"][0]["value"] == "10.00"
    with pytest.raises(AppError):
        generators.validate_output(
            {**output, "anomalies": [{"metric_ref": "forged", "reasoning": "x"}]}, data
        )


@pytest.mark.parametrize(
    "kind,params",
    [
        ("THEME_IDEAS", {"product_ids": ["1"], "goal": "x", "audience": "x", "count": True}),
        ("VIDEO_SCRIPT", {"product_id": "1", "goal": "x", "audience": "x", "duration_seconds": 20}),
        (
            "AD_STRATEGY",
            {
                "product_ids": ["1"],
                "goal": "x",
                "budget": "0",
                "channels": ["x"],
                "start_date": "2026-01-01",
                "end_date": "2026-01-02",
            },
        ),
        ("COMPETITOR_ANALYSIS", {"product_id": "1", "competitor_ids": ["2", "2"]}),
    ],
)
def test_discriminated_inputs_reject_invalid_constraints(kind, params):
    with pytest.raises(ValidationError):
        TypeAdapter(ai_tasks.AnyTask).validate_python(
            {"task_type": kind, "store_id": "1", "input": params}
        )


def test_json_generation_uses_strict_schema_and_one_request(wire):
    wire.body = json.dumps(
        {
            "choices": [
                {
                    "message": {"content": json.dumps({"title": "创意", "ideas": [IDEA]})},
                    "finish_reason": "stop",
                }
            ]
        }
    ).encode()
    data = saved("THEME_IDEAS", {"count": 1})
    result = asyncio.run(generators.generate(config(), SECRET, ["api.deepseek.com"], data))
    request = json.loads(b"".join(wire.requests).split(b"\r\n\r\n", 1)[1])
    assert request["response_format"] == {"type": "json_object"}
    assert "output_json_schema" in json.loads(request["messages"][1]["content"])
    assert len(wire.targets) == 1 and result.content["task_type"] == "THEME_IDEAS"


@mysql
@pytest.mark.parametrize(
    "kind", ["COMPETITOR_ANALYSIS", "THEME_IDEAS", "VIDEO_SCRIPT", "AD_STRATEGY", "BUSINESS_REVIEW"]
)
def test_five_tasks_snapshot_and_draft_end_to_end(scenario, monkeypatch, kind):
    client, db, _, headers, body = scenario
    pid, sid = body["input"]["product_id"], body["store_id"]
    today = business_today()
    if kind == "COMPETITOR_ANALYSIS":
        competitor = Competitor(
            store_id=int(sid),
            code="COMP-TEST",
            name="竞品",
            platform="手工",
            source_note="人工采集",
            collected_at=today,
        )
        db.add(competitor)
        db.flush()
        params = {"product_id": pid, "competitor_ids": [str(competitor.id)]}
        output = {
            "title": "竞品对比",
            "comparison_refs": ["C1"],
            "opportunities": ["建议验证差异表达"],
            "risks": ["资料待核实"],
        }
    elif kind == "THEME_IDEAS":
        params = {"product_ids": [pid], "goal": "创意", "audience": "家庭", "count": 1}
        output = {"title": "创意", "ideas": [IDEA]}
    elif kind == "VIDEO_SCRIPT":
        params = {"product_id": pid, "goal": "展示", "audience": "家庭", "duration_seconds": 15}
        output = {"title": "脚本", "shots": [SHOT], "closing": "结束"}
    elif kind == "AD_STRATEGY":
        params = {
            "product_ids": [pid],
            "goal": "验证",
            "budget": "100",
            "channels": ["搜索"],
            "start_date": str(today),
            "end_date": str(today + timedelta(days=6)),
        }
        output = ad_output([{"channel": "搜索", "amount": "100"}])
    else:
        for offset in (1, 2):
            db.add(
                StoreDaily(
                    store_id=int(sid),
                    business_date=today - timedelta(days=offset),
                    paid_amount=Decimal("10"),
                    paid_orders=1,
                    visitors=5,
                    refund_amount=Decimal("0"),
                )
            )
        params = {
            "goal": "复盘",
            "start_date": str(today - timedelta(days=1)),
            "end_date": str(today - timedelta(days=1)),
        }
        output = {
            "title": "复盘",
            "metric_refs": ["paid_amount"],
            "anomalies": [{"metric_ref": "paid_amount", "reasoning": "建议核对金额表现"}],
            "assumptions": ["原因需验证"],
            "next_actions": ["建议核对订单口径"],
        }
    db.commit()
    response = submit(client, headers, {"task_type": kind, "store_id": sid, "input": params})
    assert response.status_code == 202, response.text
    task_id = response.json()["data"]["id"]

    async def controlled(config, secret, hosts, snapshot):
        assert snapshot["data_copy"]["task_type"] == kind
        return generators.Generated(1, {}, generators.validate_output(output, snapshot))

    monkeypatch.setattr(generators, "generate", controlled)
    execute(db, client.app.state.settings)
    final = client.get(f"/api/v1/ai-tasks/{task_id}").json()["data"]
    assert final["status"] == "SUCCEEDED" and final["draft"]["content"]["task_type"] == kind


@mysql
def test_review_requires_both_periods_before_queue(scenario):
    client, db, _, headers, body = scenario
    yesterday = str(business_today() - timedelta(days=1))
    response = submit(
        client,
        headers,
        {
            "task_type": "BUSINESS_REVIEW",
            "store_id": body["store_id"],
            "input": {"goal": "复盘", "start_date": yesterday, "end_date": yesterday},
        },
    )
    assert (
        response.status_code == 422 and response.json()["error"]["code"] == "AI_INSUFFICIENT_DATA"
    )
