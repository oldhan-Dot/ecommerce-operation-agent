"""经营看板测试脚本。

用途：离线验证汇总精度、完整性、环比、渠道归因、日期边界及趋势缺日；MySQL 验证权限与聚合查询。
运行：backend 下 uv run pytest tests/test_analytics.py -q；数据库用例需 RUN_MYSQL_TESTS=1。
前置：离线测试无需数据库；集成用例需开发 MySQL 迁移到 head，测试资料由外层事务回滚。
不以模拟数据库代替 MySQL 查询与权限验证，不包含性能压测。
"""

import os
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_identity import new_store, sign_in
from test_identity import system as system

from app.analytics import build_dashboard, period_range
from app.core.errors import AppError
from app.models import AdDaily, Enterprise, Product, ProductDaily, Store, StoreDaily, StoreGrant

TODAY = date(2026, 10, 17)
DAY = date(2026, 10, 16)


def store_row(day=DAY, paid="1000", orders=10, visitors=200, refund="100"):
    return SimpleNamespace(
        business_date=day,
        paid_amount=Decimal(paid),
        paid_orders=orders,
        visitors=visitors,
        refund_amount=Decimal(refund),
    )


def ad_row(
    day=DAY,
    channel="搜索",
    spend="10",
    impressions=100,
    clicks=10,
    attributed="30",
    note="七日点击",
):
    return SimpleNamespace(
        business_date=day,
        channel=channel,
        spend=Decimal(spend),
        impressions=impressions,
        clicks=clicks,
        attributed_amount=Decimal(attributed) if attributed is not None else None,
        attribution_note=note,
    )


def test_acceptance_metrics_and_growth():
    rows = [store_row(), store_row(DAY - timedelta(days=1), paid="800")]
    data = build_dashboard(rows, [], DAY, DAY, TODAY)
    metrics = data["metrics"]
    assert metrics["average_order_value"]["value"] == "100.00"
    assert metrics["orders_per_visitor"]["value"] == "5.00"
    assert metrics["net_amount"]["value"] == "900.00"
    assert metrics["paid_amount"]["change_percent"] == "25.00"
    assert data["completeness"]["complete"]


def test_ratios_use_total_numerators_and_keep_large_integers():
    rows = [
        store_row(DAY, paid="1", orders=1, visitors=1),
        store_row(DAY - timedelta(days=1), paid="99", orders=9, visitors=99),
    ]
    data = build_dashboard(rows, [], DAY - timedelta(days=1), DAY, TODAY)
    assert data["metrics"]["average_order_value"]["value"] == "10.00"
    assert data["metrics"]["orders_per_visitor"]["value"] == "10.00"
    rows = [
        store_row(DAY, paid="9999999999999999.99", orders=2**63 - 1),
        store_row(DAY - timedelta(days=1), paid="0.01", orders=2**63 - 1),
    ]
    data = build_dashboard(rows, [], DAY - timedelta(days=1), DAY, TODAY)
    assert data["metrics"]["paid_orders"]["value"] == str(2 * (2**63 - 1))
    assert data["metrics"]["paid_amount"]["value"] == "10000000000000000.00"


def test_missing_days_are_not_zero_and_disable_growth():
    start = DAY - timedelta(days=6)
    rows = [store_row(DAY - timedelta(days=i), paid="0") for i in range(5)]
    rows.extend(store_row(start - timedelta(days=i + 1)) for i in range(7))
    data = build_dashboard(rows, [], start, DAY, TODAY)
    assert data["completeness"]["recorded_days"] == 5
    assert data["completeness"]["expected_days"] == 7
    assert len(data["completeness"]["missing_dates"]) == 2
    assert data["metrics"]["paid_amount"]["value"] == "0.00"
    assert data["metrics"]["paid_amount"]["change_percent"] is None
    assert data["trend"][0]["paid_amount"] is None
    assert data["trend"][-1]["paid_amount"] == "0.00"
    empty = build_dashboard([], [], DAY, DAY, TODAY)
    assert all(m["value"] is None for m in empty["metrics"].values())


def test_current_day_zero_denominators_negative_net():
    rows = [store_row(TODAY, paid="0", orders=0, visitors=0, refund="100"), store_row()]
    data = build_dashboard(rows, [], TODAY, TODAY, TODAY)
    assert data["completeness"]["includes_current_day"] and not data["completeness"]["complete"]
    assert data["trend"][0]["is_current_day"]
    assert data["metrics"]["net_amount"]["value"] == "-100.00"
    assert data["metrics"]["average_order_value"]["value"] is None
    assert data["metrics"]["orders_per_visitor"]["value"] is None
    assert data["metrics"]["paid_amount"]["change_percent"] is None


def test_previous_zero_and_missing_and_ratio_over_hundred():
    for previous in ([], [store_row(DAY - timedelta(days=1), paid="0")]):
        data = build_dashboard([store_row(orders=300), *previous], [], DAY, DAY, TODAY)
        assert data["metrics"]["paid_amount"]["change_percent"] is None
        assert data["metrics"]["orders_per_visitor"]["value"] == "150.00"


@pytest.mark.parametrize("problem", ["missing", "inconsistent", "zero_spend"])
def test_invalid_attribution_does_not_substitute_store_revenue(problem):
    start = DAY - timedelta(days=1)
    rows = [
        ad_row(start),
        ad_row(
            DAY,
            attributed=None if problem == "missing" else "30",
            note="一日点击" if problem == "inconsistent" else "七日点击",
        ),
    ]
    if problem == "zero_spend":
        for row in rows:
            row.spend = Decimal(0)
    data = build_dashboard([store_row()], rows, start, DAY, TODAY)
    assert data["channels"][0]["metrics"]["roas"]["value"] is None


def test_channels_are_independent_and_notes_must_match_for_comparison():
    old = DAY - timedelta(days=1)
    rows = [
        ad_row(),
        ad_row(old, spend="20"),
        ad_row(channel="信息流", note="一日点击"),
        ad_row(old, channel="信息流"),
    ]
    data = build_dashboard([], rows, DAY, DAY, TODAY)
    assert "roas" not in data["metrics"] and len(data["channels"]) == 2
    channels = {c["channel"]: c for c in data["channels"]}
    assert channels["搜索"]["metrics"]["roas"]["value"] == "3.00"
    assert channels["搜索"]["metrics"]["roas"]["change_percent"] == "100.00"
    assert channels["信息流"]["metrics"]["roas"]["change_percent"] is None
    assert channels["信息流"]["metrics"]["spend"]["change_percent"] == "0.00"


def test_ad_ratio_weighting_missing_days_and_anomalies():
    start = DAY - timedelta(days=1)
    rows = [ad_row(start, impressions=1, clicks=1), ad_row(DAY, impressions=99, clicks=9)]
    data = build_dashboard([], rows, start, DAY, TODAY)
    assert data["channels"][0]["metrics"]["ctr"]["value"] == "10.00"
    data = build_dashboard(
        [], [ad_row(clicks=101), ad_row(start - timedelta(days=1))], start, DAY, TODAY
    )
    channel = data["channels"][0]
    assert channel["metrics"]["ctr"]["value"] == "101.00"
    assert channel["metrics"]["ctr"]["change_percent"] is None
    assert any("点击量大于" in w for w in channel["warnings"])


def test_default_period_and_equal_previous_period():
    assert period_range(None, None, TODAY) == (
        date(2026, 10, 10),
        DAY,
        date(2026, 10, 3),
        date(2026, 10, 9),
    )
    start = DAY - timedelta(days=89)
    assert period_range(start, DAY, TODAY)[2] == start - timedelta(days=90)


@pytest.mark.parametrize(
    "start,end",
    [
        (DAY, DAY - timedelta(days=1)),
        (DAY - timedelta(days=90), DAY),
        (DAY, TODAY + timedelta(days=1)),
        (date.min, date.min),
        (None, date.min),
    ],
)
def test_invalid_periods(start, end):
    with pytest.raises(AppError):
        period_range(start, end, TODAY)


mysql = pytest.mark.skipif(os.getenv("RUN_MYSQL_TESTS") != "1", reason="显式启用真实 MySQL")


@mysql
def test_dashboard_query_isolated_and_does_not_sum_product_data(system):
    client, db, people = system
    headers = sign_in(client, people["admin"])
    store = new_store(client, headers)
    for day, amount in ((DAY, "1000"), (DAY - timedelta(days=1), "800")):
        db.add(
            StoreDaily(
                store_id=int(store["id"]),
                business_date=day,
                paid_amount=Decimal(amount),
                paid_orders=10,
                visitors=200,
                refund_amount=Decimal(100),
            )
        )
    product = Product(
        store_id=int(store["id"]), code="P", name="商品", category="食品", selling_points="卖点"
    )
    db.add(product)
    db.flush()
    db.add(
        ProductDaily(
            store_id=int(store["id"]),
            product_id=product.id,
            business_date=DAY,
            paid_amount=Decimal(9000),
            paid_orders=10,
            visitors=200,
            refund_amount=Decimal(0),
        )
    )
    db.add(
        AdDaily(
            store_id=int(store["id"]),
            business_date=DAY,
            channel="搜索",
            spend=Decimal(10),
            clicks=10,
            impressions=100,
            attributed_amount=Decimal(30),
            attribution_note="七日点击",
        )
    )
    foreign = Enterprise(name="其他企业")
    db.add(foreign)
    db.flush()
    hidden = Store(enterprise_id=foreign.id, code="H", name="隐藏店铺", platform="手工")
    db.add(hidden)
    db.commit()
    url = f"/api/v1/stores/{store['id']}/dashboard?start_date={DAY}&end_date={DAY}"
    response = client.get(url)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["metrics"]["paid_amount"]["value"] == "1000.00"
    assert data["metrics"]["paid_amount"]["change_percent"] == "25.00"
    assert any("商品支付小计" in warning for warning in data["warnings"])
    assert data["channels"][0]["metrics"]["roas"]["value"] == "3.00"
    assert client.get(f"/api/v1/stores/{hidden.id}/dashboard").status_code == 404
    sign_in(client, people["op"])
    assert client.get(url).status_code == 404
    grant = StoreGrant(
        user_id=people["op"]["id"], store_id=int(store["id"]), granted_by=people["admin"]["id"]
    )
    db.add(grant)
    db.commit()
    assert client.get(url).status_code == 200
    assert (
        client.get(
            f"/api/v1/stores/{store['id']}/dashboard?start_date=2026-01-01&end_date=2026-10-16"
        ).status_code
        == 422
    )
    db.get(Store, int(store["id"])).status = "ARCHIVED"
    db.commit()
    assert client.get(url).json()["data"]["store"]["status"] == "ARCHIVED"
    db.delete(grant)
    db.commit()
    assert client.get(url).status_code == 404
