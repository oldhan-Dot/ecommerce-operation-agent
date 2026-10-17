"""六类 CSV 解析与业务预检，不写入业务表；规范行供后续原子入库复用。"""

import csv
import io
import json
from collections import defaultdict
from dataclasses import dataclass, field

from pydantic import ValidationError
from sqlalchemy import select

from app.ad_daily import AdDailyInput
from app.daily_data import StoreDailyInput
from app.models import AdDaily, Competitor, Product, ProductDaily, Sku, Store, StoreDaily
from app.product_daily import ProductDailyInput
from app.schemas import CompetitorInput, ProductInput, StoreInput

MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 10_000
# 与所有录入接口共享字段口径；商品文件一行一个 SKU，店铺由上传时选择。
TEMPLATES = {
    "stores": ["code", "name", "platform", "external_id", "category", "notes"],
    "products": [
        "code",
        "name",
        "category",
        "selling_points",
        "brand",
        "audience",
        "source_url",
        "sku_code",
        "spec_name",
        "price",
        "cost",
    ],
    "competitors": [
        "code",
        "name",
        "platform",
        "brand",
        "spec",
        "price",
        "selling_points",
        "excerpt",
        "source_note",
        "source_url",
        "collected_at",
        "product_code",
    ],
    "store": ["business_date", "paid_amount", "paid_orders", "visitors", "refund_amount"],
    "product": [
        "product_code",
        "business_date",
        "paid_amount",
        "paid_orders",
        "visitors",
        "refund_amount",
    ],
    "ad": [
        "channel",
        "business_date",
        "spend",
        "impressions",
        "clicks",
        "attributed_amount",
        "attribution_note",
    ],
}


class CsvFormatError(ValueError):
    """只携带可展示的固定提示，避免输出原始文件或底层异常。"""


@dataclass
class PreviewRow:
    row_no: int
    business_key: str
    payload: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    action: str = "NEW"
    expected_version: int | None = None
    before: dict | None = None


def read_csv(raw: bytes, kind: str):
    """兼容 UTF-8 BOM，拒绝坏引号、错列、重复表头和超限，保留逻辑记录起始行号。"""
    if len(raw) > MAX_BYTES:
        raise CsvFormatError("文件不能超过 10 MB")
    try:
        content = raw.decode("utf-8-sig")
        if "\x00" in content:
            raise CsvFormatError("文件包含非法空字符")
        reader = csv.reader(io.StringIO(content, newline=""), strict=True)
        headers = next(reader, None)
        if (
            headers is None
            or len(set(headers)) != len(headers)
            or set(headers) != set(TEMPLATES[kind])
        ):
            raise CsvFormatError("表头与所选模板不一致，请下载并保留全部模板列名")
        rows = []
        while True:
            line = reader.line_num + 1
            values = next(reader, None)
            if values is None:
                break
            if len(rows) >= MAX_ROWS:
                raise CsvFormatError("文件不能超过 10000 行数据")
            if len(values) != len(headers):
                raise CsvFormatError(f"第 {line} 行列数与模板不一致")
            rows.append((line, dict(zip(headers, values, strict=True))))
        if not rows:
            raise CsvFormatError("文件没有数据行，请在模板表头后填写数据")
        return rows
    except UnicodeDecodeError as exc:
        raise CsvFormatError("文件必须使用 UTF-8 编码（兼容 BOM）") from exc
    except csv.Error as exc:
        raise CsvFormatError("CSV 引号或字段格式错误，请检查文件格式") from exc


def preview(db, batch, store, raw):
    """批量读取目标和引用，避免万行 CSV 对云数据库逐行查询。"""
    kind = batch.import_type
    source = read_csv(raw, kind)
    dependencies = {}

    def depend(item):
        if item is not None:
            dependencies[(item.__tablename__, item.id)] = item.version

    depend(store)
    if kind == "stores":
        stores = {
            s.code: s
            for s in db.scalars(select(Store).where(Store.enterprise_id == batch.enterprise_id))
        }
        products, skus, targets = {}, {}, {}
    else:
        stores = {}
        products = {
            p.code: p for p in db.scalars(select(Product).where(Product.store_id == store.id))
        }
        skus = (
            {s.code: s for s in db.scalars(select(Sku).where(Sku.store_id == store.id))}
            if kind == "products"
            else {}
        )
        model = {
            "competitors": Competitor,
            "store": StoreDaily,
            "product": ProductDaily,
            "ad": AdDaily,
        }.get(kind)
        targets = {}
        if model:
            for item in db.scalars(select(model).where(model.store_id == store.id)):
                if kind == "competitors":
                    key = item.code
                elif kind == "store":
                    key = item.business_date.isoformat()
                elif kind == "product":
                    key = (item.product_id, item.business_date.isoformat())
                else:
                    key = (item.channel, item.business_date.isoformat())
                targets[key] = item
    rows = []
    duplicates, product_groups, sku_groups = defaultdict(list), defaultdict(list), defaultdict(list)
    for number, raw in source:
        row = PreviewRow(number, "")
        rows.append(row)
        values = {k: v if v != "" else None for k, v in raw.items()}
        # 即使字段校验失败也参与重复键检测，不能让另一条同键合法行漏过。
        code = (raw.get("code") or "").strip()
        if kind in {"stores", "competitors"}:
            raw_key = code
        elif kind == "products":
            raw_key = (code, (raw.get("sku_code") or "").strip())
        elif kind == "store":
            raw_key = raw["business_date"]
        elif kind == "product":
            reference = products.get((raw.get("product_code") or "").strip())
            raw_key = (reference.id if reference else raw.get("product_code"), raw["business_date"])
        else:
            raw_key = (raw["channel"].strip(), raw["business_date"])
        row.business_key = json.dumps(raw_key, ensure_ascii=False, separators=(",", ":"))[:256]
        duplicates[row.business_key].append(row)
        if kind == "products":
            sku_groups[(raw.get("sku_code") or "").strip()].append(row)
        try:
            target = None
            if kind == "stores":
                value = StoreInput.model_validate(values)
                key = value.code
                target = stores.get(key)
            elif kind == "products":
                code = (values["code"] or "").strip()
                target = products.get(code)
                sku = skus.get((values["sku_code"] or "").strip())
                if sku and (target is None or sku.product_id != target.id):
                    raise ValueError("SKU 编码已属于其他商品，不能跨商品关联")
                value = ProductInput.model_validate(
                    {
                        "store_id": str(store.id),
                        **{
                            k: v
                            for k, v in values.items()
                            if k not in {"sku_code", "spec_name", "price", "cost"}
                        },
                        "skus": [
                            {
                                "id": str(sku.id) if sku else None,
                                "code": values["sku_code"],
                                "spec_name": values["spec_name"],
                                "price": values["price"],
                                "cost": values["cost"],
                            }
                        ],
                    }
                )
                depend(sku)
                key = (value.code, value.skus[0].code)
                product_groups[value.code].append(row)
            else:
                data = {"store_id": str(store.id), **values}
                product = None
                if kind in {"competitors", "product"}:
                    code = data.pop("product_code")
                    product = products.get((code or "").strip())
                    if code and not product or kind == "product" and not product:
                        raise ValueError("商品编码不存在或不属于所选店铺")
                    data["product_id"] = str(product.id) if product else None
                schema = {
                    "competitors": CompetitorInput,
                    "store": StoreDailyInput,
                    "product": ProductDailyInput,
                    "ad": AdDailyInput,
                }[kind]
                value = schema.model_validate(data)
                if kind == "competitors":
                    key = value.code
                elif kind == "store":
                    key = value.business_date.isoformat()
                elif kind == "product":
                    key = (product.id, value.business_date.isoformat())
                else:
                    key = (value.channel, value.business_date.isoformat())
                target = targets.get(key)
                if product:
                    depend(product)
                    if product.status != "ACTIVE" and not (
                        kind == "competitors" and target and target.product_id == product.id
                    ):
                        raise ValueError("不能为已归档商品新增或修改关联业务数据")
            if target and getattr(target, "status", "ACTIVE") != "ACTIVE":
                raise ValueError("已归档记录仅可查看，不能通过导入修改")
            row.business_key = json.dumps(key, ensure_ascii=False, separators=(",", ":"))
            row.payload = value.model_dump(mode="json")
            if kind == "ad" and value.clicks > value.impressions:
                row.warnings.append("点击量大于曝光量，请核对数据口径")
            if target:
                row.action, row.expected_version = "OVERWRITE", target.version
                depend(target)
                # 仅保留本次输入涉及的列，不含敏感配置或数据库内部属性。
                row.before = {
                    k: str(getattr(target, k)) if getattr(target, k) is not None else None
                    for k in row.payload
                    if hasattr(target, k)
                }
                if kind == "products":
                    row.before["skus"] = (
                        [
                            {
                                "id": str(sku.id),
                                "code": sku.code,
                                "spec_name": sku.spec_name,
                                "price": format(sku.price, ".2f"),
                                "cost": format(sku.cost, ".2f") if sku.cost is not None else None,
                            }
                        ]
                        if sku
                        else []
                    )
        except ValidationError as exc:
            row.errors = [
                {"field": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in exc.errors()
            ]
        except ValueError as exc:
            row.errors = [{"field": "关联/状态", "message": str(exc)}]
    for group in duplicates.values():
        if len(group) > 1:
            for row in group:
                row.errors.append(
                    {"field": "业务键", "message": "文件内业务键重复，所有重复行均须修正"}
                )
    for group in product_groups.values():
        signatures = {
            json.dumps({k: v for k, v in row.payload.items() if k != "skus"}, sort_keys=True)
            for row in group
            if row.payload
        }
        if len(signatures) > 1:
            for row in group:
                row.errors.append(
                    {"field": "商品资料", "message": "同商品各 SKU 行的商品资料必须一致"}
                )
    for group in sku_groups.values():
        if len(group) > 1:
            for row in group:
                row.errors.append({"field": "sku_code", "message": "店内 SKU 编码在文件中重复"})
    return rows, dependencies
