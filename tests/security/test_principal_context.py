"""令牌 → 主体上下文的映射测试。

存在的理由：ACL 判定用 `authz.policy.Subject`，HTTP 层用 `PrincipalContext`。
两者字段一旦不同步，按部门/用户组授权的规则会在真实请求下静默失效——
不报错、只是永远不匹配。这类缺陷在单元测试里看不出来，只有把两条链路接起来才暴露。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest

from fkh.api.errors import ApiError
from fkh.api.security import decode_token
from fkh.authz.policy import AclPolicy, Decision, Subject, evaluate
from fkh.config import Settings

SECRET = "principal-context-secret-0123456789abcdef"


def _settings() -> Settings:
    return Settings(jwt_secret=SECRET)


def _token(**overrides: Any) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "iss": "fkh.local",
        "aud": "fkh-api",
        "sub": "user-a@example.test",
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "tenant_id": str(uuid.uuid4()),
        "principal_id": str(uuid.uuid4()),
        "roles": ["staff"],
        "scope": ["knowledge.answer"],
        "allowed_scopes": ["internal_product"],
    }
    payload.update(overrides)
    return jwt.encode(payload, SECRET, algorithm="HS256")


def test_groups_and_departments_are_parsed() -> None:
    token = _token(groups=["外贸组"], departments=["tech"])
    context = decode_token(token, _settings())
    assert context.groups == frozenset({"外贸组"})
    assert context.departments == frozenset({"tech"})


def test_missing_group_claims_default_to_empty() -> None:
    context = decode_token(_token(), _settings())
    assert context.groups == frozenset()
    assert context.departments == frozenset()


def test_subject_mapping_preserves_all_dimensions() -> None:
    context = decode_token(
        _token(roles=["staff", "tech"], groups=["外贸组"], departments=["tech"]), _settings()
    )
    subject = context.to_subject()
    assert isinstance(subject, Subject)
    assert subject.principal_id == str(context.principal_id)
    assert subject.tenant_id == str(context.tenant_id)
    assert subject.roles == {"staff", "tech"}
    assert subject.groups == {"外贸组"}
    assert subject.departments == {"tech"}


def test_department_policy_works_through_token_path() -> None:
    """按部门授权的策略，必须能通过真实令牌链路命中。

    这是修复前会失败的关键用例：PrincipalContext 缺 departments 时，
    策略允许但 apply 永远不匹配，表现成"有权限却拿不到数据"。
    """
    context = decode_token(_token(departments=["tech"]), _settings())
    policy = AclPolicy.from_json({"allow": {"departments": ["tech"]}})

    decision, _ = evaluate(policy, context.to_subject(), resource_tenant_id=str(context.tenant_id))
    assert decision is Decision.ALLOW


def test_group_policy_denied_when_token_lacks_group() -> None:
    context = decode_token(_token(), _settings())
    policy = AclPolicy.from_json({"allow": {"groups": ["外贸组"]}})

    decision, _ = evaluate(policy, context.to_subject(), resource_tenant_id=str(context.tenant_id))
    assert decision is Decision.DENY


def test_invalid_scope_claim_type_rejected() -> None:
    with pytest.raises(ApiError):
        decode_token(_token(roles=123), _settings())
