"""Phase 9 CLI Renderer：AgentEvent 流 → 终端文本；会话持久化；失败退出码。

渲染约定借鉴 pi-mono / oh-my-pi（均 MIT，见 cli.py 模块 docstring 署名）：
状态行语法、参数折叠、结果尾部预览、时长徽章、用量页脚；V1 采用 ascii 符号
（oh-my-pi 的 ascii preset 路线——Windows GBK 控制台编码 ✔/⏳ 会崩）。
渲染器是事件流的纯函数：完整事实源是 Session JSONL，终端只挑人要看的。
"""

import json

import pytest
from langchain_core.messages import AIMessage

from agent_harness.agent import AgentEvent
from agent_harness.cli import (
    StreamRenderer,
    _collapse_args,
    _emit_stderr,
    _format_tokens,
    _preview_window,
    _render_footer,
    run,
)
from agent_harness.cli_theme import Theme
from agent_harness.session import (
    MODEL_COMPLETED,
    MODEL_REQUEST,
    MODEL_REQUEST_STARTED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_STARTED,
    SESSION_STARTED,
    TEXT_DELTA,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
    JsonlSessionStore,
)
from tests.scripted_model import ScriptedModel


def _event(event_type: str, data: dict) -> AgentEvent:
    return AgentEvent(type=event_type, data=data, run_id="r1", step_id=1)


class TestStreamRenderer:
    def test_deltas_stream_verbatim_without_newlines(self):
        out: list[str] = []
        renderer = StreamRenderer(out.append)
        renderer.handle(_event(TEXT_DELTA, {"delta": "你"}))
        renderer.handle(_event(TEXT_DELTA, {"delta": "好"}))
        assert out == ["你", "好"], "delta 是流式正文，逐段原样续写"

    def test_tool_call_closes_delta_and_collapses_args(self):
        out: list[str] = []
        renderer = StreamRenderer(out.append)
        renderer.handle(_event(TEXT_DELTA, {"delta": "正在查"}))
        renderer.handle(_event(TOOL_CALL, {
            "tool_call_id": "c1", "tool_name": "bash",
            "args": {"command": "ls -la", "timeout": 5}}))
        # plain 模式：glyph/分隔符保留、零 ANSI——布局与模式无关（#734）
        assert "".join(out) == '正在查\n\n● bash command="ls -la" · timeout=5\n'

    def test_tool_call_truecolor(self):
        out: list[str] = []
        renderer = StreamRenderer(out.append, theme=Theme(color="truecolor"))
        renderer.handle(_event(TOOL_CALL, {
            "tool_call_id": "c1", "tool_name": "bash",
            "args": {"command": "ls -la", "timeout": 5}}))
        joined = "".join(out)
        # ● 与 tool_name 着 accent（品牌粉 #f1b3ca），参数段着 muted
        assert "\x1b[38;2;241;179;202m●\x1b[0m" in joined
        assert "\x1b[38;2;241;179;202mbash\x1b[0m" in joined

    def test_tool_call_ascii_preset(self):
        out: list[str] = []
        renderer = StreamRenderer(
            out.append, theme=Theme(color="truecolor", glyph_set="ascii"))
        renderer.handle(_event(TOOL_CALL, {
            "tool_call_id": "c1", "tool_name": "bash",
            "args": {"command": "ls -la", "timeout": 5}}))
        joined = "".join(out)
        # ascii preset：pending glyph=[*]（OMP symbols.ts L1173）、sep.dot=" - "（L1233）
        assert "[*]" in joined
        assert " - " in joined

    def test_tool_result_shows_status_duration_and_preview(self):
        out: list[str] = []
        renderer = StreamRenderer(out.append)
        renderer.handle(_event(TOOL_RESULT, {
            "tool_call_id": "c1",
            "content": json.dumps({
                "ok": True, "message": "a\nb\nc\nd",
                "metadata": {"duration_ms": 1234}})}))
        # 4 行 ≤ 5：全部显示、无截断行（#736 取行方向修正后旧 `... +1 more lines` 消失）
        # P0-7：状态标签 `●`（plain 无色；suffix 归 P0-5，本票保留现状 `(1.2s)`）
        assert out == ["  ● (1.2s)\n", "  a\n", "  b\n", "  c\n", "  d\n"]

    def test_preview_tail_five(self):
        """8 行输出取尾部 5 行（l4..l8）；hint 在保留行之前——隐藏的是更早的行。"""
        out: list[str] = []
        renderer = StreamRenderer(out.append)
        renderer.handle(_event(TOOL_RESULT, {
            "tool_call_id": "c1",
            "content": json.dumps({
                "ok": True, "message": "\n".join(f"l{i}" for i in range(1, 9)),
                "metadata": {"duration_ms": 1234}})}))
        assert out == ["  ● (1.2s)\n", "  └ … (3 earlier lines)\n",
                       "  l4\n", "  l5\n", "  l6\n", "  l7\n", "  l8\n"]
        joined = "".join(out)
        # 顺序即契约：hint 在保留行之前（隐藏的是更早的行，在上方）
        assert joined.index("  └ … (3 earlier lines)\n") < joined.index("  l4\n")

    def test_preview_exactly_five(self):
        """恰好 5 行：全显示，无截断行。"""
        out: list[str] = []
        StreamRenderer(out.append).handle(_event(TOOL_RESULT, {
            "tool_call_id": "c1",
            "content": json.dumps({
                "ok": True, "message": "\n".join(f"l{i}" for i in range(1, 6))})}))
        assert out == ["  ●\n", "  l1\n", "  l2\n", "  l3\n", "  l4\n", "  l5\n"]

    def test_preview_empty(self):
        """message 为空：无预览行、无截断行（现状保持）。"""
        out: list[str] = []
        StreamRenderer(out.append).handle(_event(TOOL_RESULT, {
            "tool_call_id": "c1",
            "content": json.dumps({"ok": True, "message": ""})}))
        assert out == ["  ●\n"]

    def test_tool_failure_marked_without_duration(self):
        out: list[str] = []
        renderer = StreamRenderer(out.append)
        renderer.handle(_event(TOOL_RESULT, {
            "tool_call_id": "c1",
            "content": json.dumps({"ok": False, "message": "boom"})}))
        assert out == ["  ●\n", "  boom\n"]

    def test_run_completed_prints_usage_footer(self):
        out: list[str] = []
        renderer = StreamRenderer(out.append, theme=Theme(color="nocolor"))
        renderer.handle(_event(RUN_COMPLETED, {
            "final_text": "完成",
            "usage_total": {"prompt_tokens": 1200, "completion_tokens": 345}}))
        # #738：页脚升级为 `↑in ↓out`（Pi footer.ts 语法）；左对齐单行，无模型名/右对齐
        assert out == ["\n", "↑1.2k ↓345\n"]

    def test_run_completed_without_usage_prints_blank_line_only(self):
        out: list[str] = []
        StreamRenderer(out.append).handle(
            _event(RUN_COMPLETED, {"final_text": "done"}))
        assert out == ["\n"]

    def test_run_failed_shows_reason(self):
        # P0-7（AC1）：plain 档整块 `● 人话 (技术原因)。下一步：指引`，零 ANSI
        out: list[str] = []
        StreamRenderer(out.append, theme=Theme(color="nocolor")).handle(
            _event(RUN_FAILED, {"reason": "cancelled"}))
        assert out == [
            (
                "\n● 运行被手动取消 (cancelled)。"
                "下一步：用 `agent-harness resume <session_id>` 接上同一次运行\n"
            )
        ]

    def test_run_failed_message_priority(self):
        """P0-7（AC2）：data 带 message ⇒ 走归因面固定文案（ADR-0033），不套 run 级表；
        provider 类文案内已含"请…"指引 ⇒ 无「。下一步：」段。"""
        out: list[str] = []
        StreamRenderer(out.append, theme=Theme(color="nocolor")).handle(
            _event(RUN_FAILED, {
                "reason": "provider_auth_failed",
                "message": "模型供应商鉴权失败（API Key 无效或无权限），请检查供应商凭证配置"}))
        assert out == [
            (
                "\n● 模型供应商鉴权失败（API Key 无效或无权限），请检查供应商凭证配置"
                " (provider_auth_failed)\n"
            )
        ]

    def test_run_failed_unknown_reason(self):
        """P0-7（AC3）：未分类 reason ⇒ 兜底人话 + 后端日志指引；reason 缺失时省略 ` ()` 段。"""
        out: list[str] = []
        StreamRenderer(out.append, theme=Theme(color="nocolor")).handle(
            _event(RUN_FAILED, {"reason": "weird_x"}))
        assert out == [
            "\n● 运行失败（weird_x），原因未分类。下一步：完整原始信息见后端日志\n"
        ]
        out2: list[str] = []
        StreamRenderer(out2.append, theme=Theme(color="nocolor")).handle(
            _event(RUN_FAILED, {}))
        assert out2 == ["\n● 运行失败，原因未分类。下一步：完整原始信息见后端日志\n"]

    def test_run_failed_truecolor_block(self):
        """P0-7（AC6）：真彩档整块 ERR 色（#e5726f → 215;95;95）包住 `●` 到句尾。"""
        out: list[str] = []
        StreamRenderer(out.append, theme=Theme(color="truecolor")).handle(
            _event(RUN_FAILED, {"reason": "cancelled"}))
        assert out == [
            (
                "\n\x1b[38;2;215;95;95m"
                "● 运行被手动取消 (cancelled)。"
                "下一步：用 `agent-harness resume <session_id>` 接上同一次运行"
                "\x1b[0m\n"
            )
        ]

    def test_persistence_only_events_are_silent(self):
        """model/completed、user/message 等持久化镜像不渲染——事实在 JSONL，
        重复打印只会把终端变成第二份日志（与不变量 #5/#6 同一哲学）。"""
        out: list[str] = []
        renderer = StreamRenderer(out.append)
        for event_type, data in ((USER_MESSAGE, {"content": "hi"}),
                                 (MODEL_COMPLETED, {"content": "你好"})):
            renderer.handle(_event(event_type, data))
        assert out == []


def test_collapse_args_flattens_newlines():
    """字符串参数里的换行压成单个空格（oh-my-pi flattenForHeader 思想，#734），
    状态行永远单行；引号规则：压平后含空格加引号。"""
    result = _collapse_args({"command": "echo a\necho b"}, sep=" · ")
    assert "\n" not in result
    assert result == 'command="echo a echo b"'


def test_preview_window():
    """`_preview_window` 纯函数：keep="end" 取尾部，隐藏数 = 总行数 − keep。"""
    assert _preview_window(["a", "b"], 5) == (["a", "b"], 0)
    visible, hidden = _preview_window([str(i) for i in range(7)], 5)
    assert visible == ["2", "3", "4", "5", "6"]
    assert hidden == 2


def test_emit_stderr_is_muted_gray(capsys):
    """P0-7（AC8）：stderr 统一 muted 灰（246）；Pi 纪律——stderr 不染 ERR 红。"""
    _emit_stderr("x", theme=Theme(color="truecolor"))
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "\x1b[38;2;148;148;148mx\x1b[0m\n"

    _emit_stderr("x", theme=Theme(color="nocolor"))
    captured = capsys.readouterr()
    assert captured.err == "x\n"


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (999, "999"),
        (1200, "1.2k"),
        (9999, "10.0k"),
        (15230, "15k"),
        (999999, "1000k"),
        (1500000, "1.5M"),
        (15500000, "16M"),
        (None, "?"),
        (-5, "?"),
        ("x", "?"),
        (True, "?"),
    ],
)
def test_format_tokens_pi_five_tiers(count, expected):
    """`_format_tokens` Pi 五档边界（#738 票面 §1 表逐条；`<10000` 与 `>=10000`
    的 k 档、`<10000000` 与 `>=10000000` 的 M 档各走小数/整数压缩不同分支）。"""
    assert _format_tokens(count) == expected


def test_render_footer_left_aligned_no_padding():
    """`_render_footer` 返回左对齐单行、无填充（#738 去右对齐）。"""
    assert _render_footer(
        {"prompt_tokens": 1200, "completion_tokens": 345},
        theme=Theme(color="nocolor"),
    ) == "↑1.2k ↓345"


def test_render_footer_drops_none_or_zero_segments():
    """`None`/0 的段 drop；两段都无 → 空串（Pi L181-182 语义）。"""
    assert _render_footer(
        {"prompt_tokens": None, "completion_tokens": 345},
        theme=Theme(color="nocolor"),
    ) == "↓345"
    assert _render_footer(
        {"prompt_tokens": 0, "completion_tokens": 345},
        theme=Theme(color="nocolor"),
    ) == "↓345"
    assert _render_footer({}, theme=Theme(color="nocolor")) == ""


def test_render_footer_muted_truecolor():
    """dim 段统一取 P0-1 的 muted=（148,148,148）（跨票口径 #738）。"""
    assert _render_footer(
        {"prompt_tokens": 1200, "completion_tokens": 345},
        theme=Theme(color="truecolor"),
    ) == "\x1b[38;2;148;148;148m↑1.2k ↓345\x1b[0m"


@pytest.mark.asyncio
async def test_cli_run_streams_persists_session_and_returns_final_text(
    tmp_path, monkeypatch
):
    import agent_harness.cli as cli_module
    from agent_harness.config import Settings

    monkeypatch.setattr(
        "agent_harness.cli.Settings",
        lambda: Settings(model_api_key="sk-test", workspace_dir=str(tmp_path),
                         _env_file=None),
    )
    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model",
        lambda config, **kw: ScriptedModel([AIMessage(content="你好世界")], chunk_size=2),
    )
    original_build_runtime = cli_module.build_runtime
    tool_names: set[str] = set()

    async def capture_cli_tools(**kwargs):
        runtime = await original_build_runtime(**kwargs)
        tool_names.update(tool.name for tool in runtime.registry.list())
        return runtime

    monkeypatch.setattr(cli_module, "build_runtime", capture_cli_tools)
    out: list[str] = []
    outcome = await run("打个招呼", write=out.append)

    assert outcome.final_text == "你好世界"
    assert outcome.paused is False, "正常完成不是暂停（#312：两者都可能没有回答，必须可区分）"
    assert "register_constraint" in tool_names
    assert "request_constraint_resolution" not in tool_names
    streamed = "".join(out)
    assert "你好" in streamed and "世界" in streamed, "回答经 delta 流式可见"

    # 会话持久化：完整事件链落在 JSONL（CLI 不再是无事实源的裸 ainvoke）
    store = JsonlSessionStore(root=tmp_path / "sessions")
    (session_id,) = store.list_session_ids()
    types = [event.type for event in store.read_events(session_id)]
    assert types == [SESSION_STARTED, USER_MESSAGE, RUN_STARTED,
                     MODEL_REQUEST_STARTED, TEXT_DELTA, MODEL_REQUEST,
                     MODEL_COMPLETED, RUN_COMPLETED]


@pytest.mark.asyncio
async def test_cli_run_failed_returns_empty_final_text(tmp_path, monkeypatch):
    """失败的 run 不抛异常（runtime 契约：失败事实由终结事件承载）——
    返回空 final_text 供 main() 转 SystemExit(1)，渲染层已告知原因。

    `#312`：失败**不是**暂停（`paused=False`）——两者都没有回答，退出码必须区分。
    """
    from agent_harness.config import Settings

    class ExplodingModel:
        def bind_tools(self, tools, **kwargs):
            return self

        async def astream(self, messages, **kwargs):
            raise TimeoutError("模型请求超时")
            yield AIMessage(content="")  # pragma: no cover — 声明 async generator 用

    monkeypatch.setattr(
        "agent_harness.cli.Settings",
        lambda: Settings(model_api_key="sk-test", workspace_dir=str(tmp_path),
                         _env_file=None),
    )
    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model", lambda config, **kw: ExplodingModel(),
    )
    out: list[str] = []
    outcome = await run("触发失败", write=out.append)
    assert outcome.final_text == ""
    assert outcome.paused is False
    # P0-7：失败块走人话渲染（未分类异常落 UNCLASSIFIED_FAILURE_MESSAGE，含类名 +
    # 后端日志指引），旧 `[run failed]` 标签已由 `●` 人话块取代。
    streamed = "".join(out)
    assert "●" in streamed and "TimeoutError" in streamed
    assert "下一步：完整原始信息见后端日志" in streamed


@pytest.mark.asyncio
async def test_cli_create_rejects_unregistered_session_tool_name_before_any_work(
    tmp_path, monkeypatch
):
    """P2-2（独立审查）：CLI create 的 session 坏名必须"任何工作开始前拒绝"（04 §9.1）。

    #564 把校验移到 pre-CAS（`session_declared_limits`）后，CLI create 漏接线
    曾让坏名静默进入 durable 账行——本用例钉住该通道与 Web 同一条 MUST。
    """
    from agent_harness.agent.budget import BudgetRejection
    from agent_harness.config import Settings

    monkeypatch.setattr(
        "agent_harness.cli.Settings",
        lambda: Settings(model_api_key="sk-test", workspace_dir=str(tmp_path),
                         _env_file=None),
    )
    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model",
        lambda config, **kw: ScriptedModel([AIMessage(content="不应到达")]),
    )
    with pytest.raises(BudgetRejection) as exc_info:
        await run("任务", session_tool_limits={"nope_tool": 1})
    assert "nope_tool" in str(exc_info.value)
    store = JsonlSessionStore(root=tmp_path / "sessions")
    assert store.list_session_ids() == [], "被拒请求零工作：不得落任何会话"
