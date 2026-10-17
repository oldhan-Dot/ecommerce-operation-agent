"""版本编辑复用生成器校验；确定性资料/金额/来源不可通过编辑伪造。"""

import copy
import html
import json
import re

from app.core.errors import AppError
from app.generators import validate_output
from app.product_generator import validate_content


def edited_content(title, content, snapshot, kind):
    try:
        if len(json.dumps(content, ensure_ascii=False)) > 100000:
            raise ValueError
        payload = copy.deepcopy(content.get("payload", {}))
        refs = content.get("metric_refs", [])
        raw = {
            **payload,
            "title": title,
            "metric_refs": [r["ref"] if isinstance(r, dict) else r for r in refs],
        }
        if kind == "COMPETITOR_ANALYSIS":
            raw.pop("comparison_rows", None)
        if kind == "AD_STRATEGY":
            raw["allocations"] = [
                {key: value for key, value in a.items() if key != "percent"}
                for a in payload["allocations"]
            ]
        canonical = (
            validate_content(raw, snapshot)
            if kind == "PRODUCT_ANALYSIS"
            else validate_output(raw, snapshot)
        )
        for key in content:
            if key not in canonical:
                raise ValueError
        for key in (
            "facts",
            "metrics",
            "sku_facts",
            "analysis_mode",
            "missing_items",
            "task_type",
            "schema_version",
        ):
            if key in content and content[key] != canonical[key]:
                raise ValueError
        if (
            "comparison_rows" in payload
            and payload["comparison_rows"] != canonical["payload"]["comparison_rows"]
        ):
            raise ValueError
        for item in refs:
            if isinstance(item, dict) and item not in canonical["metric_refs"]:
                raise ValueError
        return canonical
    except (AppError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise AppError(
            422, "ARTIFACT_INVALID_CONTENT", "正文结构或业务约束无效，来源及确定性指标不可修改"
        ) from None


def safe_text(value):
    """转义HTML及Markdown语法，防止导出正文夹带脚本、图片或任意链接语法。"""
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", html.escape(str(value), quote=False))


def markdown_report(artifact, version, snapshot, store, model_name, author, reviewer):
    def nullable(value, suffix=""):
        return f"{value}{suffix}" if value is not None else "N/A"

    content = version.content
    lines = [
        f"# {safe_text(version.title)}",
        "",
        f"店铺：{safe_text(store.name)}",
        f"任务类型：{safe_text(artifact.artifact_type)}",
        f"版本：v{version.version_no} · 审核状态：{version.review_status}",
        f"作者：{safe_text(author.login_name)}",
        f"模型：{safe_text(model_name)}",
        f"生成/保存时间（UTC）：{version.created_at.isoformat()}",
        f"审核人：{safe_text(reviewer.login_name) if reviewer else '未审核'}",
        f"审核时间（UTC）：{version.reviewed_at.isoformat() if version.reviewed_at else 'N/A'}",
        f"审核意见：{safe_text(version.review_comment or '无')}",
        "",
        "> AI辅助生成，经营判断和建议仍需人工核实。",
        "",
    ]
    metrics = content.get("metrics", {})
    if metrics.get("period"):
        lines += [
            f"数据期间：{metrics['period']['start_date']} 至 {metrics['period']['end_date']}",
            f"数据完整性：{metrics.get('completeness', {}).get('recorded_days', 0)}/"
            f"{metrics.get('completeness', {}).get('expected_days', 0)} 天",
            "",
        ]

    def section(title, items):
        lines.extend([f"## {title}", ""])
        for item in items:
            lines.append("- " + safe_text(item))
        lines.append("")

    section("已录入资料", [f["text"] for f in content.get("facts", [])])
    if metrics.get("values"):
        section(
            "服务端计算指标",
            [
                f"{v.get('label', k)}：{v['value'] if v['value'] is not None else 'N/A'}；"
                f"上期 {nullable(v.get('previous_value'))}；"
                f"正式环比 {nullable(v.get('change_percent'), '%')}"
                for k, v in metrics["values"].items()
            ],
        )
    section("待验证判断", [i["text"] for i in content.get("hypotheses", [])])
    section("建议行动", [i["text"] for i in content.get("recommendations", [])])

    # 嵌套类型按字段递归为纯文本列表，不执行用户提供的Markdown/HTML。
    def walk(value, depth=0):
        if isinstance(value, dict):
            for key, item in value.items():
                lines.append("  " * depth + "- " + safe_text(key) + ":")
                walk(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                walk(item, depth)
        else:
            lines.append("  " * depth + "- " + safe_text("N/A" if value is None else value))

    lines += ["## 类型正文", ""]
    walk(content.get("payload", {}))
    section("缺失与约束", content.get("missing_items", []))
    section(
        "来源清单",
        [
            f"{r['ref']} · {r['entity_type']} #{r['id']} · 资料版本 {r['version']}"
            for r in snapshot.source_refs
        ],
    )
    for competitor in snapshot.data_copy.get("competitors", []):
        lines.append(
            safe_text(
                f"来源：{competitor.get('source_note', '')} · {competitor.get('source_url', '')} · "
                f"采集日期：{competitor.get('collected_at', '')}"
            )
        )
    return "\n".join(lines) + "\n"
