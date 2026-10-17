"""M2商品资料生成器网络契约验证脚本。

用途：复用受控DNS/TCP夹具，校验真实HTTP JSON模式、冻结输入、响应结构与数值防编造边界。
命令：backend下 uv run pytest tests/test_product_generator.py -q。
前置：uv sync --dev；无需数据库和真实API Key，不访问外网，不产生费用。
"""

import asyncio
import json

import pytest
from test_ai_tasks import OUTPUT
from test_model_provider import SECRET, config
from test_model_provider import wire as wire

from app.core.errors import AppError
from app.product_generator import generate, validate_content

SNAPSHOT = {
    "input_params": {"goal": "改进表达"},
    "data_copy": {"product": {"name": "测试商品", "selling_points": "500克包装"}},
    "missing_items": ["无经营数据"],
}


def test_json_protocol_and_frozen_fields(wire):
    wire.body = json.dumps(
        {
            "choices": [{"message": {"content": json.dumps(OUTPUT)}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 16},
        }
    ).encode()
    result = asyncio.run(generate(config(), SECRET, ["api.deepseek.com"], SNAPSHOT))
    request = json.loads(b"".join(wire.requests).split(b"\r\n\r\n", 1)[1])
    assert request["response_format"] == {"type": "json_object"}
    assert request["max_tokens"] == 4096 and request["stream"] is False
    data = json.loads(request["messages"][1]["content"])
    assert data == {"goal": "改进表达", "product": SNAPSHOT["data_copy"]["product"]}
    assert result.content["facts"][0]["text"] == "测试商品"
    assert result.usage_tokens == {"total_tokens": 16}
    assert len(wire.targets) == 1


@pytest.mark.parametrize(
    "content,finish",
    [
        ("not-json", "stop"),
        (json.dumps(OUTPUT), "length"),
        (json.dumps({**OUTPUT, "facts": ["untrusted"]}), "stop"),
    ],
)
def test_invalid_or_truncated_response_is_not_retried(wire, content, finish):
    wire.body = json.dumps(
        {"choices": [{"message": {"content": content}, "finish_reason": finish}]}
    ).encode()
    with pytest.raises(AppError) as error:
        asyncio.run(generate(config(), SECRET, ["api.deepseek.com"], SNAPSHOT))
    assert error.value.code == "AI_OUTPUT_INVALID"
    assert len(wire.targets) == 1


@pytest.mark.parametrize("claim", ["销量达到100万", "转化率可提高百分之十", "GMV增长50%"])
def test_unsupported_business_numbers_rejected(claim):
    with pytest.raises(AppError):
        validate_content({**OUTPUT, "strengths": [claim]}, SNAPSHOT)


def test_expanded_content_cannot_exceed_limit():
    with pytest.raises(AppError):
        validate_content(
            {
                "title": "测试",
                "strengths": ["a" * 2000] * 10,
                "issues": ["b" * 2000] * 10,
                "actions": ["c" * 2000] * 10,
            },
            SNAPSHOT,
        )
