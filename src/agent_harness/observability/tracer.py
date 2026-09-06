"""RunTracer——一次 agent run 的 Langfuse 生命周期映射（ADR-0018 D5/D6/D7）。

映射（不发明第二套 trace identity，spec 12 §4）：
- Langfuse trace 根 = 一次 agent run：name=``agent-run``，input=用户消息，
  metadata=run_id / session_id / agent_id / git_commit；
- generation = 每次 model call：model、input 消息、output 文本、usage
  （项目键 → Langfuse usage_details 键）、latency、fallback 履历；
- run 终态 → 根观测 update（output / level=ERROR）+ end。

内容边界（D6）：``full`` 原样；``redacted`` 截断——redaction 是本模块
``_redact`` 单一函数边界，未来接策略不改埋点。所有 span 操作零伪造
（无数据不带键）且绝不抛出：句柄操作失败经 ``sink.report_failure``
计数进熔断账本后吞掉。

热路径约束（D3 性能红线）：本模块全部是内存操作 + SDK 后台入队。
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_harness.observability.sink import LangfuseSink

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REDACTED_TEXT_LIMIT = 500

#: git_commit 惰性缓存：None=未取，False=取不到（不再重试），str=短哈希。
_git_commit_cache: str | bool | None = None


def git_commit() -> str | None:
    """仓库短哈希（regression metadata / trace metadata 用）；取不到如实 None。"""
    global _git_commit_cache
    if _git_commit_cache is None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=3, cwd=_REPO_ROOT,
                check=False,
            )
            _git_commit_cache = result.stdout.strip() or False
        except Exception:  # noqa: BLE001 - 观测元数据取不到不致命
            _git_commit_cache = False
    return str(_git_commit_cache) if _git_commit_cache else None


def _redact(value: Any) -> Any:
    """redacted 模式的内容收敛（D6 单一函数边界）：字符串截断，容器递归。"""
    if isinstance(value, str):
        if len(value) <= _REDACTED_TEXT_LIMIT:
            return value
        return value[:_REDACTED_TEXT_LIMIT] + "…"
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


class RunTracer:
    """一次 run 的 Langfuse 映射。sink 缺席（未配置）时全部方法安全 no-op。"""

    def __init__(
        self,
        sink: LangfuseSink,
        *,
        session_id: str,
        run_id: str,
        agent_id: str,
        user_input: str,
    ) -> None:
        self._sink = sink
        self._session_id = session_id
        self._run_id = run_id
        self._agent_id = agent_id
        self._user_input = user_input
        self.trace_id: str | None = None
        self._root: Any = None

    def _content(self, value: Any) -> Any:
        if getattr(self._sink, "trace_content", "full") == "redacted":
            return _redact(value)
        return value

    def _quiet(self, operation: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - D3 异常边界：观测故障不传染
            self._sink.report_failure(operation, exc)
            return None

    def run_started(self) -> None:
        root = self._sink.start_observation(
            name="agent-run",
            as_type="span",
            input=self._content(self._user_input),
            metadata={
                "session_id": self._session_id,
                "run_id": self._run_id,
                "agent_id": self._agent_id,
                "git_commit": git_commit(),
            },
        )
        if root is None:
            return
        self._root = root
        # 真实 trace id 回填 run/completed（D7）；SDK 未暴露时如实保持 None。
        self.trace_id = getattr(root, "trace_id", None)

    def model_call_started(
        self, *, step: int, messages: Any, model: str | None = None,
    ) -> Any:
        if self._root is None:
            return None
        return self._quiet(
            "model_call_started",
            lambda: self._root.start_observation(
                name="model-call",
                as_type="generation",
                input=self._content(messages),
                model=model,
                metadata={"step": step},
            ),
        )

    def model_call_completed(
        self,
        generation: Any,
        *,
        output_text: str,
        usage: dict[str, int] | None = None,
        duration_ms: int | None = None,
        finish_reason: str | None = None,
        provider_request_id: str | None = None,
        response_model: str | None = None,
        fallback_transitions: list[Any] | None = None,
    ) -> None:
        if generation is None:
            return
        metadata: dict[str, Any] = {}
        if duration_ms is not None:
            metadata["duration_ms"] = duration_ms
        if finish_reason:
            metadata["finish_reason"] = finish_reason
        if provider_request_id:
            metadata["provider_request_id"] = provider_request_id
        if response_model:
            metadata["response_model"] = response_model
        if fallback_transitions:
            first = fallback_transitions[0]
            metadata["fallback_from"] = first.from_model
            metadata["fallback_to"] = first.to_model
            metadata["fallback_reason"] = first.reason
        self._quiet("model_call_completed", lambda: generation.update(
            output=self._content(output_text),
            **({"usage_details": _usage_details(usage)} if usage else {}),
            **({"metadata": metadata} if metadata else {}),
        ))
        self._quiet("model_call_end", generation.end)

    def model_call_failed(self, generation: Any, *, error_type: str) -> None:
        if generation is None:
            return
        self._quiet("model_call_failed", lambda: generation.update(
            level="ERROR", status_message=f"model call failed: {error_type}",
        ))
        self._quiet("model_call_end", generation.end)

    def run_completed(self, final_text: str, usage_total: dict[str, int] | None = None) -> None:
        if self._root is None:
            return
        self._quiet("run_completed", lambda: self._root.update(
            output=self._content(final_text),
            **({"metadata": {"usage_total": usage_total}} if usage_total else {}),
        ))
        self._quiet("run_end", self._root.end)

    def run_failed(self, reason: str) -> None:
        if self._root is None:
            return
        self._quiet("run_failed", lambda: self._root.update(
            level="ERROR", status_message=reason,
        ))
        self._quiet("run_end", self._root.end)


def _usage_details(usage: dict[str, int]) -> dict[str, int]:
    """项目 usage 键（prompt/completion/total_tokens）→ Langfuse usage_details 键。"""
    details: dict[str, int] = {}
    if usage.get("prompt_tokens") is not None:
        details["input"] = usage["prompt_tokens"]
    if usage.get("completion_tokens") is not None:
        details["output"] = usage["completion_tokens"]
    if usage.get("total_tokens") is not None:
        details["total"] = usage["total_tokens"]
    return details
