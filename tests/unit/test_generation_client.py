"""本地推理网关的边界：只连 loopback、不跟重定向、不吃环境代理、模型必须显式且存在。

2026-11-06 L1 裁决 §第 1 项要求"仅检查 URL 字符串不足"，所以这里用**真实回环 HTTP 服务**
验证连接行为，而不是只断言字符串。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from fkh.generation import (
    DeterministicGenerator,
    GenerationError,
    GenerationNotConfigured,
    OllamaGenerator,
    build_generator,
)


def test_non_loopback_url_is_refused_before_any_network() -> None:
    with pytest.raises(GenerationNotConfigured, match="loopback"):
        OllamaGenerator(base_url="http://8.8.8.8:11434", model="qwen3:14b")


def test_localhost_must_resolve_to_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """`localhost` 被 hosts 改指到外部地址时必须拒绝，而不是"名字看着安全就放行"。"""
    import socket

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 11434))],
    )
    with pytest.raises(GenerationNotConfigured, match="loopback"):
        OllamaGenerator(base_url="http://localhost:11434", model="qwen3:14b")


def test_connection_binds_validated_ip_and_never_re_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """校验的地址必须就是连接的地址：不能在建连时再解析一次主机名。

    否则"校验时是 loopback、连接时被 hosts/DNS rebinding 指到外部"就能绕过检查。
    """
    import ipaddress
    import socket

    with _Server() as server:
        seen: list[str] = []
        real_getaddrinfo = socket.getaddrinfo

        def counting(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
            seen.append(str(host))
            return real_getaddrinfo(host, port, *args, **kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", counting)
        generator = OllamaGenerator(base_url=f"http://localhost:{server.port}", model="qwen3:14b")

    assert ipaddress.ip_address(generator._host).is_loopback  # noqa: SLF001 - 断言绑定目标
    # 构造期：先解析主机名（校验），随后 http.client 对**数值 IP** 再解析一次用于建连
    assert seen[0] == "localhost", f"首次必须是主机名校验，实际 {seen}"
    assert "localhost" not in seen[1:], f"校验后不得再出现主机名解析，实际 {seen}"

    recorded: dict[str, Any] = {}

    def fake_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        recorded["address"] = address
        raise OSError("测试中阻止真实建连")

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    with pytest.raises(GenerationError):
        generator.generate("提示", max_tokens=8, temperature=0.0)

    assert recorded["address"][0] == generator._host  # noqa: SLF001
    assert str(recorded["address"][0]) != "localhost", "连接目标不能是主机名"
    # 生成阶段仍可能出现一次对**数值 IP** 的解析（http.client 内部 create_connection），
    # 但绝不能再出现主机名——IP 字面量解析结果不会变，主机名会。
    assert "localhost" not in seen[1:], f"生成阶段出现了主机名解析：{seen}"


def test_unknown_provider_and_missing_model_are_refused() -> None:
    assert build_generator("none", base_url="http://127.0.0.1:11434", model=None, timeout_seconds=5) is None
    with pytest.raises(GenerationNotConfigured):
        build_generator("ollama", base_url="http://127.0.0.1:11434", model=None, timeout_seconds=5)
    with pytest.raises(GenerationNotConfigured, match="未知"):
        build_generator("openai", base_url="http://127.0.0.1:11434", model="x", timeout_seconds=5)


class _Server:
    """最小回环服务：GET 答 /api/tags，POST 答生成；可切换 302 验证不跟随重定向。"""

    def __init__(
        self,
        *,
        status: int = 200,
        tags_body: dict[str, Any] | None = None,
        generate_body: dict[str, Any] | None = None,
        location: str | None = None,
    ) -> None:
        self.status = status
        self.tags_body = tags_body if tags_body is not None else _tags()
        self.generate_body = generate_body or {}
        self.location = location
        self.hits: list[str] = []

        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server 协议
                server.hits.append(self.path)
                self._respond(server.tags_body)

            def do_POST(self) -> None:  # noqa: N802 - http.server 协议
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                server.hits.append(self.path)
                self._respond(server.generate_body)

            def _respond(self, body: dict[str, Any]) -> None:
                self.send_response(server.status)
                if server.location:
                    self.send_header("Location", server.location)
                payload = json.dumps(body).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: Any) -> None:  # 静音
                return

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> _Server:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def _tags(name: str = "qwen3:14b", digest: str = "bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8") -> dict[str, Any]:
    return {"models": [{"name": name, "digest": digest}]}


def test_model_digest_is_recorded_for_audit_traceability() -> None:
    with _Server() as server:
        generator = OllamaGenerator(base_url=f"http://127.0.0.1:{server.port}", model="qwen3:14b")
    assert generator.model_id == "qwen3:14b@bdbd181c33f2"


def test_missing_model_is_refused_not_pulled() -> None:
    with _Server(tags_body={"models": []}) as server, pytest.raises(
        GenerationNotConfigured, match="未安装"
    ):
        OllamaGenerator(base_url=f"http://127.0.0.1:{server.port}", model="ghost:1b")


def test_redirect_is_not_followed() -> None:
    with _Server(status=302, location="http://example.com/api/tags") as server:
        with pytest.raises(GenerationError, match="重定向"):
            OllamaGenerator(base_url=f"http://127.0.0.1:{server.port}", model="qwen3:14b")
        assert server.hits == ["/api/tags"], "不得跟随重定向产生第二次请求"


def test_generation_ignores_thinking_field_and_environment_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """qwen3 的思考过程在独立字段；同时验证 HTTP_PROXY 不会把请求带出本机。"""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    body = {"response": "接口 24/29。", "thinking": "内部推理不应出现在答案里", "model": "qwen3:14b"}
    with _Server(generate_body=body) as server:
        generator = OllamaGenerator(base_url=f"http://127.0.0.1:{server.port}", model="qwen3:14b")
        result = generator.generate("提示", max_tokens=16, temperature=0.0)
    assert result.text == "接口 24/29。"
    assert "内部推理" not in result.text
    assert server.hits == ["/api/tags", "/api/generate"]


def test_deterministic_generator_is_not_reachable_from_production_config() -> None:
    """测试替身只能直接构造，不能通过 provider 配置混进生产。"""
    with pytest.raises(GenerationNotConfigured, match="未知"):
        build_generator("deterministic", base_url="http://127.0.0.1:11434", model="x", timeout_seconds=5)
    assert DeterministicGenerator("x").model_id.startswith("test-deterministic")


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """这些用例只允许连回环；连外网一律视为失败。"""
    import socket

    real_connect = socket.socket.connect

    def guarded(self: Any, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host not in {"127.0.0.1", "::1", "localhost"}:
            raise AssertionError(f"测试试图连接非本机地址：{address}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    yield


__all__: list[str] = []
