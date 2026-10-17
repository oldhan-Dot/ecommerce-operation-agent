"""同口径经营指标与日历完整性；所有数值由代码计算，不交由模型推算。"""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, localcontext

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select

from app.core.errors import AppError
from app.daily_data import business_today
from app.dependencies import current_user, get_db, store_scope
from app.identity import result
from app.models import AdDaily, ProductDaily, Store, StoreDaily

router = APIRouter(prefix="/api/v1", tags=["analytics"])
STORE_UNITS = {
    "paid_amount": "money",
    "paid_orders": "count",
    "visitors": "count",
    "refund_amount": "money",
    "net_amount": "money",
    "average_order_value": "money",
    "orders_per_visitor": "percent",
}
AD_UNITS = {
    "spend": "money",
    "impressions": "count",
    "clicks": "count",
    "ctr": "percent",
    "cpc": "money",
    "attributed_amount": "money",
    "roas": "ratio",
}


def period_range(start, end, today):
    """默认最近七个已结束业务日；本期最长九十天，上期紧邻且等长。"""
    try:
        end = end or today - timedelta(days=1)
        start = start or end - timedelta(days=6)
    except OverflowError as exc:
        raise AppError(422, "DATA_INVALID", "日期区间超出支持范围") from exc
    length = (end - start).days + 1
    if not 1 <= length <= 90 or end > today or start.toordinal() <= length:
        raise AppError(422, "DATA_INVALID", "请选择不超过 90 天且不含未来日期的有效区间")
    return start, end, start - timedelta(days=length), start - timedelta(days=1)


def calendar(start, end):
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def coverage(rows, start, end, today):
    days = calendar(start, end)
    present = {r.business_date for r in rows}
    missing = [d.isoformat() for d in days if d not in present]
    return {
        "recorded_days": len(present),
        "expected_days": len(days),
        "missing_dates": missing,
        "complete": not missing and end < today,
        "includes_current_day": end == today,
    }


def divide(numerator, denominator):
    return numerator / denominator if numerator is not None and denominator else None


def sums(rows, fields):
    # 没有记录时为未知，已有记录的明确零值才是零。
    return {
        name: sum((Decimal(getattr(r, name)) for r in rows), Decimal(0)) if rows else None
        for name in fields
    }


def store_values(rows):
    values = sums(rows, ("paid_amount", "paid_orders", "visitors", "refund_amount"))
    values["net_amount"] = values["paid_amount"] - values["refund_amount"] if rows else None
    values["average_order_value"] = divide(values["paid_amount"], values["paid_orders"])
    values["orders_per_visitor"] = divide(values["paid_orders"], values["visitors"])
    return values


def ad_values(rows):
    values = sums(rows, ("spend", "impressions", "clicks"))
    notes = {r.attribution_note for r in rows}
    known = (
        bool(rows)
        and all(r.attributed_amount is not None for r in rows)
        and len(notes) == 1
        and bool(next(iter(notes)))
    )
    values["attributed_amount"] = (
        sum((r.attributed_amount for r in rows), Decimal(0)) if known else None
    )
    values["ctr"] = divide(values["clicks"], values["impressions"])
    values["cpc"] = divide(values["spend"], values["clicks"])
    values["roas"] = divide(values["attributed_amount"], values["spend"])
    return values, notes, known


def display(value, unit):
    if value is None:
        return None
    if unit == "count":
        return str(int(value))
    return format(value * 100 if unit == "percent" else value, ".2f")


def metric_views(current, previous, units, comparable, *, attribution_comparable=True):
    output = {}
    for key, unit in units.items():
        value, old = current[key], previous[key]
        allowed = comparable and (
            attribution_comparable or key not in {"roas", "attributed_amount"}
        )
        change = (
            divide(value - old, old) if allowed and value is not None and old is not None else None
        )
        output[key] = {
            "value": display(value, unit),
            "previous_value": display(old, unit),
            "change_percent": display(change, "percent"),
            "unit": unit,
        }
    return output


def build_dashboard(store_rows, ad_rows, start, end, today):
    """本期和上期独立核对完整性；按渠道计算归因，不合并跨渠道成交。"""
    start, end, previous_start, previous_end = period_range(start, end, today)
    current = [r for r in store_rows if start <= r.business_date <= end]
    previous = [r for r in store_rows if previous_start <= r.business_date <= previous_end]
    complete = coverage(current, start, end, today)
    previous_complete = coverage(previous, previous_start, previous_end, today)
    warnings = []
    if not complete["complete"]:
        warnings.append("本期店铺数据缺失或含当日未完结数据，仅展示已录入小计，关闭正式环比")
    if not previous_complete["complete"]:
        warnings.append("上期店铺数据不完整，无法计算正式环比")
    grouped = defaultdict(list)
    for row in ad_rows:
        if previous_start <= row.business_date <= end:
            grouped[row.channel].append(row)
    channels = []
    # 汇总和比率计算保留高精度，最后一步才格式化显示；不平均每日比率。
    with localcontext() as context:
        context.prec = 80
        metrics = metric_views(
            store_values(current),
            store_values(previous),
            STORE_UNITS,
            complete["complete"] and previous_complete["complete"],
        )
        for channel, rows in sorted(grouped.items()):
            now_rows = [r for r in rows if start <= r.business_date <= end]
            old_rows = [r for r in rows if previous_start <= r.business_date <= previous_end]
            now_values, now_notes, now_known = ad_values(now_rows)
            old_values, old_notes, old_known = ad_values(old_rows)
            now_coverage = coverage(now_rows, start, end, today)
            old_coverage = coverage(old_rows, previous_start, previous_end, today)
            notes_match = now_known and old_known and now_notes == old_notes
            channel_warnings = []
            if not now_known:
                channel_warnings.append("本期归因金额缺失或口径不一致，归因金额与 ROAS 为 N/A")
            if not notes_match:
                channel_warnings.append("两期归因资料缺失或口径不一致，归因指标不计算环比")
            if any(r.clicks > r.impressions for r in now_rows):
                channel_warnings.append("存在点击量大于曝光量的数据，请核对口径")
            channels.append(
                {
                    "channel": channel,
                    "attribution_note": next(iter(now_notes)) if now_known else None,
                    "previous_attribution_note": next(iter(old_notes)) if old_known else None,
                    "completeness": now_coverage,
                    "previous_completeness": old_coverage,
                    "metrics": metric_views(
                        now_values,
                        old_values,
                        AD_UNITS,
                        now_coverage["complete"] and old_coverage["complete"],
                        attribution_comparable=notes_match,
                    ),
                    "warnings": channel_warnings,
                }
            )
    by_date = {r.business_date: r for r in current}
    trend = []
    for day in calendar(start, end):
        row = by_date.get(day)
        trend.append(
            {
                "date": day.isoformat(),
                "is_current_day": day == today,
                "paid_amount": display(row.paid_amount, "money") if row else None,
                "refund_amount": display(row.refund_amount, "money") if row else None,
                "paid_orders": str(row.paid_orders) if row else None,
                "visitors": str(row.visitors) if row else None,
            }
        )
    return {
        "period": {"start_date": start.isoformat(), "end_date": end.isoformat()},
        "previous_period": {
            "start_date": previous_start.isoformat(),
            "end_date": previous_end.isoformat(),
            "completeness": previous_complete,
        },
        "completeness": complete,
        "metrics": metrics,
        "trend": trend,
        "channels": channels,
        "warnings": warnings,
    }


@router.get("/stores/{store_id}/dashboard")
def dashboard(
    store_id: int,
    request: Request,
    start_date: date | None = None,
    end_date: date | None = None,
    p=Depends(current_user),
    db=Depends(get_db),
):
    store = db.scalar(store_scope(p).where(Store.id == store_id))
    if not store:
        raise AppError(404, "RESOURCE_NOT_FOUND", "店铺不存在")
    today = business_today()
    start, end, previous_start, _ = period_range(start_date, end_date, today)
    # 每张表一次读取两个周期；查询始终限制已授权店铺，归档店铺可查看历史。
    store_rows = db.scalars(
        select(StoreDaily).where(
            StoreDaily.store_id == store.id, StoreDaily.business_date.between(previous_start, end)
        )
    ).all()
    ad_rows = db.scalars(
        select(AdDaily).where(
            AdDaily.store_id == store.id, AdDaily.business_date.between(previous_start, end)
        )
    ).all()
    data = build_dashboard(store_rows, ad_rows, start, end, today)
    product_totals = dict(
        db.execute(
            select(ProductDaily.business_date, func.sum(ProductDaily.paid_amount))
            .where(
                ProductDaily.store_id == store.id, ProductDaily.business_date.between(start, end)
            )
            .group_by(ProductDaily.business_date)
        ).all()
    )
    differences = sum(
        r.business_date in product_totals and product_totals[r.business_date] != r.paid_amount
        for r in store_rows
        if start <= r.business_date <= end
    )
    if differences:
        data["warnings"].append(
            f"有 {differences} 日的已录入商品支付小计与店铺汇总不同，请核对覆盖范围；两者未相加"
        )
    data["store"] = {"id": str(store.id), "name": store.name, "status": store.status}
    return result(request, data)
