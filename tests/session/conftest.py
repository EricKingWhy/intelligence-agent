"""SessionService 测试专用 fixtures（T1 / #131）。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_harness.config import Settings
from agent_harness.session import Session
from agent_harness.session.service import SessionService
from agent_harness.web.app import AppState


@pytest.fixture
def app_state(tmp_path: Path) -> AppState:
    """构造一个隔离的 AppState（用 tmp_path 做 workspace_dir）。

    不装配 Capability 子系统（get_wiring 惰性触发时返回空 wiring 或由
    调用方注入 fake）；RunManager 用默认 grace period。
    """
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        model_api_key="sk-test",
        enable_cors=False,
    )
    return AppState(settings)


@pytest.fixture
def make_session_service(tmp_path: Path):
    """按**显式 collaborators** 造 `SessionService`（#248 AC4）。

    需要 AppState / FastAPI 的地方用 `session_service(state)`；这条路径专门服务
    「本用例只关心其中两三个 collaborator」的场景——不碰容器，也就不再依赖它的
    魔法属性。默认值全是替身，用例按需覆盖。

    两个**形状**上的默认值按真实契约给（不是裸 MagicMock）：
    `get_wiring` 必须是"返回二元组"的 awaitable（调用点解包 `_, wiring = ...`），
    `run_manager` 的 `get_active` 默认答"没有在途 run"。
    其余 MagicMock 仍是"什么答什么"——**真值语义要当心**：`MagicMock().read_events(...)`
    为真会让 `has_session` 恒真、`is_busy` 恒真会把删除判成 409。要断言这些路径的用例
    必须显式覆盖对应 collaborator。
    """
    defaults = {
        "store": MagicMock(),
        "run_manager": MagicMock(get_active=MagicMock(return_value=None)),
        "settings": Settings(_env_file=None, workspace_dir=str(tmp_path)),
        "workspace_registry": MagicMock(),
        "session_meta_store": MagicMock(),
        "message_queues": MagicMock(),
        "approval_queues": {},
        "workspaces_root": tmp_path / "workspaces",
        "workspace_index": None,
        "operation_ledger": MagicMock(),
        "transport_ledger": MagicMock(),
        "checkpoint_store": MagicMock(),
        "harness_db": tmp_path / "harness.db",
        "stores": MagicMock(),
        "ensure_stores": AsyncMock(),
        "get_wiring": AsyncMock(return_value=(MagicMock(), MagicMock())),
    }

    def build(**overrides) -> SessionService:
        unknown = set(overrides) - set(defaults)
        assert not unknown, f"不是 SessionService 的 collaborator: {sorted(unknown)}"
        return SessionService(**{**defaults, **overrides})

    return build


@pytest.fixture
def existing_session(app_state: AppState) -> str:
    """在 app_state.store 里预创建一个 session，返回其 session_id。

    用于需要「session 存在但没有在途 run」的测试场景（审批校验等）。
    """
    session = Session.start(app_state.store)
    return session.session_id
