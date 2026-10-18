"""#1322 后端半：Web 逻辑回放端点（`GET /api/sessions/{id}/replay`）。

契约（spec 03 §6 / ADR-0017 决策 4，与 CLI `replay_command` 同一份语义）：

| 条件 | 响应 |
| --- | --- |
| 正常会话 | 200 `{session_id, count, steps:[{seq,type,line}]}` |
| 无任何可渲染事件（只有生命周期噪音） | 200 `count=0, steps=[]` |
| 会话不存在 | 404 `session '<id>' not found` |
| id 形态非法 | 422（先于 404，路径穿越防线） |

三条口径要点：

1. **零副作用**（本文件的核心不变量）：回放只读 `events.jsonl`，**不写**任何事件
   （连 `session/resumed` 都不追加）、不构造 runtime（无模型访问）、不触碰 workspace。
   用"回放前后 `events.jsonl` 逐字节相同 + 模型工厂被调用即炸"双向锁死。
2. **不复制回放逻辑**（AC-A2）：端点的 `steps[].line` 必须与 CLI `replay_command`
   的输出行**逐字相同**——它是同一份 `render_replay_event` 投影。谁若在 web 层另写
   一份回放渲染，这条会红。
3. **冻结终态**：tool result 渲染为 `→ result (frozen):`，绝不重执行工具。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent_harness.cli import replay_command
from agent_harness.config import Settings
from agent_harness.session import Session
from agent_harness.session.event import (
    MODEL_COMPLETED,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
)
from agent_harness.session.store import JsonlSessionStore
from agent_harness.web.app import create_app


def _seed_session(tmp_path: Path, session_id: str = "hist") -> None:
    """往 app 会读的那棵树（`<workspace>/sessions`）里写一段可渲染的历史。"""
    store = JsonlSessionStore(root=tmp_path / "sessions")
    s = Session.start(store, session_id=session_id)
    s.append(USER_MESSAGE, {"content": "列出文件"})
    s.append(TOOL_CALL, {"tool_call_id": "c1", "tool_name": "bash",
                         "args": {"command": "ls"}})
    s.append(TOOL_RESULT, {"tool_call_id": "c1", "content": "a.txt\nb.txt"})
    s.append(MODEL_COMPLETED, {"content": "有两个文件"})


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(
        Settings(_env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test"),
        enable_cors=False,
    ))


def _events_path(tmp_path: Path, session_id: str) -> Path:
    return tmp_path / "sessions" / session_id / "events.jsonl"


# ── AC-A2/A3：端点返回逐事件重建（复用 CLI 投影，冻结终态）────────────────


def test_replay_returns_reconstructed_steps(tmp_path: Path) -> None:
    _seed_session(tmp_path)
    client = _client(tmp_path)

    resp = client.get("/api/sessions/hist/replay")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["session_id"] == "hist"
    assert body["count"] == len(body["steps"]) > 0
    lines = "\n".join(step["line"] for step in body["steps"])
    assert "[user]" in lines and "列出文件" in lines
    assert "[assistant]" in lines and "有两个文件" in lines
    assert "[tool]" in lines and "bash" in lines
    # 冻结终态：tool result 不重执行，渲染已发生的事实
    assert "→ result (frozen):" in lines and "a.txt" in lines
    # 每个 step 带 seq / type，供前端逐事件推进（生命周期噪音不进 steps）
    for step in body["steps"]:
        assert isinstance(step["seq"], int)
        assert isinstance(step["type"], str)
    assert all(step["type"] != "session/started" for step in body["steps"])


def test_replay_steps_match_the_cli_replay_line_for_line(tmp_path: Path) -> None:
    """AC-A2 的可证伪形式：Web steps 的行 = CLI `replay_command` 的输出行。

    两边若各写一份回放渲染，只要有一处措辞 / 取行方向漂开，这条就红——它证明
    "复用同一实现"不是一句声明。
    """
    import asyncio

    _seed_session(tmp_path)
    client = _client(tmp_path)

    web_lines = [step["line"] for step in client.get("/api/sessions/hist/replay").json()["steps"]]
    cli_output = asyncio.run(replay_command("hist", workspace_dir=str(tmp_path)))
    cli_lines = cli_output.split("\n")

    # CLI 把多行 step 用 "\n" join 成一整段；逐行比对会拆散多行 step（如 tool result
    # 的冻结预览），所以按"每个 step 的文本"对齐：Web 的 step 串起来必须等于 CLI 输出。
    assert "\n".join(web_lines) == cli_output
    assert cli_lines  # 非空，避免两个空串假相等


def test_replay_only_lifecycle_events_yields_empty_steps(tmp_path: Path) -> None:
    """只有生命周期噪音的会话：合法但无可渲染内容——`count=0`、`steps=[]`，不是 404。"""
    store = JsonlSessionStore(root=tmp_path / "sessions")
    Session.start(store, session_id="quiet")  # 只写 session/started
    client = _client(tmp_path)

    resp = client.get("/api/sessions/quiet/replay")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"session_id": "quiet", "count": 0, "steps": []}


# ── AC-A3：零副作用契约（回放前后事件流逐字节一致 + 无模型访问）──────────────


def test_replay_is_zero_side_effect_on_the_event_log(tmp_path: Path) -> None:
    """回放**不写入**原会话事件流：`events.jsonl` 逐字节相同，目录无新文件。

    连 `session/resumed` 都不许追加——这是"逻辑回放"与"恢复"的分界（spec 03 §6）。
    """
    _seed_session(tmp_path)
    path = _events_path(tmp_path, "hist")
    before_bytes = path.read_bytes()
    before_files = sorted(p.name for p in path.parent.iterdir())
    client = _client(tmp_path)

    resp = client.get("/api/sessions/hist/replay")
    assert resp.status_code == 200, resp.text

    assert path.read_bytes() == before_bytes, "回放不许改动原会话事件流"
    assert sorted(p.name for p in path.parent.iterdir()) == before_files


def test_replay_never_constructs_a_model(tmp_path: Path) -> None:
    """零 Provider 访问（spec 03 §6：replay MUST NOT 调用 Provider / Tool）。

    模型工厂若被调用即抛——回放路径上不该有任何模型构造点。
    """
    _seed_session(tmp_path)
    client = _client(tmp_path)

    with patch(
        "agent_harness.assembly.create_chat_model",
        side_effect=AssertionError("replay 不许构造模型"),
    ):
        resp = client.get("/api/sessions/hist/replay")
    assert resp.status_code == 200, resp.text


def test_replay_is_idempotent(tmp_path: Path) -> None:
    """只读、幂等：重复调用结果逐字相同。"""
    _seed_session(tmp_path)
    client = _client(tmp_path)
    first = client.get("/api/sessions/hist/replay").json()
    second = client.get("/api/sessions/hist/replay").json()
    assert first == second


# ── 错误矩阵：404 / 422 ───────────────────────────────────────────────


def test_replay_unknown_session_is_404(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.get("/api/sessions/no-such-session/replay")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "session 'no-such-session' not found"


def test_replay_invalid_session_id_is_422(tmp_path: Path) -> None:
    """形态非法 → 422，且**先于** 404——否则 `../x` 会被说成"不存在"。"""
    client = _client(tmp_path)
    resp = client.get("/api/sessions/bad.id/replay")
    assert resp.status_code == 422, resp.text


def test_replay_does_not_leak_into_the_event_vocabulary() -> None:
    """回放是投影，不是会话真相：SessionEvent 词汇表里不许冒出 replay 事件。"""
    from agent_harness.session.event import EVENT_TYPES

    assert [t for t in EVENT_TYPES if "replay" in t.lower()] == []
