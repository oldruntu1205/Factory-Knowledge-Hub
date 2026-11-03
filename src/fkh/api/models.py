"""请求模型。

与 contracts/schema/*.json 保持一一对应；契约测试会交叉校验两者，
避免出现"OpenAPI 写一套、实现收一套"的漂移。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

KnowledgeScope = Literal["customer_public", "internal_training", "internal_product"]
Language = Literal["zh-CN", "en-US"]


class AnswerRequest(BaseModel):
    """L1 §6.3 AnswerRequest。"""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    knowledge_scope: KnowledgeScope
    language: Language
    max_evidence: int = Field(default=5, ge=1, le=10)
    conversation_id: str | None = Field(default=None, max_length=128)

    @field_validator("query")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        """仅由空白组成的 query 视为无效，避免空检索打满向量库。"""
        if not value.strip():
            raise ValueError("query 不能为空白")
        return value


class TrainingAskRequest(BaseModel):
    """L1 §13.5。不接受 role / acl_policy / knowledge_scope 作为授权依据。"""

    model_config = ConfigDict(extra="forbid")

    assignment_id: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1, max_length=2000)
    language: Language


class ReviewRequest(BaseModel):
    """技术审核动作。PASS 提交审核，REJECT 退回草稿，APPROVE_INTERNAL 批准内部可用。"""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["PASS", "REJECT", "APPROVE_INTERNAL"]
    reason: str | None = Field(default=None, max_length=500)


class PublishRequest(BaseModel):
    """发布请求。audience=customer_public 时额外要求 knowledge.publish_public 权限。

    请求体里不出现审批人字段：公开审批人取自令牌，客户端无法指定"由谁批准"。
    """

    model_config = ConfigDict(extra="forbid")

    audience: Literal["internal", "customer_public"]
    reason: str | None = Field(default=None, max_length=500)
