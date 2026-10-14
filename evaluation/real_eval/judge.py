"""real_eval Judge（issue #1005）：J 类断言的 3 票多数判定。

形状来源：confident-ai/deepeval（Apache-2.0）`deepeval/metrics/g_eval/g_eval.py`
的 GEval——criteria + 结构化 `{score, reason}` 入库；本票 ADAPT 成更简单的
**3 票多数**：同一 criteria 调 judge 模型 3 次（temperature 0），每次要求返回
结构化 `{"verdict": true|false, "reason": "..."}`；抽不出 JSON 的一票作废记 error，
多数票定 ok，`flip` = 3 票是否非一致（含废票）。

Judge 模型：kunyou deepseek-v4.1-flash，temperature 0，key 只从环境变量读。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

#: judge_fn 契约：喂一段提示，返回模型原始文本（供 JSON 抽取）。
JudgeFn = Callable[[str], str]

DEFAULT_JUDGE_MODEL = "deepseek-v4.1-flash"
DEFAULT_JUDGE_BASE_URL = "https://api.kunyou.asia/v1"
DEFAULT_JUDGE_API_KEY_ENV = "KUNYOU_API_KEY"

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_OBJECT = re.compile(r"\{.*\}", re.DOTALL)

_TRUE_WORDS = frozenset({"true", "yes", "1", "pass", "correct", "是", "正确", "通过"})
_FALSE_WORDS = frozenset({"false", "no", "0", "fail", "incorrect", "否", "错误", "不通过"})


class JudgeResult(BaseModel):
    """一次多数判定的结果（入 JSONL 记录）。"""

    ok: bool
    votes: list[dict[str, Any]] = Field(default_factory=list)
    valid_votes: int = 0
    majority: bool = False
    flip: bool = False
    error: str | None = None


def build_judge_prompt(task: str, criteria: str, actual: str) -> str:
    """构造判分提示（要求严格返回 JSON）。"""
    return (
        "你是评测判分器。请判断模型对下面任务的回答是否满足判分标准。\n"
        f"任务：{task}\n"
        f"判分标准：{criteria}\n"
        f"模型回答：{actual}\n\n"
        '只返回一个 JSON 对象，形如 {"verdict": true, "reason": "..."}，'
        "verdict 为布尔值（true 表示满足标准），reason 为一句话理由。不要输出其它内容。"
    )


def _json_candidates(text: str) -> list[str]:
    candidates = [text.strip()]
    candidates.extend(block.strip() for block in _FENCE.findall(text))
    candidates.extend(match.group(0) for match in _OBJECT.finditer(text))
    return [candidate for candidate in candidates if candidate]


def extract_verdict(text: str | None) -> tuple[bool | None, str]:
    """从模型输出里抽取 `(verdict, reason)`；抽不出返回 `(None, "")`。"""
    if not text:
        return (None, "")
    for candidate in _json_candidates(text):
        try:
            payload = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or "verdict" not in payload:
            continue
        reason = str(payload.get("reason", ""))
        verdict = payload["verdict"]
        if isinstance(verdict, bool):
            return (verdict, reason)
        if isinstance(verdict, str):
            low = verdict.strip().lower()
            if low in _TRUE_WORDS:
                return (True, reason)
            if low in _FALSE_WORDS:
                return (False, reason)
    return (None, "")


def majority_judge(
    *,
    task: str,
    criteria: str,
    actual: str,
    judge_fn: JudgeFn,
    votes: int = 3,
) -> JudgeResult:
    """调 judge_fn `votes` 次，多数票定 ok；记录逐票 reason 与 flip。"""
    prompt = build_judge_prompt(task, criteria, actual)
    vote_records: list[dict[str, Any]] = []
    verdicts: list[bool] = []
    for _ in range(votes):
        error: str | None = None
        raw: str | None = None
        try:
            raw = judge_fn(prompt)
        except Exception as exc:  # noqa: BLE001 - 单票失败不炸整条判定
            error = f"{type(exc).__name__}: {exc}"
        verdict, reason = extract_verdict(raw) if raw is not None else (None, "")
        if verdict is None and error is None:
            error = "unparseable verdict"
        vote_records.append(
            {"verdict": verdict, "reason": reason, "error": error}
        )
        if verdict is not None:
            verdicts.append(verdict)
    true_count = sum(1 for value in verdicts if value)
    majority = bool(verdicts) and true_count > len(verdicts) / 2
    flip = len(set(verdicts)) > 1 or len(verdicts) < votes
    error = None if verdicts else "judge produced no parseable verdict"
    return JudgeResult(
        ok=majority,
        votes=vote_records,
        valid_votes=len(verdicts),
        majority=majority,
        flip=flip,
        error=error,
    )


def build_judge_fn(
    *,
    model: str = DEFAULT_JUDGE_MODEL,
    base_url: str = DEFAULT_JUDGE_BASE_URL,
    api_key_env: str = DEFAULT_JUDGE_API_KEY_ENV,
) -> JudgeFn:
    """真实 judge_fn：调 kunyou deepseek（temperature 0）。key 只从环境变量读。

    仅在真实基线运行时调用；单元测试用 fake judge_fn 注入（不触网）。
    """
    from agent_harness.model.config import ModelConfig
    from agent_harness.model.provider import create_chat_model

    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise RuntimeError(f"judge 需要环境变量 {api_key_env}（未设置）")
    config = ModelConfig(
        provider="kunyoun",
        model_name=model,
        api_key=api_key,
        base_url=base_url,
        temperature=0.0,
    )
    chat = create_chat_model(config, request_timeout=300.0)

    def judge_fn(prompt: str) -> str:
        message = chat.invoke(prompt)
        content = message.content
        return content if isinstance(content, str) else str(content)

    return judge_fn
