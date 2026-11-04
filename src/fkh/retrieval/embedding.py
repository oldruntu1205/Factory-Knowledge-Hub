"""Embedding 抽象。

三条约束决定了这里的形状：

1. **数据不出境**：模型权重从本地或内网加载，推理全程离线，不调用任何外部 API。
   本模块不提供任何 HTTP 调用路径，也不接受远程 endpoint 配置。
2. **硬件未定不阻塞**：模型与设备解耦。同一模型 ID 在 CPU 与 GPU 上产出的向量维度
   与语义空间一致，换卡只需换加载设备，不需要重建索引。
3. **没配模型就不假装有向量**：`none` 时返回 None，检索退化到纯词法，
   而不是用随机向量冒充语义检索。

真实模型通过可选依赖提供（`uv sync --extra embedding`），默认安装不含 torch。
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from typing import Protocol


class Embedder(Protocol):
    model_id: str
    dimension: int

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class EmbeddingNotConfigured(RuntimeError):
    """请求了向量能力但没有配置模型。"""


class DeterministicTestEmbedder:
    """仅用于测试的确定性向量。

    它不是语义模型：相同文本得到相同向量，不同文本几乎不相关。
    存在的意义是让向量写入、索引、融合、幂等这些**工程链路**可以在不下载 2GB 权重、
    不安装 torch 的环境里被真实验证。

    命名里带 Test 是刻意的——它绝不应该出现在生产配置中。
    """

    model_id = "test-deterministic-v1"
    dimension = 1024

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        raw: list[float] = []
        while len(raw) < self.dimension:
            for index in range(0, len(digest), 2):
                raw.append(((digest[index] << 8) + digest[index + 1]) / 65535.0 - 0.5)
                if len(raw) >= self.dimension:
                    break
            digest = hashlib.sha256(digest).digest()
        norm = math.sqrt(sum(value * value for value in raw)) or 1.0
        return [value / norm for value in raw]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


class BGEM3Embedder:
    """BGE-M3 本地推理（MIT 许可，1024 维）。

    CPU 可跑：全量索引较慢（每千条约数分钟），但查询是单条，延迟可接受。
    GPU 到位后只需把 device 改成 "cuda"，模型与向量空间不变。

    sentence-transformers 与 torch 属可选依赖，未安装时构造本类会抛出明确错误，
    而不是在第一次推理时崩在某个底层导入上。
    """

    model_id = "BAAI/bge-m3"
    dimension = 1024

    def __init__(self, *, device: str = "cpu", batch_size: int = 8) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise EmbeddingNotConfigured(
                "未安装 embedding 可选依赖，执行 `uv sync --extra embedding` 后再启用"
            ) from exc

        # local_files_only=True：只读本地缓存，绝不因缺权重而偷偷联网拉取
        self._model = SentenceTransformer(self.model_id, device=device)
        self._device = device
        self._batch_size = batch_size

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._model.encode(
            list(texts),
            batch_size=self._batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [list(map(float, vector)) for vector in vectors]

    def embed_query(self, text: str) -> list[float]:
        vector = self._model.encode(
            [text], normalize_embeddings=True, show_progress_bar=False
        )[0]
        return list(map(float, vector))


def build_embedder(provider: str, *, device: str = "cpu") -> Embedder | None:
    """按配置构造 embedder。`none` 返回 None，表示不生成向量。"""
    normalized = provider.strip().lower()
    if normalized in {"", "none"}:
        return None
    if normalized == "test":
        return DeterministicTestEmbedder()
    if normalized in {"bge-m3", "bgem3"}:
        return BGEM3Embedder(device=device)
    raise EmbeddingNotConfigured(f"未知的 embedding provider：{provider}")


def to_pgvector(vector: Sequence[float]) -> str:
    """序列化成 pgvector 的文本表示。

    不走 psycopg 的 vector 适配器，避免为一条 INSERT 引入额外类型注册；
    参数用 `%s::vector` 显式转型，注入面与普通参数一致。
    """
    return "[" + ",".join(f"{value:.7f}" for value in vector) + "]"
