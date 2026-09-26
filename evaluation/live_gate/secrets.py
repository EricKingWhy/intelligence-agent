"""Live Gate 的凭证扫描与脱敏（`#307` R6 / AC「自动扫描确认输出与证据不含凭证值」）。

## 与既有两份脱敏实现的**分工**（不是重复，也不是可以合并的三份）

| 实现 | 用途 | 对文本做的事 |
| --- | --- | --- |
| `agent_harness.transport.contract.redact_command_summary` | 命令**审计摘要** | 连路径一起 `<scoped>` 掉、截断 500 字符 |
| `tests.live_model_guard._redact_base_url` | 把端点探测的**理由串**放进 skip 消息 | 掩掉 base_url 的 userinfo / query 与 key 形状 |
| **本模块** | 断言"证据里**没有**凭证"，并把命中处掩掉 | 只掩凭证，**其余原样保留**（工具轨迹要可读） |

前两份都不能直接用：第一份会把工具轨迹里的路径抹成 `<scoped>`（证据就不可核对了），
第二份是私有的、且只面向 base_url 一种形状。所以判据在这里**另立一份**，并由
`tests/live_gate/test_secrets.py` 用**植入的假凭证**做正控（含"没有凭证时必须不报"的反控）。

## 两层扫描，缺一不可

1. **精确值扫描**（最强）：把进程内 `Settings` 里**真实的** `SecretStr` 值（只在内存里走一圈，
   绝不打印、绝不落盘）拿去在文本里查。这是唯一能抓住"形状不像 key 的自定义凭证"的一层。
   - 值 < 8 字符不参与（`"   "` / `"1"` 这类会满地假阳性，反把真命中淹掉）；
   - **JSON 形状的 `SecretStr` 再往里走一层**（`_nested_secret_values`）：`agent_models`
     装的是 `[{"api_key": …}]`，真凭证在**条目里**，整串 JSON 当"一个值"去比几乎永远
     匹配不上（它在进程里从没以那种拼法出现过）；
   - 环境里没有配置任何凭证时（干净克隆里的独立复核）**如实标注 `unavailable`**，
     不假装扫过 —— `SecretScanRecord.exact_value_scan` 就是这个字段。
2. **形状扫描**（兜底）：抓 `sk-` / `AKIA…` / JWT / 长 hex、`Authorization: Bearer …`、
   `api_key=…` 赋值、URL userinfo（`https://user:pass@…`，以及**只带 token 的**
   `https://<token>@…`）这类**未配置凭证**的回显面。
   形状词表与 `tests/live_model_guard.py` 同源（那处是"掩掉再打印"，本处是"断言不存在"）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

#: 形状词表。刻意保守（宁可漏报也不把普通文本打成"疑似凭证"）：都以"几乎不可能是自然文本"
#: 为界 —— 固定前缀 + 足够长的随机段，或 JWT 的 `eyJ` 头 + 两段点分。
#:
#: ⚠ **刻意不收"纯长 hex"**（`\b[0-9a-fA-F]{32,}\b`）：本证据里 `sha` / `tree` /
#: `events_sha256` 全是 32 位以上的十六进制，收进来会让**每一次运行**都命中 ⇒ 闸门恒 FAIL
#: （实测形状：一次构建后立刻自报"证据含凭证"）。hex 形态的真凭据由**精确值扫描**兜住
#: ——那是更强的一层（只认真配置过的值，零误报）。
_KEY_SHAPED = (
    re.compile(r"\b(?:sk|pk|rk|hf|gh[pousr]|glpat|xox[baprs])[-_][A-Za-z0-9_\-]{12,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}"),
    re.compile(r"\b[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\b"),  # id.secret 形态
)
_AUTH_HEADER = re.compile(r"(?i)\b(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;'\"]+")
#: "键名像凭证"的词表：赋值形态（`_SECRET_ASSIGNMENT`）与 JSON 条目扫描（`_SECRET_KEY_NAME`）
#: 共用一份，避免两处对"什么算凭证字段"给出不同答案。
_SECRET_KEY_VOCAB = r"api[_-]?key|apikey|secret|token|password|passwd|credential"
#: 赋值形态**要求值本身像凭证**（≥16 字符）：否则 `max_tokens=20` 一类的普通参数会在模型
#: 文本里命中，把正常运行打成"含凭证"（假阳性会让 3/3 判据失去意义）。
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Za-z0-9_]*(?:" + _SECRET_KEY_VOCAB + r")"
    r"[A-Za-z0-9_]*\s*[:=]\s*)([\"']?)([^\s,;\"']{16,})\2"
)
#: JSON 条目里的键名判据（`{"api_key": "…"}`）。
_SECRET_KEY_NAME = re.compile(rf"(?i)^[A-Za-z0-9_]*(?:{_SECRET_KEY_VOCAB})[A-Za-z0-9_]*$")
#: `://user:pass@host` **与** `://token@host` 两种都算（后者是最常见的一种漏法：
#: 只有 token、没有冒号）。冒号段可选。
_URL_USERINFO = re.compile(r"(?<=://)[^\s/@:]+(?::[^\s/@]+)?@")

#: 掩码常量：与 `live_model_guard` 的 `***` 同形，便于人眼一致。
MASK = "***"

#: 形状规则三件套：`(模式, 规则名, 替换串)`。替换串**保留键名 / 头名**（`api_key=***` 比
#: 光秃秃的 `***` 可核对得多），只掩值 —— 这与"掩码后仍可读"的目标一致。
_SHAPE_RULES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (_AUTH_HEADER, "authorization_header", r"\1***"),
    (_SECRET_ASSIGNMENT, "secret_assignment", r"\1***"),
    (_URL_USERINFO, "url_userinfo", "***@"),
) + tuple((pattern, "key_shaped_token", MASK) for pattern in _KEY_SHAPED)

#: 精确值扫描的最小长度（见模块 docstring）。
MIN_SCANNED_SECRET_LEN = 8


@dataclass(frozen=True)
class SecretFinding:
    """一处命中。`sample` 是**掩码后**的片段 —— 本类不得携带原值。"""

    rule: str
    where: str
    sample: str = ""

    def line(self) -> str:
        return f"[{self.rule}] {self.where}" + (f" :: {self.sample}" if self.sample else "")


def _nested_secret_values(text: str) -> list[str]:
    """从 JSON 形状的 `SecretStr` 里取出**条目级令牌**（见模块 docstring 第 1 条）。

    只收**键名像凭证**的字符串标量：键名判据是本模块的 `_SECRET_ASSIGNMENT` 同款词表
    （`api_key` / `token` / `secret` / …）。把 `base_url` 一类的值也收进来会让正常文本
    被掩掉并报"含凭证"，而假阳性会让这一层彻底失去信号（每次运行都红 ⇒ 没人再看它）。

    解析失败**不抛**：那只说明"这个字段不是 JSON"，不该让凭证扫描整条崩掉。
    """
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return []
    found: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and _SECRET_KEY_NAME.match(key) and isinstance(value, str):
                    found.append(value)
                    continue
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(payload)
    return found


def credential_values(settings: Any) -> list[str]:
    """进程内取 `Settings` 上所有 `SecretStr` 的**值**（仅内存，调用方不得打印/落盘）。

    只认 `SecretStr`（pydantic 的显式密钥类型，`config.py` 用了它）= 走类型判据而不是
    字段名猜谜。`GETTER` 缺失（鸭子类型替身）或取值为空/空白 ⇒ 跳过；JSON 形状的值
    再往条目里走一层（`_nested_secret_values`）。
    """
    from pydantic import SecretStr

    values: list[str] = []
    for name in type(settings).model_fields:
        raw = getattr(settings, name, None)
        if not isinstance(raw, SecretStr):
            continue
        value = raw.get_secret_value()
        for candidate in (value, *_nested_secret_values(value)):
            if (
                candidate and candidate.strip()
                and len(candidate) >= MIN_SCANNED_SECRET_LEN
                and candidate not in values
            ):
                values.append(candidate)
    return values


def credential_names(settings: Any) -> list[str]:
    """在场的**键名**清单（环境变量名口径 = 字段名大写，与 `tests/conftest.py` 同一约定）。

    `#307` Must Do 要求"只允许输出 key 名/能力状态" —— 这个函数就是那个"名"的出口：
    值为空/空白的字段**不列**（列了会让人以为配过）。
    """
    from pydantic import SecretStr

    names: list[str] = []
    for name in type(settings).model_fields:
        raw = getattr(settings, name, None)
        if not isinstance(raw, SecretStr):
            continue
        value = raw.get_secret_value()
        if value and value.strip():
            names.append(name.upper())
    return sorted(names)


def mask_text(text: str, *, values: Iterable[str] = (), where: str = "") -> tuple[str, list[SecretFinding]]:
    """把文本里的凭证掩掉，返回 `(掩码后的文本, 命中清单)`。

    顺序**不可换**：先按**精确值**掩（那是真凭证，最长匹配、最可靠），再走形状层 ——
    反过来的话，形状层会把真凭证截成 `***` 的碎片（`sk-…***…`），既漏掉"这是配置的凭证"
    这条归因，也可能让精确值的子串匹配失败。
    """
    if not isinstance(text, str) or not text:
        return text, []
    findings: list[SecretFinding] = []
    masked = text
    for value in values:
        if value and value in masked:
            findings.append(SecretFinding("configured_credential_value", where, f"{MASK}（{len(value)} 字符）"))
            masked = masked.replace(value, MASK)
    for pattern, rule, replacement in _SHAPE_RULES:
        match = pattern.search(masked)
        if match is None:
            continue
        # 样本自己也要过一遍替换：命中点右边那段是真值，直接切片就会把凭证塞进 finding。
        sample = pattern.sub(replacement, masked[max(0, match.start() - 8):match.end()])
        findings.append(SecretFinding(rule, where, sample[:70]))
        masked = pattern.sub(replacement, masked)
    return masked, findings


def scan_payload(payload: Any, *, values: Iterable[str] = (), where: str = "evidence") -> tuple[Any, list[SecretFinding]]:
    """递归遍历证据结构，掩掉所有字符串字段里的凭证并返回命中清单（**就地重建**，不改入参）。

    dict 的**键**同样过一遍：真实事故里出现过"键名带着一次命令回显"的形状（台账反引号
    被命令替换吞掉，见 `scripts/check_review_coverage.py::lint_description`），键不值得信任。
    """
    values = tuple(values)
    findings: list[SecretFinding] = []
    if isinstance(payload, str):
        masked, hits = mask_text(payload, values=values, where=where)
        findings.extend(hits)
        return masked, findings
    if isinstance(payload, dict):
        out: dict[Any, Any] = {}
        for key, item in payload.items():
            masked_key, key_hits = (
                mask_text(key, values=values, where=f"{where}.<key>")
                if isinstance(key, str) else (key, [])
            )
            findings.extend(key_hits)
            masked_item, item_hits = scan_payload(item, values=values, where=f"{where}.{masked_key}")
            findings.extend(item_hits)
            out[masked_key] = masked_item
        return out, findings
    if isinstance(payload, list):
        out_list = []
        for index, item in enumerate(payload):
            masked_item, item_hits = scan_payload(item, values=values, where=f"{where}[{index}]")
            findings.extend(item_hits)
            out_list.append(masked_item)
        return out_list, findings
    return payload, findings
