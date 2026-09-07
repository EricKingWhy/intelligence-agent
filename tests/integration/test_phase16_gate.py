"""Phase 16 Final Full E2E Gate（ADR-0019）。

承载分段独立断言（D1）。各分段 test function 只断自己负责的那一段语义，
不混进别的分段（维护成本 / CI 信号噪声比的权衡见 ADR）。

当前覆盖：
- T1 #126 `test_critical_path_e2e`：薄编排层 4 轮关键路径全链通
- T2 #127 `test_research_kb_insufficient_web_citation`：research → KB 证据不足 → web 补齐 → citation validity
- T2 #127 `test_permission_violation_blocked`：显式越权被 PERMISSION_DENIED 拦下 + 合法调用照常执行
- T3 #128 `test_coding_edit_test_failure_retry`：写失败测试 → 跑测试（exit_code=1 不重试）→ 修正重跑通过
- T3 #128 `test_mutating_tool_ledger_recorded`：write/edit 副作用持久化 + Ledger PENDING→SUCCEEDED 流转

后续分段（T4-T5）继续在本文件追加独立 test function 或独立 gate 文件。

跑法：
    .venv/Scripts/python.exe -m pytest tests/integration/test_phase16_gate.py -m integration -v
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_harness.agent import AgentRuntime
from agent_harness.sandbox import LocalSubprocessSandbox
from agent_harness.session import (
    MODEL_COMPLETED,
    RUN_COMPLETED,
    TOOL_CALL,
    TOOL_RESULT,
)
from agent_harness.storage import OperationState, SqliteOperationLedger
from agent_harness.tooling import ErrorCode, PermissionPolicy, ToolExecutor
from evaluation.assertions import dangling_tool_call_ids
from tests.conftest import make_session
from tests.integration._phase16_helpers import (
    PHASE16_WEB_URL,
    build_coding_executor,
    build_coding_registry,
    build_critical_path_registry,
    build_kb_seeded_registry,
    build_permission_registry,
    coding_failure_then_fix_script,
    critical_path_scripted_model,
    kb_insufficient_then_web_script,
    mutating_tool_ledger_script,
    permission_violation_script,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _tool_result_payload(result_event) -> dict:
    """解 TOOL_RESULT 事件 → ToolResult dict。

    event.data["content"] 直接是 ToolResult.model_dump_json()（单层 JSON），
    其中 ToolResult.data 对 KB/Web 工具是 ``{"output": json.dumps(领域payload)}``
    （第二层 JSON）；对 bash/read 这类工具就是普通 dict。本 helper 只解第一层，
    调用方按需再解 data.output。
    """
    return json.loads(result_event.data["content"])


@pytest.mark.asyncio
async def test_critical_path_e2e(tmp_path):
    """薄编排层：4 轮简化关键路径全链通（ADR-0019 D1）。

    断言维度（ADR-0019 D8 Gate 指标的薄编排层子集）：
    - 事件序列完整：MODEL_COMPLETED × 4 + TOOL_CALL × 3 + TOOL_RESULT × 3
    - dangling tool call = 0（所有 tool_call 都有配对 tool_result）
    - citation 在场：web citation 格式合法且出现在最终回答
    - run terminal：RUN_COMPLETED 事件 + result.status 正常终结
    """
    registry = await build_critical_path_registry(tmp_path)
    model = critical_path_scripted_model()
    session = make_session(tmp_path / "sess")
    runtime = AgentRuntime(
        model, registry, ToolExecutor(registry),
    )

    result = await runtime.run(session, "请回答：python typing 是什么？并用 add 计算 1+2。")

    events = session.events

    # ── 1. run terminal ──
    assert result.final_text, "薄编排层必须有最终回答"
    assert any(e.type == RUN_COMPLETED for e in events), "RUN_COMPLETED 事件必须在场"
    assert result.status == "completed", f"run 必须正常终结，实际 status={result.status}"

    # ── 2. 事件序列完整（分段独立断言：薄编排层只断关键路径形状）──
    tool_calls = [e for e in events if e.type == TOOL_CALL]
    tool_results = [e for e in events if e.type == TOOL_RESULT]
    model_completed = [e for e in events if e.type == MODEL_COMPLETED]
    assert len(tool_calls) == 3, f"关键路径 3 次工具调用，实际 {len(tool_calls)}"
    assert len(tool_results) == 3, f"关键路径 3 次工具结果，实际 {len(tool_results)}"
    assert len(model_completed) >= 4, (
        f"4 轮模型调用至少 4 个 MODEL_COMPLETED，实际 {len(model_completed)}"
    )

    # 工具调用顺序 = 编排剧本顺序（kb → web → add）
    tool_names = [e.data.get("tool_name") for e in tool_calls]
    assert tool_names == ["retrieve_knowledge", "web_search", "add"], (
        f"工具调用顺序必须匹配剧本，实际 {tool_names}"
    )

    # ── 3. dangling tool call = 0（Gate 指标 D8）──
    dangling = dangling_tool_call_ids(events)
    assert dangling == [], f"悬空 tool_call 必须为零，实际 {dangling}"

    # ── 4. citation 在场 + 合法 ──
    # TOOL_RESULT.content = ToolResult.model_dump_json()；KB/web 工具的 data.output
    # 是再次 JSON 序列化的领域 payload（第二层）。两层都解才能拿到 is_sufficient
    # 与 citation 字段——避免对转义细节做脆弱字符串匹配。
    def _tool_domain_payload(result_event) -> dict:
        outer = _tool_result_payload(result_event)
        return json.loads(outer["data"]["output"])

    # retrieve_knowledge：is_sufficient 必须为 False（编排前提：KB 证据不足
    # → 触发 Retrieval Fallback affordance）
    kb_payload = _tool_domain_payload(tool_results[0])
    assert kb_payload["is_sufficient"] is False, (
        f"KB 证据必须如实标记不足，实际 is_sufficient={kb_payload['is_sufficient']}"
    )
    assert kb_payload["hint"], "证据不足时 KB 工具必须下发 web_search affordance hint"

    # web_search：citation 格式合法（web:<url>）
    web_payload = _tool_domain_payload(tool_results[1])
    assert web_payload["hits"], "fake web provider 默认文档必须命中 Python 官方文档"
    assert web_payload["hits"][0]["citation"].startswith("web:http"), (
        f"web citation 格式必须为 web:<url>，实际 {web_payload['hits'][0]['citation']!r}"
    )

    # 最终回答引用 web 来源（编排剧本第 4 轮的 AIMessage.content）
    assert f"web:{PHASE16_WEB_URL}" in result.final_text, (
        f"最终回答必须引用 web citation，实际 final_text={result.final_text!r}"
    )


# ─── T2 #127：research / KB insufficient / web / citation 分段独立断言 ──────


@pytest.mark.asyncio
async def test_research_kb_insufficient_web_citation(tmp_path):
    """research → KB 证据不足 → web 补齐 → citation validity（ADR-0019 D1/D8）。

    分段独立于薄编排层 critical path：本测试聚焦 KB 真实检索路径的 is_sufficient
    信号 + citation validity 的"格式合法 + KB chunk 可查"双重维度。

    断言维度：
    - KB 检索真实走 store.search → service.retrieve 路径，命中空 → is_sufficient=False
    - is_sufficient=False 时 KB 工具下发 Retrieval Fallback affordance hint（web_search）
    - web_search 被编排剧本第 2 轮触发，命中默认 demo 文档
    - citation validity Gate（D8）：web citation 格式合法（web:<url>）；最终回答含合法 citation
    - dangling tool call = 0
    """
    registry, kb_service, seeded_source = await build_kb_seeded_registry(tmp_path)
    model = kb_insufficient_then_web_script()
    session = make_session(tmp_path / "t2_research")
    runtime = AgentRuntime(model, registry, ToolExecutor(registry))

    result = await runtime.run(session, "请回答：python typing 是什么？")

    events = session.events
    tool_calls = [e for e in events if e.type == TOOL_CALL]
    tool_results = [e for e in events if e.type == TOOL_RESULT]

    # ── 1. 工具调用顺序 = 编排剧本（kb → web）──
    tool_names = [e.data.get("tool_name") for e in tool_calls]
    assert tool_names == ["retrieve_knowledge", "web_search"], (
        f"research 分段工具调用顺序必须 kb → web，实际 {tool_names}"
    )
    assert len(tool_results) == 2, f"分段两次工具调用，实际结果 {len(tool_results)}"

    # ── 2. KB 证据不足信号诚实（真实 store.search → service.retrieve 路径）──
    # FakeKnowledgeVectorStore.search 用 substring 词重叠评分；预置语料与 query
    # 零词重叠 → top hit 的 score 必 < min_score(0.6) → is_sufficient=False。
    # 这证明 KB 不足信号走的是真实评分路径，不是 fake hook 直接返回空。
    def _kb_domain(result_event) -> dict:
        outer = _tool_result_payload(result_event)
        return json.loads(outer["data"]["output"])

    kb_payload = _kb_domain(tool_results[0])
    assert kb_payload["is_sufficient"] is False, (
        f"KB 必须如实返回不足信号（top hit score < min_score），实际 is_sufficient="
        f"{kb_payload['is_sufficient']}"
    )
    # 诚实性验证：如有 hits，top hit 的 score 必 < min_score(0.6)，这才是
    # is_sufficient=False 的根因（而不是 fake 强制返回空）
    if kb_payload["hits"]:
        top_score = kb_payload["hits"][0]["score"]
        assert top_score < 0.6, (
            f"is_sufficient=False 的根因必须是 top hit score < min_score(0.6)，"
            f"实际 top score={top_score}"
        )
    # 证据不足必须下发 Retrieval Fallback affordance（编排层据此知道可调 web_search）
    assert kb_payload.get("hint"), (
        "is_sufficient=False 时 KB 工具必须下发 web_search affordance hint"
    )

    # ── 3. citation validity Gate（D8）：web citation 格式合法 ──
    web_payload = _kb_domain(tool_results[1])
    assert web_payload["hits"], "fake web provider 默认文档必须命中 Python 官方文档"
    web_citation = web_payload["hits"][0]["citation"]
    assert web_citation.startswith("web:"), (
        f"web citation 必须是 web:<url> 格式，实际 {web_citation!r}"
    )

    # ── 4. 最终回答含合法 citation（编排剧本第 3 轮的 AIMessage.content）──
    assert f"web:{PHASE16_WEB_URL}" in result.final_text, (
        f"最终回答必须引用 web citation，实际 final_text={result.final_text!r}"
    )

    # ── 5. dangling tool call = 0（D8）──
    assert dangling_tool_call_ids(events) == [], "悬空 tool_call 必须为零"

    # ── 6. citation validity Gate（D8）延伸：KB seeded source 可经 read_source 反查 ──
    # 证明 citation 不是凭空字符串——预置 source 经 service.read_source 可定位到真实 chunk。
    from tests.integration._phase16_helpers import PHASE16_IDENTITY

    read_result = await kb_service.read_source(
        citation=f"kb:{seeded_source}#0", identity=PHASE16_IDENTITY,
    )
    assert read_result.match is not None, (
        f"citation kb:{seeded_source}#0 必须能反查到真实 chunk（citation validity 可查维度）"
    )
    assert "团建" in read_result.match.content, (
        f"反查到的 chunk 内容必须匹配预置文本，实际 {read_result.match.content!r}"
    )


# ─── T2 #127：permission violation 分段独立断言 ──────────────────────────────


@pytest.fixture()
def t2_sandbox(tmp_path: Path) -> LocalSubprocessSandbox:
    """T2 permission 分段用的 LocalSubprocessSandbox。

    预置 notes.txt 让编排剧本第 2 轮的 read 工具能成功读取——证明"越权被拦
    但合法调用照常执行"，不是"一被拦全链崩"。
    """
    sandbox = LocalSubprocessSandbox(workspace_root=tmp_path / "t2_sandbox")
    sandbox.write_text("notes.txt", "合法 workspace 内容。")
    return sandbox


@pytest.mark.asyncio
async def test_permission_violation_blocked(t2_sandbox: LocalSubprocessSandbox, tmp_path):
    """显式越权被 PERMISSION_DENIED 拦下 + 合法调用照常执行（ADR-0019 D8 permission violation=0）。

    链路：
    1. 模型试图跑 bash（DANGER）→ 默认 WORKSPACE_WRITE policy + 无 approval_callback
       → approval gate 拒绝 → PERMISSION_DENIED（非重试）
    2. 模型改用 read（READ_ONLY）→ 永远放行 → 成功读 notes.txt
    3. 模型基于 read 结果给最终回答

    "permission violation=0" 的精确语义（ADR-0019 D8）：
    - 显式越权尝试必须被拦（PERMISSION_DENIED 事件在场，且 error_code 正确）
    - 合法调用在同一链路里照常执行（read 的 ToolResult.ok=True）
    - 两者都成立才算"边界守住了"——只断前者会漏"误伤"，只断后者会漏"放行越权"。
    """
    registry = build_permission_registry(t2_sandbox)
    # 关键：无 approval_callback + 默认 WORKSPACE_WRITE policy。
    # BashTool(DANGER) 在该配置下必被 approval gate 拒（同 tests/tooling/test_approval_gate.py
    # test_bash_denied_without_callback 的真实路径，非 fake stub）。
    executor = ToolExecutor(
        registry, policy=PermissionPolicy.WORKSPACE_WRITE, approval_callback=None,
    )
    model = permission_violation_script()
    session = make_session(tmp_path / "t2_perm")
    runtime = AgentRuntime(model, registry, executor)

    result = await runtime.run(session, "帮我跑 bash 再读 notes.txt。")

    events = session.events
    tool_calls = [e for e in events if e.type == TOOL_CALL]
    tool_results = [e for e in events if e.type == TOOL_RESULT]

    # ── 1. 工具调用顺序 = 剧本（bash 越权 → read 合法）──
    tool_names = [e.data.get("tool_name") for e in tool_calls]
    assert tool_names == ["bash", "read"], (
        f"permission 分段工具调用顺序必须 bash → read，实际 {tool_names}"
    )
    assert len(tool_results) == 2

    # ── 2. 越权 bash 被 PERMISSION_DENIED 拦下（approval gate 真实路径）──
    bash_payload = _tool_result_payload(tool_results[0])
    assert bash_payload["ok"] is False, "越权 bash 必须被拦下（ok=False）"
    assert bash_payload["error_code"] == ErrorCode.PERMISSION_DENIED.value, (
        f"越权 bash 必须 PERMISSION_DENIED，实际 error_code="
        f"{bash_payload['error_code']!r}，message={bash_payload['message']!r}"
    )
    assert bash_payload["retryable"] is False, (
        "permission denial 必须非重试（不会自动重试越权调用）"
    )

    # ── 3. 合法 read 在同一链路照常执行（permission violation=0 的"合法放行"维度）──
    read_payload = _tool_result_payload(tool_results[1])
    assert read_payload["ok"] is True, (
        f"合法 read 必须放行（ok=True），实际 error_code={read_payload.get('error_code')!r}"
    )
    assert read_payload["error_code"] is None, (
        f"合法 read 不应携带 error_code，实际 {read_payload['error_code']!r}"
    )
    # read 返回的 data 含合法 workspace 文件内容
    assert read_payload["data"]["path"] == "notes.txt"
    assert "合法" in read_payload["data"]["content"], (
        f"read 必须返回预置的 notes.txt 内容，实际 {read_payload['data']['content']!r}"
    )

    # ── 4. run terminal：越权不阻断整链，最终回答在场 ──
    assert result.final_text, "permission 分段必须有最终回答（越权被拦不等于全链崩）"
    assert any(e.type == RUN_COMPLETED for e in events)

    # ── 5. dangling tool call = 0（D8）──
    assert dangling_tool_call_ids(events) == [], (
        "permission 分段悬空 tool_call 必须为零（每个 tool_call 都有配对 tool_result）"
    )


# ─── T3 #128：coding / edit / test-failure / mutating-tool 分段独立断言 ──────


@pytest.mark.asyncio
async def test_coding_edit_test_failure_retry(tmp_path):
    """写失败测试 → 跑测试（exit_code=1 不重试）→ 修正重跑通过（ADR-0002 + ADR-0019 D6）。

    核心断言：
    - bash 工具被执行 4 次（write-fail / run-fail / write-pass / run-pass）
    - 测试失败（exit_code=1）时 ToolResult.ok=True（ADR-0002：非零退出是确定性失败，
      不是工具异常）→ ToolExecutor 不重试
    - 模型据此调整策略（覆写测试文件）而非原地重试同一条命令
    - 最终重跑 exit_code=0 → 测试通过
    - 文件系统副作用持久化（test_fail.py 真被写入）
    - dangling=0
    """
    sandbox = LocalSubprocessSandbox(workspace_root=tmp_path / "t3_coding")
    ledger = SqliteOperationLedger(tmp_path / "t3_coding_ledger.db")
    await ledger.initialize()
    registry = build_coding_registry(sandbox)
    executor = build_coding_executor(sandbox, ledger)
    model = coding_failure_then_fix_script()
    session = make_session(tmp_path / "t3_coding_sess")
    runtime = AgentRuntime(model, registry, executor)

    result = await runtime.run(session, "帮我写一个测试并跑通它。")

    events = session.events
    tool_calls = [e for e in events if e.type == TOOL_CALL]
    tool_results = [e for e in events if e.type == TOOL_RESULT]

    # ── 1. 4 次 bash 调用，顺序匹配剧本 ──
    assert len(tool_calls) == 4, f"coding 分段 4 次 bash 调用，实际 {len(tool_calls)}"
    assert all(e.data.get("tool_name") == "bash" for e in tool_calls), (
        "coding 分段全部走 bash"
    )
    call_ids = [e.data.get("tool_call_id") for e in tool_calls]
    assert call_ids == [
        "t3-write-fail", "t3-run-fail", "t3-write-pass", "t3-run-pass",
    ], f"bash 调用顺序必须匹配剧本，实际 {call_ids}"

    # ── 2. 测试失败（exit_code=1）是 ok=True 的确定性失败（ADR-0002）──
    run_fail_payload = _tool_result_payload(tool_results[1])
    assert run_fail_payload["ok"] is True, (
        "bash 非零退出时 ToolResult.ok 必须 True（ADR-0002：exit_code 不是工具异常）"
    )
    assert run_fail_payload["data"]["exit_code"] != 0, (
        f"第一次跑测试必须失败（exit_code != 0），实际 "
        f"exit_code={run_fail_payload['data']['exit_code']}"
    )
    # retryable 必须为 False（ok=True 的结果天然不重试；这条断言锁定语义不被未来改动破坏）
    assert run_fail_payload["retryable"] is False, (
        "确定性失败（ok=True, exit_code!=0）不应标记 retryable"
    )

    # ── 3. ToolExecutor 没有重试失败的 bash（Ledger 只有 1 条 run-fail 操作）──
    run_fail_op = await ledger.get(session.session_id, "t3-run-fail")
    assert run_fail_op is not None, "run-fail 操作必须在 Ledger 里有记录"
    assert run_fail_op.state is OperationState.SUCCEEDED, (
        f"bash ok=True 的操作 Ledger 状态必须 SUCCEEDED（不是 FAILED/重试），"
        f"实际 {run_fail_op.state}"
    )

    # ── 4. 修正后重跑通过（exit_code=0）──
    run_pass_payload = _tool_result_payload(tool_results[3])
    assert run_pass_payload["ok"] is True
    assert run_pass_payload["data"]["exit_code"] == 0, (
        f"修正后重跑必须 exit_code=0，实际 "
        f"exit_code={run_pass_payload['data']['exit_code']}, "
        f"stderr={run_pass_payload['data'].get('stderr')}"
    )

    # ── 5. 文件系统副作用持久化（test_fail.py 真被写入且最终内容是通过的测试）──
    persisted = (tmp_path / "t3_coding" / "test_fail.py").read_text(encoding="utf-8")
    assert "assert True" in persisted, (
        f"test_fail.py 最终内容必须是修正后的通过测试，实际 {persisted!r}"
    )

    # ── 6. run terminal + dangling=0 ──
    assert result.final_text, "coding 分段必须有最终回答"
    assert any(e.type == RUN_COMPLETED for e in events)
    assert dangling_tool_call_ids(events) == [], "coding 分段悬空 tool_call 必须为零"


@pytest.mark.asyncio
async def test_mutating_tool_ledger_recorded(tmp_path):
    """write/edit 副作用持久化 + Ledger PENDING→SUCCEEDED 流转（ADR-0004 + ADR-0019 D6）。

    核心断言：
    - write 创建 doc.md → 文件真实写入；Ledger 记录 SUCCEEDED
    - edit 替换 doc.md 内容 → 文件真实变更；Ledger 记录 SUCCEEDED
    - 两次 MUTATING 操作都在 Ledger 里有完整记录（list_for_session 顺序 = 执行顺序）
    - 副作用持久化可经 sandbox.read_text 反查验证
    - dangling=0

    这是 T4 kill/reconcile 的前置上下文：mutating tool 先正常跑通，
    然后 T4 才能验证 kill 后 Ledger 残留态经 RecoveryCoordinator 对账。
    """
    sandbox = LocalSubprocessSandbox(workspace_root=tmp_path / "t3_mutating")
    ledger = SqliteOperationLedger(tmp_path / "t3_mutating_ledger.db")
    await ledger.initialize()
    registry = build_coding_registry(sandbox)
    executor = build_coding_executor(sandbox, ledger)
    model = mutating_tool_ledger_script()
    session = make_session(tmp_path / "t3_mutating_sess")
    runtime = AgentRuntime(model, registry, executor)

    result = await runtime.run(session, "帮我写一份文档并编辑它。")

    events = session.events
    tool_results = [e for e in events if e.type == TOOL_RESULT]

    # ── 1. 两次 MUTATING 工具调用都成功（ok=True）──
    assert len(tool_results) == 2, f"mutating 分段 2 次工具调用，实际 {len(tool_results)}"
    write_payload = _tool_result_payload(tool_results[0])
    edit_payload = _tool_result_payload(tool_results[1])
    assert write_payload["ok"] is True, (
        f"write 必须成功，实际 error_code={write_payload.get('error_code')}"
    )
    assert edit_payload["ok"] is True, (
        f"edit 必须成功，实际 error_code={edit_payload.get('error_code')}"
    )

    # ── 2. 副作用持久化：doc.md 真被写 + 被 edit，最终内容含"已编辑完成"──
    persisted = (tmp_path / "t3_mutating" / "doc.md").read_text(encoding="utf-8")
    assert "已编辑完成。" in persisted, (
        f"edit 副作用必须持久化到 doc.md，实际 {persisted!r}"
    )
    assert "待编辑" not in persisted, (
        f"edit 必须替换掉 old_string，实际仍含旧文本：{persisted!r}"
    )

    # ── 3. Ledger 记录完整：write + edit 都 SUCCEEDED，顺序 = 执行顺序 ──
    operations = await ledger.list_for_session(session.session_id)
    assert len(operations) == 2, (
        f"Ledger 必须记录 2 次 MUTATING 操作，实际 {len(operations)}"
    )
    assert all(op.state is OperationState.SUCCEEDED for op in operations), (
        f"两次 MUTATING 操作都必须 SUCCEEDED，实际 states="
        f"{[op.state for op in operations]}"
    )
    op_ids = [op.tool_call_id for op in operations]
    assert op_ids == ["t3-write", "t3-edit"], (
        f"Ledger 操作顺序必须 = 执行顺序（write → edit），实际 {op_ids}"
    )
    # 操作的 tool_name 与 args_identity 必须记录（reconcile 时据此判断幂等性）
    assert operations[0].tool_name == "write", (
        f"第一条操作 tool_name 必须 write，实际 {operations[0].tool_name}"
    )
    assert operations[1].tool_name == "edit", (
        f"第二条操作 tool_name 必须 edit，实际 {operations[1].tool_name}"
    )

    # ── 4. dangling=0 ──
    assert dangling_tool_call_ids(events) == [], "mutating 分段悬空 tool_call 必须为零"
    assert result.final_text, "mutating 分段必须有最终回答"
