"""`secrets.py` 的两层扫描（`#307` R6 / AC「自动扫描确认输出与证据不含凭证值」）。

正控与**反控成对**：只证明"能抓到"不够 —— 一个把 `sha` / `tree` / `events_sha256`（全是
32 位以上十六进制）也打成凭证的扫描器会让每次运行都 FAIL，那与"抓不到"一样没用
（`_KEY_SHAPED` 里刻意不收纯长 hex，本文件的反控就是钉住这条取舍）。
"""

from __future__ import annotations

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
