"""商品分析输入冻结：SKU精确金额、商品日经营小计及可比性，全部在服务端计算。"""

from decimal import localcontext

from sqlalchemy import select

from app.analytics import STORE_UNITS, coverage, display, metric_views, period_range, store_values
from app.daily_data import business_today
from app.models import ProductDaily, Sku

METRIC_LABELS = {
    "paid_amount": "实付金额",
    "paid_orders": "支付订单数",
    "visitors": "访客数",
    "refund_amount": "退款金额",
    "net_amount": "净成交额",
    "average_order_value": "客单价",
    "orders_per_visitor": "订单访客比（非买家转化率）",
}


def build_product_snapshot(db, product, params):
    """调用方已锁店铺/商品；按主键冻结明细，禁止把缺失日填零或平均每日比率。"""
    refs = [
        {"ref": "D1", "entity_type": "Product", "id": str(product.id), "version": product.version}
    ]
    data = {
        "product": {
            field: getattr(product, field)
            for field in ("name", "category", "selling_points", "brand", "audience")
        },
        "skus": [],
    }
    missing = []
    with localcontext() as context:
        context.prec = 80
        for i, sku in enumerate(
            db.scalars(
                select(Sku)
                .where(Sku.product_id == product.id, Sku.store_id == product.store_id)
                .order_by(Sku.id)
                .with_for_update()
            ),
            1,
        ):
            ref = f"S{i}"
            refs.append(
                {"ref": ref, "entity_type": "Sku", "id": str(sku.id), "version": sku.version}
            )
            data["skus"].append(
                {
                    "ref": ref,
                    "code": sku.code,
                    "spec_name": sku.spec_name,
                    "price": display(sku.price, "money"),
                    "cost": display(sku.cost, "money"),
                    "gross_margin_percent": display((sku.price - sku.cost) / sku.price, "percent")
                    if sku.price and sku.cost is not None
                    else None,
                }
            )
    if not data["skus"]:
        missing.append("未提供SKU资料")
    elif any(s["cost"] is None for s in data["skus"]):
        missing.append("部分SKU成本未知，不计算对应毛利率")
    metrics = {}
    if params.start_date is None:
        missing.append("未选择经营日期，本次仅分析商品资料")
    else:
        today = business_today()
        start, end, previous_start, previous_end = period_range(
            params.start_date, params.end_date, today
        )
        rows = db.scalars(
            select(ProductDaily)
            .where(
                ProductDaily.product_id == product.id,
                ProductDaily.store_id == product.store_id,
                ProductDaily.business_date >= previous_start,
                ProductDaily.business_date <= end,
            )
            .order_by(ProductDaily.id)
            .with_for_update()
        ).all()
        current = [r for r in rows if start <= r.business_date <= end]
        previous = [r for r in rows if previous_start <= r.business_date <= previous_end]
        complete, old_complete = (
            coverage(current, start, end, today),
            coverage(previous, previous_start, previous_end, today),
        )
        for i, row in enumerate(rows, 1):
            refs.append(
                {
                    "ref": f"R{i}",
                    "entity_type": "ProductDaily",
                    "id": str(row.id),
                    "version": row.version,
                }
            )
        data["product_daily"] = [
            {
                "id": str(r.id),
                "version": r.version,
                "business_date": str(r.business_date),
                "paid_amount": display(r.paid_amount, "money"),
                "refund_amount": display(r.refund_amount, "money"),
                "paid_orders": str(r.paid_orders),
                "visitors": str(r.visitors),
            }
            for r in rows
        ]
        with localcontext() as context:
            context.prec = 80
            values = metric_views(
                store_values(current),
                store_values(previous),
                STORE_UNITS,
                complete["complete"] and old_complete["complete"],
            )
        metrics = {
            "analysis_mode": "DATA_SUPPORTED" if current else "PROFILE_ONLY",
            "period": {"start_date": str(start), "end_date": str(end)},
            "previous_period": {"start_date": str(previous_start), "end_date": str(previous_end)},
            "completeness": complete,
            "previous_completeness": old_complete,
            "values": {
                key: {"label": METRIC_LABELS[key], **value} for key, value in values.items()
            },
            "trend": [
                {
                    "date": str(r.business_date),
                    "paid_amount": display(r.paid_amount, "money"),
                    "refund_amount": display(r.refund_amount, "money"),
                    "paid_orders": str(r.paid_orders),
                    "visitors": str(r.visitors),
                }
                for r in sorted(current, key=lambda row: row.business_date)
            ],
        }
        if not current:
            missing.append("所选本期无商品经营数据，降级为资料分析，不作销量结论")
        elif not complete["complete"]:
            missing.append("本期有缺失日或包含当日未完结数据，仅为已录入小计，不代表完整周期表现")
        if not complete["complete"] or not old_complete["complete"]:
            missing.append("本期或上期数据不完整，不提供正式环比")
        if values["orders_per_visitor"]["value"] is None and current:
            missing.append("访客数为零，订单访客比为N/A")
    return {
        "input_params": params.model_dump(mode="json", exclude_none=True),
        "source_refs": refs,
        "data_copy": data,
        "metrics": metrics,
        "missing_items": missing,
        "schema_version": "2",
        "metric_version": "1",
        "prompt_version": "product-analysis-v2",
    }
