"""五类生成器的来源冻结；商品/竞品均须属于当前店铺，经营事实不交由模型推算。"""

from datetime import timedelta

from sqlalchemy import select

from app.analytics import build_dashboard, display, period_range
from app.core.errors import AppError
from app.daily_data import business_today
from app.models import AdDaily, Competitor, Product, StoreDaily
from app.product_snapshot import METRIC_LABELS, build_product_snapshot


def freeze_generator(db, store, body):
    from app.ai_tasks import ProductAnalysisInput

    params = body.input
    ids = getattr(
        params, "product_ids", [params.product_id] if hasattr(params, "product_id") else []
    )
    products = (
        db.scalars(
            select(Product)
            .where(Product.store_id == store.id, Product.id.in_([int(i) for i in ids]))
            .order_by(Product.id)
            .with_for_update()
        ).all()
        if ids
        else []
    )
    if len(products) != len(ids) or any(product.status != "ACTIVE" for product in products):
        raise AppError(404, "RESOURCE_NOT_FOUND", "选定商品不存在、已归档或不属于当前店铺")
    data = {
        "task_type": body.task_type,
        "store": {"name": store.name, "currency": "CNY"},
        "products": [],
    }
    refs, missing, metrics = [], [], {}
    for i, product in enumerate(products, 1):
        saved = build_product_snapshot(
            db,
            product,
            ProductAnalysisInput(
                product_id=str(product.id),
                goal=getattr(params, "goal", "对比已录入竞品资料，发现待验证的差异机会"),
            ),
        )
        prefix = f"P{i}"
        for ref in saved["source_refs"]:
            ref["ref"] = prefix if ref["ref"] == "D1" else prefix + "_" + ref["ref"]
        for sku in saved["data_copy"]["skus"]:
            sku["ref"] = prefix + "_" + sku["ref"]
        refs.extend(saved["source_refs"])
        data["products"].append({"ref": prefix, **saved["data_copy"]})
    if body.task_type == "COMPETITOR_ANALYSIS":
        competitors = db.scalars(
            select(Competitor)
            .where(
                Competitor.store_id == store.id,
                Competitor.id.in_([int(i) for i in params.competitor_ids]),
            )
            .order_by(Competitor.id)
            .with_for_update()
        ).all()
        if len(competitors) != len(params.competitor_ids) or any(
            c.status != "ACTIVE" for c in competitors
        ):
            raise AppError(404, "RESOURCE_NOT_FOUND", "竞品不存在、已归档或不属于当前店铺")
        data["competitors"] = []
        own_specs = {s["spec_name"] for s in data["products"][0]["skus"]}
        for i, competitor in enumerate(competitors, 1):
            if (
                not competitor.source_note.strip()
                or not competitor.collected_at
                or competitor.collected_at > business_today()
            ):
                raise AppError(422, "AI_INVALID_INPUT", "竞品必须有有效来源说明和采集日期")
            ref = f"C{i}"
            refs.append(
                {
                    "ref": ref,
                    "entity_type": "Competitor",
                    "id": str(competitor.id),
                    "version": competitor.version,
                }
            )
            row = {
                field: getattr(competitor, field)
                for field in (
                    "name",
                    "platform",
                    "brand",
                    "spec",
                    "selling_points",
                    "excerpt",
                    "source_note",
                    "source_url",
                )
            }
            row.update(
                ref=ref,
                price=display(competitor.price, "money"),
                collected_at=str(competitor.collected_at),
                price_comparable=bool(
                    competitor.spec
                    and competitor.spec in own_specs
                    and competitor.price is not None
                ),
            )
            data["competitors"].append(row)
        if any(not c["price_comparable"] for c in data["competitors"]):
            missing.append("部分竞品规格或价格不可比，不能直接判断价格优劣")
        missing.append("竞品资料来自手工录入来源，未自动抓取或验证其真实性")
    if body.task_type in {"BUSINESS_REVIEW", "AD_STRATEGY"}:
        today = business_today()
        history = getattr(params, "history_period", None)
        start = (
            (history.start_date if history else today - timedelta(days=7))
            if body.task_type == "AD_STRATEGY"
            else params.start_date
        )
        end = (
            (history.end_date if history else today - timedelta(days=1))
            if body.task_type == "AD_STRATEGY"
            else params.end_date
        )
        start, end, previous_start, _ = period_range(start, end, today)
        rows = db.scalars(
            select(StoreDaily)
            .where(
                StoreDaily.store_id == store.id,
                StoreDaily.business_date >= previous_start,
                StoreDaily.business_date <= end,
            )
            .order_by(StoreDaily.id)
            .with_for_update()
        ).all()
        current = [r for r in rows if r.business_date >= start]
        previous = [r for r in rows if r.business_date < start]
        if body.task_type == "BUSINESS_REVIEW" and (not current or not previous):
            raise AppError(
                422, "AI_INSUFFICIENT_DATA", "经营复盘要求本期和紧邻上期至少各一条店铺经营记录"
            )
        ad_query = select(AdDaily).where(
            AdDaily.store_id == store.id,
            AdDaily.business_date >= previous_start,
            AdDaily.business_date <= end,
        )
        # 首版投放只冻结选定的最多五个渠道，避免无界扩张来源和模型载荷。
        ad_rows = (
            db.scalars(
                ad_query.where(AdDaily.channel.in_(params.channels))
                .order_by(AdDaily.id)
                .with_for_update()
            ).all()
            if body.task_type == "AD_STRATEGY"
            else []
        )
        dashboard = build_dashboard(rows, ad_rows, start, end, today)
        metrics = {
            "analysis_mode": "DATA_SUPPORTED" if current else "PROFILE_ONLY",
            "period": dashboard["period"],
            "previous_period": dashboard["previous_period"],
            "completeness": dashboard["completeness"],
            "values": {
                key: {"label": METRIC_LABELS[key], **value}
                for key, value in dashboard["metrics"].items()
            },
            "trend": dashboard["trend"],
            "channels": dashboard["channels"],
        }
        missing.extend(dashboard["warnings"])
        data["store_daily"] = [
            {
                "id": str(r.id),
                "version": r.version,
                "date": str(r.business_date),
                "paid_amount": display(r.paid_amount, "money"),
                "paid_orders": str(r.paid_orders),
                "visitors": str(r.visitors),
                "refund_amount": display(r.refund_amount, "money"),
            }
            for r in rows
        ]
        for i, row in enumerate(rows, 1):
            refs.append(
                {
                    "ref": f"R{i}",
                    "entity_type": "StoreDaily",
                    "id": str(row.id),
                    "version": row.version,
                }
            )
        for i, row in enumerate(ad_rows, 1):
            refs.append(
                {
                    "ref": f"A{i}",
                    "entity_type": "AdDaily",
                    "id": str(row.id),
                    "version": row.version,
                }
            )
        data["ad_daily"] = [
            {
                "id": str(r.id),
                "version": r.version,
                "channel": r.channel,
                "date": str(r.business_date),
                "spend": display(r.spend, "money"),
                "impressions": str(r.impressions),
                "clicks": str(r.clicks),
                "attributed_amount": display(r.attributed_amount, "money"),
                "attribution_note": r.attribution_note,
            }
            for r in ad_rows
        ]
        if body.task_type == "AD_STRATEGY" and not ad_rows:
            missing.append("所选渠道无历史投放记录，建议不能视为已验证效果")
    return {
        "input_params": params.model_dump(mode="json", exclude_none=True),
        "source_refs": refs,
        "data_copy": data,
        "metrics": metrics,
        "missing_items": missing,
        "schema_version": "1",
        "metric_version": "1",
        "prompt_version": body.task_type.lower() + "-v1",
    }
