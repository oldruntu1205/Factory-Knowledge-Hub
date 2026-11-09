"""运行配置。

原则：凡是 L1 中标注为 ADR、尚未签署的取值，一律不设默认放行值。
缺少配置时相关能力直接不可用（fail-closed），而不是回退到某个"看起来合理"的默认。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FKH_", env_file=".env", extra="ignore")

    env: str = "local"
    service_name: str = "knowledge-api"

    # 数据库：未配置时 /health/ready 返回 degraded，问答链路不可用
    database_url: str | None = None

    # 身份：ADR-02 未签署前只支持本地 HS256；生产应改为 OIDC + JWKS
    jwt_secret: str | None = None
    # 轮换宽限期：旧密钥只用于验签，不用于签发；顺序无关，命中即通过。
    # 由实例工具在 rotate-keys 后注入，宽限期结束用 retire-jwt-previous 删除。
    jwt_previous_secrets: list[str] = Field(default_factory=list, max_length=2)
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "fkh.local"
    jwt_audience: str = "fkh-api"

    # outbox 投递：传输层未配置时 relay 拒绝启动（fail-closed），不静默丢事件
    outbox_transport: str = "none"
    outbox_webhook_url: str | None = None
    outbox_timeout_seconds: float = 10.0
    outbox_producer: str = "knowledge-api"
    outbox_batch_size: int = 20
    outbox_lease_seconds: int = 30
    outbox_max_attempts: int = 8
    outbox_backoff_base_seconds: float = 2.0
    outbox_backoff_cap_seconds: float = 300.0

    # 检索默认值（L1 §4.4 初始试验值，不是验收阈值）
    retrieval_dense_top_k: int = 30
    retrieval_lexical_top_k: int = 30
    rrf_k: int = 60
    evidence_max: int = 5
    # 召回模式（2026-11-04 L1 判决 Q2）：默认纯词法，混合检索只能服务端显式 opt-in。
    # 这是服务端配置，不是请求参数——调用方不能自己选择召回算法。
    retrieval_mode: str = "lexical"       # lexical | hybrid

    # 检索审计：query 只以 HMAC 落库，密钥不入表
    retrieval_hash_key: str | None = None
    retrieval_hash_key_id: str = "k1"

    # 本地推理（ADR-04 未签期的允许形态：只允许本地推理或拒答）
    # provider=none 时问答端点 503（能力未配置），不退回任何默认模型。
    llm_provider: str = "none"            # none | ollama
    llm_base_url: str = "http://127.0.0.1:11434"  # 只允许 loopback，非本地地址构造即拒绝
    llm_model: str | None = None           # 必须显式配置，例如 qwen3:14b
    llm_timeout_seconds: float = 60.0
    llm_max_tokens: int = 1024
    llm_temperature: float = 0.0
    # 外部客服的商务承诺词表：内置价期质保/定制等词不可删除，这里只允许按实例追加
    # 行业词（逗号分隔），避免换行业时只能改内核。
    commitment_keywords_extra: str = ""

    # Embedding（ADR-04/06）。数据不出境：只用本地权重，不提供任何远程 endpoint。
    # provider=none 时纯词法检索，不生成向量，也不假装有语义能力。
    embedding_provider: str = "none"      # none | bge-m3 | test（test 仅限测试）
    embedding_device: str = "cpu"         # cpu | cuda；同模型换设备不需要重建索引
    embedding_batch_size: int = 8


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
