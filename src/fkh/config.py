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

    # 检索默认值（L1 §4.4 初始试验值，不是验收阈值）
    retrieval_dense_top_k: int = 30
    retrieval_lexical_top_k: int = 30
    rrf_k: int = 60
    evidence_max: int = 5


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
