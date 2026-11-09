"""基座化配置：实例域/目录规则 + 外部客服承诺词表。"""

from __future__ import annotations

import argparse

import pytest

from fkh.answer import build_commitment_keywords
from fkh.answer.service import _is_commitment_request
from fkh.tools import instance


def test_commitment_keywords_append_without_removing_builtins() -> None:
    builtin = build_commitment_keywords("")
    assert "单价" in builtin and "warranty" in builtin

    extended = build_commitment_keywords("巡检周期, 保养周期, inspection interval")
    assert "单价" in extended
    assert "巡检周期" in extended
    assert "保养周期" in extended
    assert "inspection interval" in extended
    assert _is_commitment_request("设备巡检周期是多久？", extended)
    assert not _is_commitment_request("设备巡检周期是多久？", builtin)


def test_provision_domains_replace_defaults() -> None:
    domains = instance._provision_domains(  # noqa: SLF001
        ["product_catalog=产品目录", "maintenance=保养手册"]
    )
    assert domains == (("product_catalog", "产品目录"), ("maintenance", "保养手册"))
    assert instance._provision_domains(None) is instance.DEFAULT_DOMAINS  # noqa: SLF001


@pytest.mark.parametrize("spec", ["missing", "=label", "code=", "Bad=Label", "a=b"])
def test_provision_domains_reject_invalid_specs(spec: str) -> None:
    if spec == "a=b":
        # 合法但重复的场景单独覆盖（code 至少 2 字符，与 DomainService 一致）
        with pytest.raises(SystemExit, match="重复"):
            instance._provision_domains(["aa=b", "aa=c"])  # noqa: SLF001
        return
    with pytest.raises(SystemExit):
        instance._provision_domains([spec])  # noqa: SLF001


def test_provision_currencies_uppercase_and_validate() -> None:
    assert instance._provision_currencies("usd, eur ,cny") == ["USD", "EUR", "CNY"]  # noqa: SLF001
    for bad in ("", "US", "USDD", "US1"):
        with pytest.raises(SystemExit):
            instance._provision_currencies(bad)  # noqa: SLF001


def test_provision_parser_accepts_customer_config() -> None:
    args = instance.build_parser().parse_args([
        "provision", "--workdir", "x", "--db-port", "55501", "--api-port", "8151",
        "--db-name", "fkh_customer", "--domain", "maintenance=保养",
        "--catalog-pattern", r"^SKU-[0-9]{4}$", "--currencies", "USD,EUR",
    ])
    assert isinstance(args, argparse.Namespace)
    assert args.domain == ["maintenance=保养"]
    assert args.catalog_pattern == r"^SKU-[0-9]{4}$"
    assert args.currencies == "USD,EUR"
