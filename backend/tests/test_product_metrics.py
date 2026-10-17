"""商品经营分析验证脚本。

用途：验证日期边界、SKU零值/未知成本、确定性指标、完整性、同快照重试与指标引用防伪造。
命令：backend下 RUN_MYSQL_TESTS=1 uv run pytest tests/test_product_metrics.py -q。
前置：开发/测试MySQL迁移至head；数据库用例沿用外层事务回滚，不调用真实模型。
日期与生成输出校验可离线运行，测试不读取真实API Key，不产生模型费用。
"""

import os
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from test_ai_tasks import OUTPUT, execute, submit
from test_ai_tasks import scenario as scenario
from test_identity import system as system
from test_model_calls import configured as configured

from app import ai_tasks, product_generator
from app.core.errors import AppError
from app.daily_data import business_today
from app.models import ProductDaily, Sku

mysql = pytest.mark.skipif(os.getenv("RUN_MYSQL_TESTS") != "1", reason="需独立MySQL测试库")


def dated(body):
    end = business_today() - timedelta(days=1)
    return {
        **body,
        "input": {
            **body["input"],
            "start_date": str(end - timedelta(days=1)),
            "end_date": str(end),
        },
    }


def daily(db, body, offset, amount="10", visits=5, orders=1):
    row = ProductDaily(
        store_id=int(body["store_id"]),
        product_id=int(body["input"]["product_id"]),
        business_date=business_today() - timedelta(days=offset),
        paid_amount=Decimal(amount),
        paid_orders=orders,
        visitors=visits,
        refund_amount=Decimal("0"),
    )
    db.add(row)
    return row


def snapshot(client, headers, body):
    response = submit(client, headers, body)
    assert response.status_code == 202, response.text
    task_id = response.json()["data"]["id"]
    return task_id, client.get(f"/api/v1/ai-tasks/{task_id}").json()["data"]["snapshot"]


@pytest.mark.parametrize(
    "dates",
    [
        {"start_date": "2026-01-01"},
        {"end_date": "2026-01-01"},
        {"start_date": "2026-01-02", "end_date": "2026-01-01"},
        {"start_date": "2026-01-01", "end_date": "2026-04-01"},
        {"start_date": "0001-01-01", "end_date": "0001-01-01"},
        {"start_date": 0, "end_date": 0},
        {"start_date": "2026-01-01T00:00:00", "end_date": "2026-01-02"},
    ],
)
def test_invalid_period(dates):
    with pytest.raises(ValidationError):
        ai_tasks.ProductAnalysisInput.model_validate({"product_id": "1", "goal": "分析", **dates})


def test_future_date_rejected_and_ninety_days_allowed():
    today = business_today()
    valid = {
        "product_id": "1",
        "goal": "分析",
        "start_date": str(today - timedelta(days=89)),
        "end_date": str(today),
    }
    ai_tasks.ProductAnalysisInput.model_validate(valid)
    with pytest.raises(ValidationError):
        ai_tasks.ProductAnalysisInput.model_validate(
            {**valid, "end_date": str(today + timedelta(days=1))}
        )


@mysql
def test_complete_period_sums_ratios_and_frozen_sources(scenario, monkeypatch):
    client, db, _, headers, body = scenario
    for offset, amount, visits in ((1, "90", 9), (2, "10", 1), (3, "45", 5), (4, "5", 5)):
        daily(db, body, offset, amount, visits)
    db.commit()
    request = dated(body)
    task_id, frozen = snapshot(client, headers, request)
    metrics = frozen["metrics"]
    assert metrics["analysis_mode"] == "DATA_SUPPORTED"
    assert metrics["values"]["paid_amount"]["value"] == "100.00"
    assert metrics["values"]["paid_amount"]["change_percent"] == "100.00"
    assert metrics["values"]["orders_per_visitor"]["value"] == "20.00"
    assert "非买家转化率" in metrics["values"]["orders_per_visitor"]["label"]
    assert len(frozen["data_copy"]["product_daily"]) == 4
    assert len(metrics["trend"]) == 2
    assert sum(ref["entity_type"] == "ProductDaily" for ref in frozen["source_refs"]) == 4
    row = db.scalar(
        select(ProductDaily).where(ProductDaily.product_id == int(body["input"]["product_id"]))
    )
    row.paid_amount = Decimal("9999")
    db.commit()

    async def fake(config, secret, hosts, saved):
        assert saved["metrics"]["values"]["paid_amount"]["value"] == "100.00"
        output = {**OUTPUT, "metric_refs": ["paid_amount", "orders_per_visitor"]}
        return product_generator.Generated(1, {}, product_generator.validate_content(output, saved))

    monkeypatch.setattr(product_generator, "generate", fake)
    execute(db, client.app.state.settings)
    final = client.get(f"/api/v1/ai-tasks/{task_id}").json()["data"]
    assert final["status"] == "SUCCEEDED"
    assert final["draft"]["content"]["metric_refs"][0]["value"] == "100.00"


@mysql
def test_partial_period_never_fills_zero_or_reports_formal_change(scenario):
    client, db, _, headers, body = scenario
    daily(db, body, 1, "10")
    daily(db, body, 3, "5")
    daily(db, body, 4, "5")
    db.commit()
    _, frozen = snapshot(client, headers, dated(body))
    metrics = frozen["metrics"]
    assert metrics["completeness"]["recorded_days"] == 1
    assert len(metrics["completeness"]["missing_dates"]) == 1
    assert metrics["values"]["paid_amount"]["value"] == "10.00"
    assert all(value["change_percent"] is None for value in metrics["values"].values())
    assert any("小计" in text for text in frozen["missing_items"])


@mysql
def test_zero_is_present_not_missing_and_denominator_zero_is_na(scenario):
    client, db, _, headers, body = scenario
    for offset in (1, 2, 3, 4):
        daily(db, body, offset, "0", 0, 0)
    db.commit()
    _, frozen = snapshot(client, headers, dated(body))
    metrics = frozen["metrics"]
    assert metrics["analysis_mode"] == "DATA_SUPPORTED" and metrics["completeness"]["complete"]
    assert metrics["values"]["paid_amount"]["value"] == "0.00"
    assert metrics["values"]["paid_orders"]["value"] == "0"
    assert metrics["values"]["orders_per_visitor"]["value"] is None
    assert metrics["values"]["paid_amount"]["change_percent"] is None


@mysql
def test_missing_data_falls_back_to_profile(scenario):
    client, db, _, headers, body = scenario
    daily(db, body, 3)
    db.commit()
    _, frozen = snapshot(client, headers, dated(body))
    assert frozen["metrics"]["analysis_mode"] == "PROFILE_ONLY"
    assert frozen["metrics"]["values"]["paid_amount"]["value"] is None
    assert product_generator.validate_content(OUTPUT, frozen)["metric_refs"] == []


@mysql
def test_today_cannot_be_complete(scenario):
    client, db, _, headers, body = scenario
    daily(db, body, 0)
    daily(db, body, 1)
    db.commit()
    request = {
        **body,
        "input": {
            **body["input"],
            "start_date": str(business_today()),
            "end_date": str(business_today()),
        },
    }
    _, frozen = snapshot(client, headers, request)
    assert frozen["metrics"]["completeness"]["includes_current_day"]
    assert frozen["metrics"]["values"]["paid_amount"]["change_percent"] is None


@mysql
def test_sku_amounts_unknown_and_zero_cost(scenario):
    client, db, _, headers, body = scenario
    for price, cost in (("25", "0"), ("0", "0"), ("10", None)):
        db.add(
            Sku(
                store_id=int(body["store_id"]),
                product_id=int(body["input"]["product_id"]),
                code=uuid4().hex,
                spec_name="规格",
                price=Decimal(price),
                cost=Decimal(cost) if cost is not None else None,
            )
        )
    db.commit()
    _, frozen = snapshot(client, headers, body)
    skus = frozen["data_copy"]["skus"]
    assert skus[0]["cost"] == "0.00" and skus[0]["gross_margin_percent"] == "100.00"
    assert skus[1]["gross_margin_percent"] is None and skus[2]["cost"] is None
    assert any("成本未知" in text for text in frozen["missing_items"])


@pytest.mark.parametrize(
    "refs", [[], ["forged"], ["paid_amount", "paid_amount"], ["orders_per_visitor"]]
)
def test_invalid_metric_reference(refs):
    saved = {
        "data_copy": {"product": {}},
        "missing_items": [],
        "metrics": {
            "analysis_mode": "DATA_SUPPORTED",
            "values": {"paid_amount": {"value": "0.00"}, "orders_per_visitor": {"value": None}},
        },
    }
    with pytest.raises(AppError):
        product_generator.validate_content({**OUTPUT, "metric_refs": refs}, saved)
