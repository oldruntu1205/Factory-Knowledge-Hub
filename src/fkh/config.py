"""运行配置。

原则：凡是 L1 中标注为 ADR、尚未签署的取值，一律不设默认放行值。
缺少配置时相关能力直接不可用（fail-closed），而不是回退到某个"看起来合理"的默认。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FKH_", env_file=".env", extra="ignore")

    env: str = "local"
    service_name: str = "knowledge-api"

    # 数据库：未配置时 /health/ready 返回 degraded，问答链路不可用
    database_url: str | None = None

    # 身份：ADR-02 未签署前只支持本地 HS256；生产应改为 OIDC + JWKS
    jwt_secret: str | None = None
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

    # 检索审计：query 只以 HMAC 落库，密钥不入表
    retrieval_hash_key: str | None = None
    retrieval_hash_key_id: str = "k1"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
