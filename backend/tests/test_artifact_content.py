"""五类结构化产出修订契约验证脚本。

用途：验证每种正文可由生成结果无损创建修订，来源与确定性内容仍保留。
命令：uv run pytest tests/test_artifact_content.py -q。
前置：无数据库、无网络；采用受控结构化输出，不请求真实模型。
"""

import pytest
from test_generators import IDEA, SHOT, ad_output, saved

from app.artifact_content import edited_content
from app.generators import validate_output


@pytest.mark.parametrize(
    "kind", ["COMPETITOR_ANALYSIS", "THEME_IDEAS", "VIDEO_SCRIPT", "AD_STRATEGY", "BUSINESS_REVIEW"]
)
def test_each_generator_content_can_be_revised(kind):
    params = {
        "count": 1,
        "duration_seconds": 15,
        "budget": "100.00",
        "channels": ["搜索", "推荐", "内容"],
    }
    snapshot = saved(kind, params)
    if kind == "BUSINESS_REVIEW":
        snapshot["metrics"] = {
            "analysis_mode": "DATA_SUPPORTED",
            "values": {"paid_amount": {"value": "10.00"}},
        }
    output = {
        "COMPETITOR_ANALYSIS": {
            "comparison_refs": ["C1"],
            "opportunities": ["建议核验差异"],
            "risks": ["来源待复核"],
        },
        "THEME_IDEAS": {"ideas": [IDEA]},
        "VIDEO_SCRIPT": {"shots": [SHOT], "closing": "了解商品"},
        "AD_STRATEGY": ad_output(),
        "BUSINESS_REVIEW": {
            "metric_refs": ["paid_amount"],
            "anomalies": [{"metric_ref": "paid_amount", "reasoning": "原因待核验"}],
            "assumptions": ["待验证"],
            "next_actions": ["建议核对资料"],
        },
    }[kind]
    content = validate_output({**output, "title": "原始报告"}, snapshot)
    revision = edited_content("修订报告", content, snapshot, kind)
    assert revision == {**content, "title": "修订报告"}
