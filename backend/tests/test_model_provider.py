"""DeepSeek 网络适配器离线测试脚本。

用途与范围：验证 DNS 全量过滤、固定目标/SNI、HTTP 协议、超时、大小限制和错误脱敏。
运行命令：backend 下 uv run pytest tests/test_model_provider.py -q。
前置：uv sync --dev；全部 DNS 和 TCP 都替换为受控响应，无数据库、真实密钥或外网调用。
"""

import asyncio
import json
import socket
import ssl
from types import SimpleNamespace

import httpcore
import pytest

from app import model_provider as provider
from app.core.errors import AppError

SECRET = "invalid-demo-key"
PAYLOAD = {"choices": [{"message": {"content": "OK"}}], "usage": {"total_tokens": 8}}


def config(**changes):
    return SimpleNamespace(
        **{
            "endpoint": "https://api.deepseek.com",
            "model_name": "test-model",
            "timeout_seconds": 1,
            **changes,
        }
    )


@pytest.fixture
def wire(monkeypatch):
    """保留真实 httpcore HTTP 状态机，仅截获 DNS/TCP 与 TLS 握手边界。"""
    state = SimpleNamespace(
        ips=["8.8.8.8"],
        status=200,
        body=json.dumps(PAYLOAD).encode(),
        headers=b"",
        requests=[],
        targets=[],
        tls=[],
        closed=False,
        failure=None,
        delay=0,
        dns_delay=0,
    )

    async def dns(*args, **kwargs):
        await asyncio.sleep(state.dns_delay)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in state.ips]

    # 测试在独立事件循环中运行，因此替换事件循环类上的解析方法。
    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", dns)

    class Stream(httpcore.AsyncNetworkStream):
        def __init__(self):
            self.sent = False

        async def read(self, max_bytes, timeout=None):
            await asyncio.sleep(state.delay)
            if self.sent:
                return b""
            self.sent = True
            return (
                f"HTTP/1.1 {state.status} Test\r\nContent-Length: {len(state.body)}\r\n".encode()
                + state.headers
                + b"\r\n"
                + state.body
            )

        async def write(self, buffer, timeout=None):
            state.requests.append(buffer)

        async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            state.tls.append((server_hostname, ssl_context.check_hostname, ssl_context.verify_mode))
            return self

        async def aclose(self):
            state.closed = True

    class Backend(httpcore.AsyncNetworkBackend):
        async def connect_tcp(self, host, port, **kwargs):
            state.targets.append((host, port))
            if state.failure:
                raise state.failure
            return Stream()

    monkeypatch.setattr(httpcore, "AnyIOBackend", Backend)
    return state


def run_probe(**changes):
    return asyncio.run(provider.probe(config(**changes), SECRET, ["api.deepseek.com"]))


def test_pinned_target_with_original_tls_and_host(wire, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    result = run_probe(endpoint="https://api.deepseek.com/v1")
    assert result.usage_tokens == {"total_tokens": 8} and result.latency_ms >= 0
    assert wire.targets == [("8.8.8.8", 443)]
    assert wire.tls == [("api.deepseek.com", True, ssl.CERT_REQUIRED)]
    request = b"".join(wire.requests)
    assert b"POST /v1/chat/completions HTTP/1.1" in request
    assert b"Host: api.deepseek.com" in request and SECRET.encode() in request
    assert json.loads(request.split(b"\r\n\r\n", 1)[1])["max_tokens"] == 16
    assert wire.closed


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.1.1.1",
        "169.254.169.254",
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:8.8.8.8",
        "192.0.2.1",
    ],
)
def test_mixed_dns_rejected_before_connect(wire, ip):
    wire.ips.append(ip)
    with pytest.raises(AppError) as exc:
        run_probe()
    assert exc.value.code == "MODEL_HOST_NOT_ALLOWED"
    assert not wire.targets and not wire.requests


def test_empty_dns_rejected(wire):
    wire.ips = []
    with pytest.raises(AppError):
        run_probe()
    assert not wire.targets


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://api.deepseek.com",
        "https://localhost",
        "https://api.deepseek.com:8443",
        "https://api.deepseek.com?key=secret",
    ],
)
def test_revalidate_saved_endpoint(wire, endpoint):
    with pytest.raises(AppError) as exc:
        run_probe(endpoint=endpoint)
    assert exc.value.code == "MODEL_HOST_NOT_ALLOWED" and not wire.targets


def test_allowlist_required(wire):
    with pytest.raises(AppError):
        asyncio.run(provider.probe(config(), SECRET, []))
    assert not wire.targets


@pytest.mark.parametrize("status", [302, 401, 429, 500])
def test_status_never_redirects_or_retries_and_redacts(wire, status):
    wire.status, wire.body = status, SECRET.encode()
    wire.headers = b"Location: http://169.254.169.254/\r\n"
    with pytest.raises(AppError) as exc:
        run_probe()
    assert exc.value.code == "MODEL_PROVIDER_ERROR"
    assert SECRET not in exc.value.message and len(wire.targets) == 1 and wire.closed


@pytest.mark.parametrize(
    "failure,code",
    [
        (httpcore.ConnectError(SECRET), "MODEL_PROVIDER_ERROR"),
        (httpcore.ConnectTimeout(SECRET), "MODEL_TIMEOUT"),
        (ssl.SSLCertVerificationError(SECRET), "MODEL_PROVIDER_ERROR"),
    ],
)
def test_connection_errors_sanitized(wire, failure, code):
    wire.failure = failure
    with pytest.raises(AppError) as exc:
        run_probe()
    assert exc.value.code == code and SECRET not in exc.value.message
    assert len(wire.targets) == 1


@pytest.mark.parametrize("stage", ["dns", "response"])
def test_total_deadline_includes_dns_and_body(wire, stage):
    if stage == "dns":
        wire.dns_delay = 0.1
    else:
        wire.delay = 0.1
    with pytest.raises(AppError) as exc:
        run_probe(timeout_seconds=0.05)
    assert exc.value.code == "MODEL_TIMEOUT"
    assert (not wire.targets) if stage == "dns" else wire.closed


def test_response_size_limit(wire):
    wire.body = b"x" * (provider.MAX_RESPONSE_BYTES + 1)
    with pytest.raises(AppError) as exc:
        run_probe()
    assert exc.value.code == "MODEL_RESPONSE_TOO_LARGE" and wire.closed


def test_compression_rejected(wire):
    wire.headers = b"Content-Encoding: gzip\r\n"
    with pytest.raises(AppError) as exc:
        run_probe()
    assert exc.value.code == "MODEL_INVALID_RESPONSE" and wire.closed


@pytest.mark.parametrize(
    "raw",
    [
        b"not-json",
        b"null",
        b"[]",
        b"{}",
        b'{"choices":[{"message":{"content":" "}}]}',
        b'{"choices":[{"message":{"content":123}}]}',
        b'{"choices":[{"message":{"content":"OK"}}],"usage":null}',
    ],
)
def test_invalid_response_rejected(wire, raw):
    wire.body = raw
    with pytest.raises(AppError) as exc:
        run_probe()
    assert exc.value.code == "MODEL_INVALID_RESPONSE"


def test_only_safe_token_counters_returned(wire):
    wire.body = json.dumps(
        {
            **PAYLOAD,
            "usage": {
                "prompt_tokens": True,
                "completion_tokens": -1,
                "total_tokens": 2**64,
                "secret": SECRET,
            },
        }
    ).encode()
    assert run_probe().usage_tokens == {}
