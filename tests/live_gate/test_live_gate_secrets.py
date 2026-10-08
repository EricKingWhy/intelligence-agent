"""`secrets.py` 的两层扫描（`#307` R6 / AC「自动扫描确认输出与证据不含凭证值」）。

正控与**反控成对**：只证明"能抓到"不够 —— 一个把 `sha` / `tree` / `events_sha256`（全是
32 位以上十六进制）也打成凭证的扫描器会让每次运行都 FAIL，那与"抓不到"一样没用
（`_KEY_SHAPED` 里刻意不收纯长 hex，本文件的反控就是钉住这条取舍）。
"""

from __future__ import annotations

import json

from pydantic import BaseModel, SecretStr

from evaluation.live_gate.secrets import (
    MASK,
    MIN_SCANNED_SECRET_LEN,
    credential_names,
    credential_values,
    mask_text,
    scan_payload,
)

#: 40 位十六进制（= 真实 sha 的形状）。**必须不被掩**。
HEX_40 = "3ed88e7b147f34522a3c4f30781f6a2d0d41dd68"


class _Stub(BaseModel):
    """鸭子类型替身：`credential_names` / `credential_values` 只认 `model_fields` + `SecretStr`。"""

    model_api_key: SecretStr = SecretStr("")
    fallback_model_api_key: SecretStr = SecretStr("")
    #: JSON 形状的 `SecretStr`（对齐 `Settings.agent_models` 的真实形状：条目里带 api_key）。
    agent_models_json: SecretStr = SecretStr("")
    plain_field: str = "not-a-secret"


def test_configured_value_is_masked_and_reported() -> None:
    secret = "a-real-looking-configured-value"
    masked, findings = mask_text(f"GET /v1 with {secret} end", values=[secret], where="t")
    assert secret not in masked
    assert MASK in masked
    assert [finding.rule for finding in findings] == ["configured_credential_value"]
    assert findings[0].where == "t"
    # 样本本身也不得带回原值（命中点右侧就是真值）
    assert secret not in findings[0].sample


def test_short_values_are_not_collected_as_credentials() -> None:
    """值 < 8 字符不参与精确值扫描：`"1"` 这类会让每份证据都假阳性。"""
    stub = _Stub(model_api_key=SecretStr("1"), fallback_model_api_key=SecretStr("   "))
    assert len("1") < MIN_SCANNED_SECRET_LEN
    assert credential_values(stub) == []
    # 键名口径更松：非空即"配过"（`"1"` 在名单里），空白值不算配过（`"   "` 不在名单里）
    assert credential_names(stub) == ["MODEL_API_KEY"]


def test_empty_credentials_yield_no_names_and_no_values() -> None:
    assert credential_names(_Stub()) == []
    assert credential_values(_Stub()) == []


def test_non_secretstr_fields_are_not_treated_as_credentials() -> None:
    """只认 `SecretStr`（类型判据）：普通 str 字段不进精确值扫描，哪怕它看着像 key。"""
    stub = _Stub(plain_field="sk-" + "x" * 24)
    assert credential_values(stub) == []


def test_shape_rules_mask_and_name_the_rule() -> None:
    cases = {
        "authorization_header": "Authorization: Bearer " + "Z" * 30,
        "secret_assignment": "api_key=0123456789abcdef0123",
        "url_userinfo": "https://user:hunter2@api.example.com/v1",
        "key_shaped_token": "token sk-" + "a" * 24 + " done",
    }
    for expected_rule, text in cases.items():
        masked, findings = mask_text(text, where="t")
        assert expected_rule in [finding.rule for finding in findings], text
        assert masked != text


def test_jwt_and_aws_shapes_are_masked() -> None:
    _, findings = mask_text("AKIAIOSFODNN7EXAMPLE", where="t")
    assert [finding.rule for finding in findings] == ["key_shaped_token"]
    jwt = (
        "eyJhbGciOiJIUzI1NiJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "abcdefgh"
    )
    masked, findings = mask_text(f"token {jwt}", where="t")
    assert "eyJhbGciOiJIUzI1NiJ9" not in masked
    assert [finding.rule for finding in findings] == ["key_shaped_token"]


def test_forty_hex_digits_are_not_mistaken_for_a_credential() -> None:
    """反控：sha / tree / events_sha256 的形状必须原样保留（否则每次运行都 FAIL）。"""
    masked, findings = mask_text(f'sha={HEX_40} tree={HEX_40}', where="t")
    assert findings == []
    assert masked.count(HEX_40) == 2


def test_ordinary_parameters_and_short_assignments_are_untouched() -> None:
    """反控：`max_tokens=20` 与短赋值的 "key" 字段都不是凭证（假阳性会让 3/3 失去意义）。"""
    for text in ("run with max_tokens=20 steps=2", "api_key=short", "token: 12", ""):
        masked, findings = mask_text(text, where="t")
        assert findings == [], text
        assert masked == text


def test_scan_payload_masks_values_in_keys_and_nested_items() -> None:
    secret = "configured-secret-value-0001"
    payload = {
        f"k-{secret}": [{"detail": f"echo {secret}"}, "plain"],
        "tree": HEX_40,
        "nested": {"deep": {"list": [1, 2, {"note": "no secrets here"}]}},
    }
    masked, findings = scan_payload(payload, values=[secret], where="evidence")
    text = str(masked)
    assert secret not in text
    assert findings, "键与值各命中一次"
    # 反控：十六进制与普通结构原样保留、入参不被就地改写
    assert masked["tree"] == HEX_40
    assert payload["tree"] == HEX_40
    assert f"k-{secret}" in payload


def test_all_github_token_prefixes_are_masked() -> None:
    """`ghp_` 不是唯一的 GitHub 前缀（`gho_` / `ghu_` / `ghs_` / `ghr_` 同族）。"""
    for prefix in ("ghp", "gho", "ghu", "ghs", "ghr"):
        masked, findings = mask_text(f"token {prefix}_" + "a" * 24, where="t")
        assert [finding.rule for finding in findings] == ["key_shaped_token"], prefix
        assert masked == "token ***"


def test_url_userinfo_without_a_colon_is_masked() -> None:
    """只有 token、没有冒号的 userinfo 是最常见的一种漏法（`https://<token>@host`）。"""
    masked, findings = mask_text("GET https://secret-token-value@api.example.com/v1", where="t")
    assert [finding.rule for finding in findings] == ["url_userinfo"]
    assert "secret-token-value" not in masked


def test_json_shaped_secret_yields_its_entry_level_values() -> None:
    """`agent_models` 这类 `SecretStr` 装的是 **JSON**：真凭证在条目里（`api_key`）。"""
    payload = json.dumps([
        {
            "name": "a", "provider": "deepseek",
            "base_url": "https://api.example.com/v1", "api_key": "entry-level-key-0001",
        },
    ])
    values = credential_values(_Stub(agent_models_json=SecretStr(payload)))
    assert "entry-level-key-0001" in values
    # 反控：非凭证字段不进精确值层（把 base_url 当凭证会让正常文本被掩掉并误报）
    assert "https://api.example.com/v1" not in values
    masked, _ = mask_text("leak entry-level-key-0001", values=values, where="t")
    assert "entry-level-key-0001" not in masked


def test_non_json_secret_is_still_collected_as_a_whole() -> None:
    """JSON 解析失败**不抛**：那只说明"这个字段不是 JSON"，整串照旧进精确值层。"""
    values = credential_values(_Stub(model_api_key=SecretStr("plain-secret-value-0002")))
    assert values == ["plain-secret-value-0002"]


def test_protected_fact_ids_are_not_mistaken_for_an_authorization_header() -> None:
    """反控（AC16 Round 6 实测踩坑）：`authorization:<event-id>` 是**事实 ID**，不是 HTTP 头。

    证据里每条 session 都投影一条 `authorization` 保护事实，`fact_id` 逐字是
    `authorization:<uuid4>`。`_AUTH_HEADER` 的 `\\b` 在 `n` 与 `:` 之间成立，于是这条
    普通事实 ID 被当成 `Authorization: …` 命中，把整份 AC16 证据的 `status` 翻成
    failed（18 处命中）。

    判据：真 HTTP 头（键名 `Authorization` 整体，后跟冒号 + 值）仍须命中；事实 ID 不命中。
    """
    fact_id = "authorization:2dd4df30-bbe3-432f-a231-1d7de3663ffb"
    masked, findings = mask_text(fact_id, where="t")
    assert findings == []
    assert masked == fact_id

    # 正控：真头照旧命中（含 `Bearer ` 与裸值两种写法）。
    for header in (
        "Authorization: Bearer " + "Z" * 30,
        "authorization: " + "Z" * 30,
        "authorization=" + "Z" * 30,
    ):
        _, hits = mask_text(header, where="t")
        assert [hit.rule for hit in hits] == ["authorization_header"], header


def test_bearer_with_a_uuid_value_never_leaks_past_the_mask() -> None:
    """反控（Round 6 Standards 轴实测）：`Bearer <uuid>` 不能被**部分**掩成泄漏。

    第一版把 `(?!uuid)` 放在可选的 `(?:bearer\\s+)?` **之后** ⇒ 回溯让 `[^\\s]+` 只吃到
    `Bearer`，输出 `Authorization: *** 2dd4df30-…`：`Bearer` 被掩、**凭证本身明文留着**。
    这比"不掩"更糟 —— 它看起来脱敏了（`***` 在场），却把值整条泄漏。

    两种形态的行为**刻意不同**，本用例把它们钉开：
    - `Bearer <uuid>` 是**真 HTTP 头**形态 ⇒ 整条掩成 `***`，值绝不出现在输出里；
    - `authorization:<uuid>`（无空格）是生产的**事实 ID** 形态 ⇒ 整条不命中（见上一个用例）。
    """
    token = "2dd4df30-bbe3-432f-a231-1d7de3663ffb"
    masked, findings = mask_text(f"Authorization: Bearer {token}", where="t")
    assert token not in masked, f"UUID 明文泄漏：{masked!r}"
    assert masked == "Authorization: ***"
    assert [f.rule for f in findings] == ["authorization_header"]
    # 反控的另一半：裸事实 ID 形态**不命中**（这是本轮修假阳性的目的本身）。
    bare = f"authorization:{token}"
    masked_bare, findings_bare = mask_text(bare, where="t")
    assert findings_bare == []
    assert masked_bare == bare
