"""#1438 F-13a：审批面的端到端验收（CLI `agent-harness memory pending|approve|reject`）。

走 `_memory_command`（`_main_memory` 去掉 argparse/打印的部分），装配用生产同一个
`build_memory_v2_service`——CLI 与 web 是同一条领域路径，判据也应是同一组：
列表形状、批准落盘、拒绝丢弃、pin 失效拒绝且保留、不存在的 id 拒绝。
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from agent_harness.cli import _memory_command
from agent_harness.config import Settings
from agent_harness.memory.v2.assembly import build_memory_v2_service
from agent_harness.memory.v2.commands import trusted_identity_for_session


def _settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test")


def _args(command: str, staged_id: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(command=command, staged_id=staged_id)


async def _stage(settings: Settings, payload: dict) -> str:
    service = await build_memory_v2_service(settings)
    assert service.staged is not None
    pins, error = await service.staged.resolve_pins(payload)
    assert error is None, error
    staged = await service.staged.stage(
        payload, pins, trusted_identity_for_session("", None))
    return staged.id


def _memory_file(settings: Settings) -> Path:
    return Path(settings.workspace_dir) / "memories" / "MEMORY.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


@pytest.mark.asyncio
async def test_pending_renders_id_target_action_and_body(tmp_path):
    settings = _settings(tmp_path)
    staged_id = await _stage(settings, {
        "target": "memory", "action": "add", "content": "用户喜欢冰美式", "old_text": None})

    output, failed = await _memory_command(settings, _args("pending"))

    assert failed is False
    assert f"id={staged_id}" in output
    assert "target=memory" in output and "action=add" in output
    assert "用户喜欢冰美式" in output


@pytest.mark.asyncio
async def test_pending_is_empty_when_nothing_is_staged(tmp_path):
    output, failed = await _memory_command(_settings(tmp_path), _args("pending"))
    assert (output, failed) == ("（暂无待审写入）", False)


@pytest.mark.asyncio
async def test_approve_writes_through_to_markdown(tmp_path):
    settings = _settings(tmp_path)
    staged_id = await _stage(settings, {
        "target": "memory", "action": "add", "content": "用户喜欢冰美式", "old_text": None})

    output, failed = await _memory_command(settings, _args("approve", staged_id))

    assert failed is False and output.startswith(f"已批准 {staged_id}")
    assert "用户喜欢冰美式" in _read(_memory_file(settings))


@pytest.mark.asyncio
async def test_reject_discards_without_writing(tmp_path):
    settings = _settings(tmp_path)
    staged_id = await _stage(settings, {
        "target": "memory", "action": "add", "content": "用户喜欢冰美式", "old_text": None})

    output, failed = await _memory_command(settings, _args("reject", staged_id))

    assert (output, failed) == ("Memory write rejected.", False)
    assert "用户喜欢冰美式" not in _read(_memory_file(settings))
    assert (await _memory_command(settings, _args("pending")))[0] == "（暂无待审写入）"


@pytest.mark.asyncio
async def test_approve_after_external_edit_is_refused_and_keeps_the_record(tmp_path):
    settings = _settings(tmp_path)
    memory_file = _memory_file(settings)
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text("用户喜欢手冲咖啡", encoding="utf-8")
    staged_id = await _stage(settings, {
        "target": "memory", "action": "replace",
        "content": "用户喜欢冰美式", "old_text": "手冲咖啡"})

    memory_file.write_text("用户喜欢冷萃咖啡", encoding="utf-8")
    output, failed = await _memory_command(settings, _args("approve", staged_id))

    assert failed is False
    assert output.startswith("未落盘：")
    assert memory_file.read_text(encoding="utf-8") == "用户喜欢冷萃咖啡"
    assert f"id={staged_id}" in (await _memory_command(settings, _args("pending")))[0]


@pytest.mark.asyncio
async def test_approve_and_reject_unknown_ids_are_refused(tmp_path):
    settings = _settings(tmp_path)

    approve_output, _ = await _memory_command(settings, _args("approve", "stg_never_existed"))
    reject_output, _ = await _memory_command(settings, _args("reject", "stg_never_existed"))

    assert approve_output == "未落盘：No such pending memory write."
    assert reject_output == "No such pending memory write."
