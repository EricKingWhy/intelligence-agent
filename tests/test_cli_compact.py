"""#635 T4 CLI 面：`agent-harness compact --session <id> [--model M] [--dry-run]`。

CLI 是同一个 `SessionService.compact_session_context` 方法的**瘦触发器**
（ADR-0045 D8）：判定（能否压 / 在途 run / 模型解析）与落盘全在服务层；本层只做
参数解析 / 渲染 / 退出码。

**本文件钉的命令契约（Task B）**：

- `compact_command(...)` 可测核心返回渲染文本（#616 式），渲染来源是 DTO
  `SessionContextCompaction`——CLI 不重算任何压缩规则；
- 成功：`压缩完成` + 前后 token 对比 + source 区间；
- 低水位（`compacted_turn_count == 0` 且非 dry-run）：`水位过低，无需压缩（未改动）。`；
- `--dry-run`：零 LLM / 零写入，渲染"将压缩"预览；
- 错误映射：无会话 / id 非法 → exit 1；在途 run / 压缩进行中 → exit 1（明确文案）；
  非法 `--model` → exit 1；用法错 → exit 2。

为避免在 CLI 单测里重建整条运行时，本文件用替身服务注入 `_cli_session_service`：
渲染/退出码是 T4 的接缝，真实压缩语义的契约在 `tests/session/test_compact_session_context.py`。
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from agent_harness import cli
from agent_harness.config import Settings
from agent_harness.context.compactor import CompactionPostWriteError
from agent_harness.model.config import ConfigError
from agent_harness.session.errors import (
    ActiveRunConflict,
    CompactionConcurrentWrite,
    CompactionInProgress,
    InvalidSessionId,
    SessionNotFound,
)
from agent_harness.session.service import SessionContextCompaction


class _StubService:
    """替身 `SessionService`：记录调用参数，返回预置 DTO 或抛预置异常。"""

    def __init__(self, *, result=None, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._result = result
        self._error = error

    async def compact_session_context(
        self, session_id: str, *, model=None, entry_point, dry_run=False,
    ):
        self.calls.append(
            {
                "session_id": session_id,
                "model": model,
                "entry_point": entry_point,
                "dry_run": dry_run,
            }
        )
        if self._error is not None:
            raise self._error
        return self._result


def _install(
    monkeypatch, tmp_path, *, result=None, error: Exception | None = None,
) -> _StubService:
    settings = Settings(_env_file=None, workspace_dir=str(tmp_path / "workspace"))
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    stub = _StubService(result=result, error=error)

    async def _fake_service(_settings):
        return stub

    monkeypatch.setattr(cli, "_cli_session_service", _fake_service)
    return stub


def _run_dispatch(monkeypatch, argv: list[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["agent-harness", *argv])
    cli._main_dispatch()


# ── 成功渲染 ────────────────────────────────────────────────────────────


def test_success_renders_token_comparison_and_source(monkeypatch, tmp_path):
    stub = _install(
        monkeypatch, tmp_path,
        result=SessionContextCompaction(
            bracket_id="brk-1", source_seq_start=12, source_seq_end=88,
            tokens_before=182_400, tokens_after=41_200, compacted_turn_count=8,
            summary_model="main-model", dry_run=False,
        ),
    )

    out = asyncio.run(cli.compact_command(session="s1", model=None, dry_run=False))

    assert out == (
        "会话 s1 压缩完成（bracket=brk-1）：\n"
        "  tokens: 182,400 → 41,200（-77%）\n"
        "  source: seq 12..88 → 摘要（8 节，schema=eight_section）"
    )
    assert stub.calls == [
        {
            "session_id": "s1", "model": None,
            "entry_point": cli.COMPACT_ENTRY_CLI, "dry_run": False,
        }
    ]


def test_model_is_passed_through(monkeypatch, tmp_path):
    stub = _install(
        monkeypatch, tmp_path,
        result=SessionContextCompaction(
            bracket_id="b", source_seq_start=1, source_seq_end=2,
            tokens_before=1000, tokens_after=400, compacted_turn_count=2,
            summary_model="cheap-model", dry_run=False,
        ),
    )

    asyncio.run(cli.compact_command(session="s1", model="cheap-model", dry_run=False))

    assert stub.calls[0]["model"] == "cheap-model"


# ── 低水位（非 dry-run）────────────────────────────────────────────────


def test_below_floor_renders_unchanged(monkeypatch, tmp_path):
    _install(
        monkeypatch, tmp_path,
        result=SessionContextCompaction(
            bracket_id=None, source_seq_start=None, source_seq_end=None,
            tokens_before=500, tokens_after=500, compacted_turn_count=0,
            summary_model=None, dry_run=False,
        ),
    )

    out = asyncio.run(cli.compact_command(session="s1", model=None, dry_run=False))

    assert out == "会话 s1 水位过低，无需压缩（未改动）。"


# ── dry-run 预览 ────────────────────────────────────────────────────────


def test_dry_run_with_compactable_window(monkeypatch, tmp_path):
    stub = _install(
        monkeypatch, tmp_path,
        result=SessionContextCompaction(
            bracket_id=None, source_seq_start=None, source_seq_end=None,
            tokens_before=5_000, tokens_after=5_000, compacted_turn_count=1,
            summary_model=None, dry_run=True,
        ),
    )

    out = asyncio.run(cli.compact_command(session="s1", model=None, dry_run=True))

    assert "将压缩" in out
    assert "dry-run" in out
    assert "5,000" in out
    assert "有可压缩的早期轮" in out
    assert stub.calls[0]["dry_run"] is True


def test_dry_run_without_compactable_window(monkeypatch, tmp_path):
    _install(
        monkeypatch, tmp_path,
        result=SessionContextCompaction(
            bracket_id=None, source_seq_start=None, source_seq_end=None,
            tokens_before=300, tokens_after=300, compacted_turn_count=0,
            summary_model=None, dry_run=True,
        ),
    )

    out = asyncio.run(cli.compact_command(session="s1", model=None, dry_run=True))

    assert "无可压缩的早期轮" in out


# ── 退出码矩阵（经 _main_dispatch）──────────────────────────────────────


@pytest.mark.parametrize(
    "error, needle",
    [
        (SessionNotFound("session 'x' not found"), "压缩失败"),
        (InvalidSessionId("bad id"), "压缩失败"),
        (
            ActiveRunConflict("session 'x' has a run in flight; compact it after the run finishes"),
            "在途 run",
        ),
        (
            CompactionInProgress("session 'x' already has a compaction in progress"),
            "压缩已在进行中",
        ),
        (
            CompactionConcurrentWrite(
                "session 'x' changed during compaction; retry",
                reason="event_drift",
            ),
            "并发改动",
        ),
        (ConfigError("未知模型 'nope'"), "压缩失败"),
    ],
)
def test_error_matrix_exits_1_with_clear_message(
    monkeypatch, tmp_path, capsys, error, needle,
):
    _install(monkeypatch, tmp_path, error=error)
    with pytest.raises(SystemExit) as excinfo:
        _run_dispatch(monkeypatch, ["compact", "--session", "s1"])
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert needle in captured.err


@pytest.mark.parametrize(
    "reason, needle",
    [
        ("run_busy", "等待 run 结束"),
        ("event_drift", "并发改动"),
    ],
)
def test_concurrent_write_text_is_reason_specific_and_claims_no_zero_change(
    monkeypatch, tmp_path, capsys, reason, needle,
):
    """G1 (#635)：两种成因分别给可操作文案，且都不再声称"零改动"。

    `run_busy` 的真实成因是 run 在收尾窗口（此前已落失败记录时更非零改动），所以
    提示"等 run 结束"而非"重试"；`event_drift` 才是并发改动。两处都不许说"零改动"。
    """
    _install(
        monkeypatch, tmp_path,
        error=CompactionConcurrentWrite("session 'x' rejected", reason=reason),
    )
    with pytest.raises(SystemExit) as excinfo:
        _run_dispatch(monkeypatch, ["compact", "--session", "s1"])
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert needle in captured.err
    assert "零改动" not in captured.err


def test_post_write_error_exits_1_with_clear_message(monkeypatch, tmp_path, capsys):
    """bracket 已写入但复核未过：exit 1 + 明确文案（不谎报"未改动"）。"""
    _install(
        monkeypatch, tmp_path,
        error=CompactionPostWriteError(
            "Compaction bracket re-projection mismatch", bracket_id="brk-9",
        ),
    )
    with pytest.raises(SystemExit) as excinfo:
        _run_dispatch(monkeypatch, ["compact", "--session", "s1"])
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "已写入" in captured.err or "复核" in captured.err
    assert "brk-9" in captured.err


def test_usage_error_exits_2(monkeypatch, tmp_path, capsys):
    # 缺 --session：argparse 直接 SystemExit(2)，不进服务层。
    with pytest.raises(SystemExit) as excinfo:
        _run_dispatch(monkeypatch, ["compact"])
    assert excinfo.value.code == 2


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
