"""DeepSeek 单次探测适配器；由持久调用调度器负责授权、额度和重放保护。"""

import asyncio
import ipaddress
import json
import socket
import ssl
import time
from dataclasses import dataclass

import httpcore

from app.core.errors import AppError
from app.model_settings import ModelSettingsInput

MAX_RESPONSE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ProbeResult:
    latency_ms: int
    usage_tokens: dict[str, int]


async def resolve_public(host: str) -> str:
    """核对所有 DNS 结果；混入一个非公网地址也拒绝，返回固定连接目标。"""
    answers = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    addresses = [ipaddress.ip_address(answer[4][0]) for answer in answers]
    if not addresses or any(
        not ip.is_global
        or ip.is_multicast
        or (isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None)
        for ip in addresses
    ):
        raise AppError(422, "MODEL_HOST_NOT_ALLOWED", "模型域名未解析到允许的公网地址")
    return str(addresses[0])


class PinnedBackend(httpcore.AsyncNetworkBackend):
    """只替换 TCP 目标；HTTP Host、TLS SNI 和证书校验仍使用官方域名。"""

    def __init__(self, address: str):
        self.address = address
        self.backend = httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if host != "api.deepseek.com" or port != 443:
            raise AppError(422, "MODEL_HOST_NOT_ALLOWED", "模型请求目标不受支持")
        return await self.backend.connect_tcp(
            self.address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )


def parse_probe(raw: bytes) -> dict[str, int]:
    """只提取有界计数，丢弃原文和供应商错误，防止密钥或内容进入调用记录。"""
    try:
        payload = json.loads(raw)
        content = payload["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError
        usage = payload.get("usage", {})
        if not isinstance(usage, dict):
            raise ValueError
        return {
            name: value
            for name in ("prompt_tokens", "completion_tokens", "total_tokens")
            if type(value := usage.get(name)) is int and 0 <= value <= 2**63 - 1
        }
    except (ValueError, TypeError, KeyError, IndexError, UnicodeError, RecursionError):
        raise AppError(502, "MODEL_INVALID_RESPONSE", "模型返回内容无效或为空") from None


async def completion(config, secret, allowed_hosts, *, messages, max_tokens, json_mode=False):
    """共享单次请求传输；无代理、重定向或重试，总超时包含 DNS。"""
    try:
        endpoint = ModelSettingsInput.endpoint_format(config.endpoint)
    except ValueError:
        raise AppError(422, "MODEL_HOST_NOT_ALLOWED", "模型请求地址不受支持") from None
    if "api.deepseek.com" not in {host.lower() for host in allowed_hosts}:
        raise AppError(422, "MODEL_HOST_NOT_ALLOWED", "模型域名未加入服务端允许集合")
    started = time.monotonic()
    try:
        async with asyncio.timeout(config.timeout_seconds):
            address = await resolve_public("api.deepseek.com")
            async with httpcore.AsyncConnectionPool(
                ssl_context=ssl.create_default_context(),
                network_backend=PinnedBackend(address),
                retries=0,
                max_connections=1,
                max_keepalive_connections=0,
                http2=False,
            ) as pool:
                body = json.dumps(
                    {
                        "model": config.model_name,
                        "messages": messages,
                        "max_tokens": max_tokens,
                        "stream": False,
                        **({"response_format": {"type": "json_object"}} if json_mode else {}),
                    }
                ).encode()
                async with pool.stream(
                    "POST",
                    endpoint + "/chat/completions",
                    headers={
                        "Host": "api.deepseek.com",
                        "Authorization": "Bearer " + secret,
                        "Content-Type": "application/json",
                        "Content-Length": str(len(body)),
                        "Accept-Encoding": "identity",
                    },
                    content=body,
                    extensions={"timeout": {"connect": 10, "read": 30, "write": 10, "pool": 5}},
                ) as response:
                    if response.status != 200:
                        raise AppError(
                            502, "MODEL_PROVIDER_ERROR", "模型服务拒绝请求，请核对配置和账户"
                        )
                    if any(
                        name.lower() == b"content-encoding" and value.lower() != b"identity"
                        for name, value in response.headers
                    ):
                        raise AppError(502, "MODEL_INVALID_RESPONSE", "模型返回编码不受支持")
                    raw = bytearray()
                    async for chunk in response.aiter_stream():
                        if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise AppError(502, "MODEL_RESPONSE_TOO_LARGE", "模型响应超过大小限制")
                        raw.extend(chunk)
            return bytes(raw), round((time.monotonic() - started) * 1000)
    except (TimeoutError, httpcore.TimeoutException):
        raise AppError(504, "MODEL_TIMEOUT", "模型请求超时，请稍后查看调用记录") from None
    except (OSError, httpcore.NetworkError, httpcore.ProtocolError):
        raise AppError(502, "MODEL_PROVIDER_ERROR", "模型服务连接失败，请核对网络和配置") from None


async def probe(config, secret: str, allowed_hosts: list[str]) -> ProbeResult:
    """连接测试只发送固定文字，返回计数而不保留供应商正文。"""
    raw, latency = await completion(
        config,
        secret,
        allowed_hosts,
        messages=[{"role": "user", "content": "Reply only with OK."}],
        max_tokens=16,
    )
    return ProbeResult(latency, parse_probe(raw))
