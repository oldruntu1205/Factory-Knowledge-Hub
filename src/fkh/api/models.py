"""请求模型。

与 contracts/schema/*.json 保持一一对应；契约测试会交叉校验两者，
避免出现"OpenAPI 写一套、实现收一套"的漂移。
"""

from __future__ import annotations

from typing import Any, Literal

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


class SearchRequest(BaseModel):
    """词法检索请求（Phase 2 的检索侧，不含生成）。

    `knowledge_scope` 只表达"想查哪个范围"，**不是**授权依据：
    服务端还会校验令牌 allowed_scopes 是否包含该范围，未包含直接拒绝。
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    knowledge_scope: KnowledgeScope
    max_results: int = Field(default=10, ge=1, le=50)

    @field_validator("query")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query 不能为空白")
        return value


class ContextRequest(BaseModel):
    """检索并组装生成用上下文（内部端点）。

    这是问答链路的前置步骤，不是对外承诺的接口。权限与 `knowledge.search` 相同，
    额外限制上下文预算，避免被用来批量拉取全文。
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    knowledge_scope: KnowledgeScope
    max_results: int = Field(default=10, ge=1, le=50)
    max_blocks: int = Field(default=8, ge=1, le=20)
    max_tokens: int = Field(default=6000, ge=500, le=20000)

    @field_validator("query")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query 不能为空白")
        return value


class CourseCreateRequest(BaseModel):
    """创建课程草稿。岗位按 code 取或建，避免调用方先查岗位 id。"""

    model_config = ConfigDict(extra="forbid")

    role_code: str = Field(min_length=1, max_length=64)
    role_name: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=200)
    knowledge_version_ids: list[str] = Field(default_factory=list, max_length=20)


class QuestionCreateRequest(BaseModel):
    """新增题目。默认以 APPROVED 入库是没有默认值的替代：

    题目本身没有审批流时，DRAFT 状态会让课程永远无法发布。这里把"是否已审"
    交给调用方显式声明，缺省视为已审，便于课程发布校验把草稿题挡在门外。
    """

    model_config = ConfigDict(extra="forbid")

    question_type: Literal["SINGLE_CHOICE", "MULTI_CHOICE", "TRUE_FALSE", "SHORT_ANSWER"]
    prompt: str = Field(min_length=1, max_length=1000)
    answer_key: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list, max_length=10)
    status: Literal["DRAFT", "APPROVED"] = "APPROVED"


class AssignmentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    course_id: str
    learner_subject_id: str = Field(min_length=1, max_length=200)
    cohort_id: str | None = Field(default=None, max_length=64)


class AttemptSubmitRequest(BaseModel):
    """提交答卷。

    刻意**不含** score 字段：extra="forbid" 会让多传的分数直接被拒，
    客户端没有机会影响成绩。
    """

    model_config = ConfigDict(extra="forbid")

    assignment_id: str
    responses: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=128)


class SignoffRequest(BaseModel):
    """导师实操签核。刻意不含 assessor 字段：签核人只能来自令牌。"""

    model_config = ConfigDict(extra="forbid")

    assignment_id: str
    decision: Literal["PASS", "FAIL", "RETRY"]
    evidence_uri: str | None = Field(default=None, max_length=500)
