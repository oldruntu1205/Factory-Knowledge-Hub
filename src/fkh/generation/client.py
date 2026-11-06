"""本地推理网关（ADR-04 未签期的唯一允许形态）。

约束来自 `docs/ADR/README.md`：ADR-04（模型供应商 / 跨境传输）未签署前，
Gateway 不启用外部调用，**只允许本地推理或拒答**。因此这里：

- 只连 loopback：构造时把 base_url 解析成 IP 并逐个校验 `is_loopback()`；
  `localhost` 必须解析到 loopback，解析不到就拒绝；
- **不经环境代理、不跟随重定向**：直接用 `http.client`，而不是会读 `HTTP_PROXY`
  的 urllib；3xx 一律当失败（不跟到别处）；
- 未配置模型 = 未配置能力（fail-closed），由调用方返回 503；
- 模型标识带 digest（`name@sha256前 12 位`），保证审计能定位到不可变版本，而不是可变的 tag；
- 推理失败/超时抛 `GenerationError`，与"知识不足"严格分开。

该模块不提供任何工具调用能力：模型只能产出文本，引用由 `retrieval/citation.py` 按白名单核验。
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import urllib.parse
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class GenerationNotConfigured(RuntimeError):
    """没有可用的本地模型（未配置 provider / 模型名 / base url 非本地 / 模型不存在）。"""


class GenerationError(RuntimeError):
    """已配置的本地模型执行失败（超时、连接失败、响应不可解析、重定向）。"""


@dataclass(frozen=True, slots=True)
class GenerationResult:
    text: str
    model_id: str


class TextGenerator(Protocol):
    model_id: str

    def generate(self, prompt: str, *, max_tokens: int, temperature: float) -> GenerationResult: ...


def _resolve_loopback(host: str, port: int) -> str:
    """把主机名解析成 IP，要求**所有**解析结果都是 loopback，并返回用于连接的数值 IP。

    返回 IP 而不是原主机名是刻意的：`HTTPConnection("localhost")` 会在建连时**再次解析**，
    那时 DNS 可能已经变了（hosts 被改、DNS rebinding），校验过的地址就不再是被连接的地址。
    绑定到已校验的 IP 才能保证"校验的"与"连接的"是同一个目标。
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise GenerationNotConfigured(f"无法解析推理地址 {host!r}：{exc}") from exc
    if not infos:
        raise GenerationNotConfigured(f"推理地址 {host!r} 没有解析结果")

    loopbacks: list[str] = []
    for info in infos:
        address = str(info[4][0]).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise GenerationNotConfigured(f"推理地址解析出非法 IP：{address!r}") from exc
        if not ip.is_loopback:
            raise GenerationNotConfigured(
                f"本地推理只允许 loopback，{host!r} 解析到 {address}"
            )
        loopbacks.append(address)

    # 优先 IPv4：Ollama 默认监听 127.0.0.1，走 ::1 在部分 Windows 配置下会连不上
    return next((address for address in loopbacks if ":" not in address), loopbacks[0])


class OllamaGenerator:
    """Ollama 本地推理（`POST /api/generate`，stream=false）。

    用标准库而不是引入 SDK：只有一次 POST，少一个依赖就少一处供应链与版本漂移。
    """

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:11434",
        model: str,
        timeout_seconds: float = 60.0,
    ) -> None:
        if not model.strip():
            raise GenerationNotConfigured("未配置本地模型名（FKH_LLM_MODEL）")
        parsed = urllib.parse.urlparse(base_url)
        host = (parsed.hostname or "").strip().lower()
        if host not in LOOPBACK_HOSTS:
            raise GenerationNotConfigured(
                f"本地推理只允许 loopback 地址，当前为 {base_url!r}（host={host!r}）"
            )
        if parsed.scheme not in {"http", "https"}:
            raise GenerationNotConfigured(f"不支持的推理地址协议：{parsed.scheme!r}")
        default_port = 443 if parsed.scheme == "https" else 80
        port = parsed.port or default_port
        # 绑定到已校验的数值 IP：连接阶段不再做任何名字解析
        connect_ip = _resolve_loopback(host, port)

        self._host = connect_ip
        self._server_name = host
        self._port = port
        self._scheme = parsed.scheme
        self._timeout = timeout_seconds
        self._model = model.strip()
        digest = self._fetch_digest()
        # 审计要能定位不可变版本：tag 会漂移，digest 不会
        self.model_id = f"{self._model}@{digest[:12]}" if digest else self._model

    # ---- HTTP（不经代理、不跟重定向）------------------------------------
    def _connection(self) -> http.client.HTTPConnection:
        cls = (
            http.client.HTTPSConnection
            if self._scheme == "https"
            else http.client.HTTPConnection
        )
        return cls(self._host, self._port, timeout=self._timeout)

    def _request(self, path: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn = self._connection()
        try:
            conn.request("POST" if body is not None else "GET", path, body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            status = response.status
        except (OSError, http.client.HTTPException) as exc:
            raise GenerationError(f"本地推理不可达：{exc}") from exc
        finally:
            conn.close()

        if 300 <= status < 400:
            # 不跟随重定向：否则本机地址可能把请求转到非本地
            raise GenerationError(f"本地推理返回重定向（{status}），拒绝跟随")
        if status != 200:
            raise GenerationError(f"本地推理返回 HTTP {status}")
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GenerationError("本地推理返回的不是 JSON") from exc
        if not isinstance(decoded, dict):
            raise GenerationError("本地推理响应结构非法")
        return decoded

    def _fetch_digest(self) -> str:
        data = self._request("/api/tags", None)
        for item in data.get("models", []) or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if name == self._model or name == f"{self._model}:latest":
                digest = str(item.get("digest") or "")
                return digest
        raise GenerationNotConfigured(
            f"本机 Ollama 未安装模型 {self._model!r}（不会自动拉取，也不会回退到云端）"
        )

    # ---- 生成 --------------------------------------------------------------
    def generate(self, prompt: str, *, max_tokens: int, temperature: float) -> GenerationResult:
        data = self._request(
            "/api/generate",
            {
                "model": self._model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": temperature, "num_predict": max_tokens},
            },
        )
        text = data.get("response")
        if not isinstance(text, str):
            raise GenerationError("本地推理响应缺少 response 字段")
        # 只取 response：qwen3 的 thinking 在独立字段里，不得混入最终答案
        return GenerationResult(text=text, model_id=self.model_id)


class DeterministicGenerator:
    """仅用于测试：返回固定文本，不联网、不加载模型。

    `build_generator` 只接受 none/ollama，生产配置里不会出现它。
    """

    def __init__(self, text: str, *, model_id: str = "test-deterministic@000000000000") -> None:
        self.model_id = model_id
        self._text = text
        self.calls: list[str] = []

    def generate(self, prompt: str, *, max_tokens: int, temperature: float) -> GenerationResult:
        self.calls.append(prompt)
        return GenerationResult(text=self._text, model_id=self.model_id)


def build_generator(
    provider: str,
    *,
    base_url: str,
    model: str | None,
    timeout_seconds: float,
) -> TextGenerator | None:
    """按配置构造生成器；`none` 返回 None（调用方据此 503）。"""
    normalized = provider.strip().lower()
    if normalized in {"", "none"}:
        return None
    if normalized == "ollama":
        if not model:
            raise GenerationNotConfigured("provider=ollama 但未配置 FKH_LLM_MODEL")
        return OllamaGenerator(base_url=base_url, model=model, timeout_seconds=timeout_seconds)
    raise GenerationNotConfigured(f"未知的推理 provider：{provider}（只允许 none/ollama）")


@lru_cache(maxsize=4)
def _cached_generator(
    provider: str, base_url: str, model: str, timeout_seconds: float
) -> TextGenerator | None:
    """模型探测（/api/tags）只做一次；失败不缓存，配置修好后可重试。"""
    return build_generator(
        provider, base_url=base_url, model=model, timeout_seconds=timeout_seconds
    )


def get_generator(
    provider: str, *, base_url: str, model: str | None, timeout_seconds: float
) -> TextGenerator | None:
    return _cached_generator(
        provider.strip().lower(), base_url, (model or "").strip(), timeout_seconds
    )


__all__ = [
    "DeterministicGenerator",
    "GenerationError",
    "GenerationNotConfigured",
    "GenerationResult",
    "LOOPBACK_HOSTS",
    "OllamaGenerator",
    "TextGenerator",
    "build_generator",
    "get_generator",
]
