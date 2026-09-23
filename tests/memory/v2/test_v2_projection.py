"""#298 / MEM-V2-2 的模型输入投影（R2 / AC9）。

Seam：`build_formation_input` —— 纯函数，输入是本次 run 的事件 + 更早的历史 + 已经
检索好的相似记忆，输出是**可以交给记忆模型**的结构。

本文件钉的是"进去的东西里没有不该进去的"，不是"模型能不能读懂"：R2 与 §5.2.2 的每
一条都是**排除性**约束（原始大工具输出 / artifact 内容 / 凭证 / 隐藏推理 / 全量历史必须
缺席），所以用例大多是"埋一个不该出现的字节，断言它不在序列化结果里"。

`to_prompt_payload()` 是主要断言对象——AC9 说的"到达模型输入的文本"就是这份 JSON 的
序列化结果。逐字段断言会漏掉"新加的字段没接进投影管道"这类漏洞，整份序列化则不会。

# 为什么"更早消息取最后 N 条、相似记忆取前 N 条"是刻意的不对称

两者都来自"谁排的序"：历史是**时间序**，越近越相关，所以取尾部；相似记忆由检索层
按相关性排好，调用方的顺序就是相关性顺序，投影层**不重排**、取头部。任何一处"顺手
统一成同一种取法"都会让另一处丢掉真正该进模型的内容。
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from agent_harness.memory.v2.policy import find_secret
from agent_harness.memory.v2.projection import (
    MAX_EARLIER_MESSAGES,
    MAX_MESSAGE_CHARS,
    MAX_SIMILAR_MEMORIES,
    MAX_TOOL_SUMMARY_CHARS,
    SECRET_PLACEHOLDER,
    FormationInput,
    ProjectedToolCall,
    build_formation_input,
)
from agent_harness.memory.v2.types import MemoryRecordV2
from agent_harness.session import (
    MODEL_COMPLETED,
    RUN_COMPLETED,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
    SessionEvent,
)
from agent_harness.tooling.result import ErrorCode, ToolResult

#: 形状齐备的假 key：命中扫描器，但值本身毫无意义（不是任何真实凭据）。
FAKE_KEY = "sk-" + "deadbeef" * 4
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9." + "cGF5bG9hZA" * 3 + "." + "c2lnbmF0dXJl" * 3
FAKE_PRIVATE_KEY = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"

# --------------------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------------------


def _ev(event_type: str, data: dict | None = None, event_id: str = "e1") -> SessionEvent:
    return SessionEvent(
        event_id=event_id, seq=0, type=event_type, session_id="s", data=data or {},
    )


def _user(text: str, event_id: str = "u1") -> SessionEvent:
    return _ev(USER_MESSAGE, {"content": text}, event_id)


def _injected_user(text: str = "样板纠偏", event_id: str = "u-inj") -> SessionEvent:
    return _ev(USER_MESSAGE, {"content": text, "injected_by": "guard"}, event_id)


def _assistant(text: str, event_id: str = "m1") -> SessionEvent:
    return _ev(MODEL_COMPLETED, {"content": text}, event_id)


def _call(
    tool_call_id: str, tool_name: str, args: dict | None = None, event_id: str = "tc1",
) -> SessionEvent:
    return _ev(
        TOOL_CALL,
        {"tool_call_id": tool_call_id, "tool_name": tool_name, "args": args or {}},
        event_id,
    )


def _result(
    tool_call_id: str,
    *,
    ok: bool = True,
    message: str = "done",
    data: dict | None = None,
    artifact_ref: str | None = None,
    event_id: str = "tr1",
) -> SessionEvent:
    """用**真实**的 `ToolResult` 序列化器造 `tool/result` 的 content。

    手写一份 JSON 近似会造出"事件形状其实不是这样"的测试：生产代码解析的是
    `ToolResult.model_dump_json()` 的输出，ficture 就必须是它的输出。
    """
    if ok:
        result = ToolResult.success(message, data=data)
        if artifact_ref is not None:
            result = result.model_copy(update={"artifact_ref": artifact_ref})
    else:
        result = ToolResult.failure(message, error_code=ErrorCode.TOOL_EXECUTION_ERROR)
    return _ev(
        TOOL_RESULT, {"tool_call_id": tool_call_id, "content": result.model_dump_json()},
        event_id,
    )


def _record(
    memory_id: str = "mem-1",
    *,
    kind: str = "semantic",
    scope: str = "user_global",
    content: str = "用户偏好用 pnpm 安装依赖",
) -> MemoryRecordV2:
    """一条**通过完整信封校验**的记录：project 作用域自动补 project_id。"""
    payload = {
        "semantic": {"kind": "semantic", "subject": "包管理器", "fact": "偏好 pnpm",
                     "category": "preference"},
        "episodic": {"kind": "episodic", "situation": "装依赖", "action": "用 pnpm",
                     "outcome": "成功", "lesson": "优先 pnpm"},
        "procedural": {"kind": "procedural", "trigger": "装依赖", "procedure": "用 pnpm i",
                       "success_condition": "锁文件更新"},
    }[kind]
    return MemoryRecordV2(
        id=memory_id, root_id=memory_id, version=1,
        tenant_id="tenant-secret", user_id="user-secret",
        kind=kind, tier="collection", scope=scope, content=content, payload=payload,
        project_id="proj-1" if scope == "project" else None,
        importance=0.5, strength=0.5, source_type="automatic",
        source_session_id="s", source_event_ids=["e1"],
        evidence=[{"role": "user", "excerpt": "…", "hash": "h"}],
        created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
    )


def _payload(**kwargs) -> dict[str, Any]:
    return build_formation_input(**kwargs).to_prompt_payload()


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


# --------------------------------------------------------------------------------------
# 第 1 组：文本边界与凭证切除（R2 末句 / AC9）
# --------------------------------------------------------------------------------------


def test_a_short_message_survives_verbatim() -> None:
    payload = _payload(run_events=[_user("请用 pnpm")])
    assert payload["current_run"] == [{"role": "user", "text": "请用 pnpm"}]


def test_an_over_long_message_is_truncated_with_a_marker() -> None:
    text = "a" * (MAX_MESSAGE_CHARS + 500)
    (message,) = _payload(run_events=[_user(text)])["current_run"]
    assert message["text"].startswith("a" * MAX_MESSAGE_CHARS)
    assert message["text"].endswith("[truncated]")
    assert len(message["text"]) < len(text)


def test_a_message_exactly_at_the_cap_is_not_truncated() -> None:
    text = "a" * MAX_MESSAGE_CHARS
    (message,) = _payload(run_events=[_user(text)])["current_run"]
    assert message["text"] == text


def test_a_non_string_content_does_not_break_the_projection() -> None:
    """一行坏数据只损失该行：content 形状污染时投影成空串，不抛。"""
    events = [
        _ev(USER_MESSAGE, {"content": None}, "u-none"),
        _ev(USER_MESSAGE, {"content": 42}, "u-int"),
        _ev(USER_MESSAGE, {"content": {"a": 1}}, "u-dict"),
        _ev(USER_MESSAGE, {}, "u-missing"),
    ]
    assert [m["text"] for m in _payload(run_events=events)["current_run"]] == ["", "", "", ""]


@pytest.mark.parametrize(
    "text",
    [
        f"我的 key 是 {FAKE_KEY} 请记下",
        f"token={FAKE_JWT}",
        f"这是私钥：\n{FAKE_PRIVATE_KEY}\n完",
    ],
)
def test_a_secret_is_cut_off_at_its_first_byte(text: str) -> None:
    (message,) = _payload(run_events=[_user(text)])["current_run"]
    assert find_secret(message["text"]) is None
    assert message["text"].endswith(SECRET_PLACEHOLDER)


def test_the_harmless_prefix_before_a_secret_survives() -> None:
    """整段丢弃会把"顺手贴了把 key"那一轮的其余内容一起带走；只砍命中点之后的部分。"""
    (message,) = _payload(run_events=[_user(f"我用 pnpm。key 是 {FAKE_KEY}")])["current_run"]
    assert message["text"] == f"我用 pnpm。key 是 {SECRET_PLACEHOLDER}"


def test_a_secret_straddling_the_truncation_boundary_is_removed_not_cut_apart() -> None:
    """先切凭证、后截断。反过来会把 key 的**前缀**（`sk-`）当普通文本留下。

    这是两种实现顺序的判别式：先截断时 `sk-deadbe` 会落在 1000 字符窗口内并被
    原样写出（扫描器此时已经看不到完整前缀，帮不上忙），先切则整段消失。
    """
    text = "a" * (MAX_MESSAGE_CHARS - 10) + " " + FAKE_KEY
    (message,) = _payload(run_events=[_user(text)])["current_run"]
    assert "sk-" not in message["text"]
    assert "deadbeef" not in message["text"]
    assert find_secret(message["text"]) is None
    assert message["text"].endswith(SECRET_PLACEHOLDER)


def test_a_multi_line_private_key_leaves_no_body_behind() -> None:
    """span 级替换会把 base64 正文留下——本实现从命中点整段砍，正文必然一起消失。"""
    (message,) = _payload(run_events=[_user(f"看这个\n{FAKE_PRIVATE_KEY}")])["current_run"]
    assert "MIIEowIBAAKCAQEA" not in message["text"]
    assert "PRIVATE KEY" not in message["text"]


def test_the_cut_starts_at_the_earliest_of_several_matches() -> None:
    """多个模式各自 search 后取最早起点：否则后跑到的模式会把更早的秘密留在前缀里。"""
    text = f"先 {FAKE_JWT} 后 {FAKE_KEY} 尾"
    (message,) = _payload(run_events=[_user(text)])["current_run"]
    assert FAKE_JWT not in message["text"]
    assert FAKE_KEY not in message["text"]
    assert message["text"] == "先 " + SECRET_PLACEHOLDER


def test_ordinary_technical_text_is_not_mistaken_for_a_secret() -> None:
    text = "用 pnpm i 装依赖，然后跑 pytest；哈希 4f2a9c1b8e3d 是缓存键"
    (message,) = _payload(run_events=[_user(text)])["current_run"]
    assert message["text"] == text


# --------------------------------------------------------------------------------------
# 第 2 组：当前 run 的消息（R2 前半）
# --------------------------------------------------------------------------------------


def test_the_current_run_carries_user_and_assistant_text_in_order() -> None:
    payload = _payload(run_events=[_user("第一问", "u1"), _assistant("第一答", "m1"),
                                   _user("第二问", "u2"), _assistant("第二答", "m2")])
    assert payload["current_run"] == [
        {"role": "user", "text": "第一问"},
        {"role": "assistant", "text": "第一答"},
        {"role": "user", "text": "第二问"},
        {"role": "assistant", "text": "第二答"},
    ]


def test_a_runtime_injected_user_message_is_not_a_conversation_turn() -> None:
    """注入的样板不是用户说的话（与 `resolve_evidence_source` 同一口径）。"""
    payload = _payload(run_events=[_user("请用 pnpm"), _injected_user(), _assistant("好的")])
    assert [m["text"] for m in payload["current_run"]] == ["请用 pnpm", "好的"]


@pytest.mark.parametrize(
    "event",
    [
        _ev(TOOL_RESULT, {"tool_call_id": "c1", "content": "{}"}, "tr"),
        _ev("artifact/created", {"artifact_id": "a1"}, "ar"),
        _ev(RUN_COMPLETED, {"status": "ok"}, "rc"),
        _ev("memory/degraded", {"reason_code": "x"}, "md"),
        _ev("reasoning/completed", {"content": "隐藏推理"}, "rs"),
    ],
)
def test_non_message_events_never_become_conversation_text(event: SessionEvent) -> None:
    payload = _payload(run_events=[event])
    assert payload["current_run"] == []
    assert "隐藏推理" not in _json(payload)


def test_hidden_reasoning_absent_from_the_same_run_that_has_text() -> None:
    payload = _payload(run_events=[
        _ev("reasoning/completed", {"content": "先想 A 再想 B"}, "rs"),
        _user("问"),
    ])
    assert "先想 A" not in _json(payload)


def test_the_current_run_is_not_capped_by_the_projection() -> None:
    """R2 没给 run 大小上限：run 本身是有限的，外层边界是 R10 的 32k 输入预算。

    有人给 run 加一条"顺手"的条数上限时，被砍掉的会是 run 的**尾部**——也就是
    刚刚发生、最该形成记忆的那几步。这条用例把这个决定钉住。
    """
    events = [_user(f"问{i}", f"u{i}") for i in range(20)]
    assert len(_payload(run_events=events)["current_run"]) == 20


# --------------------------------------------------------------------------------------
# 第 3 组：更早的消息（R2 "at most eight earlier user/assistant messages"）
# --------------------------------------------------------------------------------------


def test_exactly_eight_earlier_messages_all_survive() -> None:
    history = [_user(f"问{i}", f"u{i}") for i in range(MAX_EARLIER_MESSAGES)]
    payload = _payload(run_events=[], history=history)
    assert [m["text"] for m in payload["earlier_messages"]] == [
        f"问{i}" for i in range(MAX_EARLIER_MESSAGES)
    ]


def test_more_than_eight_earlier_messages_keep_the_most_recent_eight() -> None:
    history = [_user(f"问{i}", f"u{i}") for i in range(12)]
    payload = _payload(run_events=[], history=history)
    assert [m["text"] for m in payload["earlier_messages"]] == [
        f"问{i}" for i in range(4, 12)
    ]


def test_the_kept_earlier_messages_stay_in_chronological_order() -> None:
    history = [_user(f"问{i}", f"u{i}") for i in range(12)]
    texts = [m["text"] for m in _payload(run_events=[], history=history)["earlier_messages"]]
    assert texts == sorted(texts, key=lambda t: int(t[1:]))


def test_excluded_history_items_do_not_consume_the_eight_slots() -> None:
    """名额是给"能进模型的消息"的：8 条里混入工具结果 / 注入样板后，真正进模型的
    用户与助手消息仍应有 8 条，而不是被噪声挤掉。"""
    history: list[SessionEvent] = []
    for i in range(4):
        history.append(_user(f"问{i}", f"u{i}"))
        history.append(_result(f"c{i}", event_id=f"tr{i}"))
        history.append(_injected_user(event_id=f"inj{i}"))
        history.append(_assistant(f"答{i}", f"m{i}"))
        history.append(_ev(RUN_COMPLETED, {"status": "ok"}, f"rc{i}"))
    payload = _payload(run_events=[], history=history)
    assert len(payload["earlier_messages"]) == MAX_EARLIER_MESSAGES
    assert [m["text"] for m in payload["earlier_messages"]] == [
        "问0", "答0", "问1", "答1", "问2", "答2", "问3", "答3",
    ]


def test_an_empty_history_produces_no_earlier_messages() -> None:
    payload = _payload(run_events=[_user("问")])
    assert payload["earlier_messages"] == []


def test_earlier_messages_are_carried_as_conversation_text_not_raw_events() -> None:
    """历史里的大工具输出不能借"更早消息"这条通道绕回来。"""
    history = [_user("问", "u0"), _result("c1", data={"output": "X" * 5000}, event_id="tr0")]
    payload = _payload(run_events=[], history=history)
    assert [m["text"] for m in payload["earlier_messages"]] == ["问"]
    assert "X" * 100 not in _json(payload)


def test_a_secret_in_the_history_is_cut_too() -> None:
    payload = _payload(run_events=[], history=[_user(f"用 {FAKE_KEY} 登录", "u0")])
    assert FAKE_KEY not in _json(payload)


# --------------------------------------------------------------------------------------
# 第 4 组：工具调用（R2 "tool names/status/structured summaries, artifact references only"）
# --------------------------------------------------------------------------------------


def test_a_successful_tool_call_is_projected_as_name_status_summary() -> None:
    payload = _payload(run_events=[
        _call("c1", "run_shell", {"command": "pnpm i"}, "tc1"),
        _result("c1", message="install ok", event_id="tr1"),
    ])
    (tool,) = payload["tool_calls"]
    assert tool["name"] == "run_shell"
    assert tool["status"] == "success"
    assert tool["summary"] == "install ok"
    assert tool["artifacts"] == []


def test_a_failed_tool_call_is_projected_as_failure() -> None:
    payload = _payload(run_events=[
        _call("c1", "run_shell", event_id="tc1"),
        _result("c1", ok=False, message="boom", event_id="tr1"),
    ])
    assert payload["tool_calls"][0]["status"] == "failure"


def test_a_tool_call_without_a_result_is_missing_not_success() -> None:
    """读不出来就不算成功——否则"还没跑完"会被记成"成功"，R5 的独立成功事件凭空多一条。"""
    payload = _payload(run_events=[_call("c1", "run_shell", event_id="tc1")])
    assert payload["tool_calls"][0]["status"] == "missing"


@pytest.mark.parametrize(
    "content",
    ["", "not json", '"a string"', "[1, 2]", '{"message": "no ok here"}', '{"ok": "true"}'],
)
def test_an_unreadable_tool_result_content_is_unknown(content: str) -> None:
    payload = _payload(run_events=[
        _call("c1", "run_shell", event_id="tc1"),
        _ev(TOOL_RESULT, {"tool_call_id": "c1", "content": content}, "tr1"),
    ])
    assert payload["tool_calls"][0]["status"] == "unknown"


def test_tool_arguments_never_reach_the_model() -> None:
    """R2 只允许 name/status/summary/artifact refs。args 是模型自己写的，也是
    最容易夹带凭证的一处（`api_key=` 之类），所以它根本不进契约。"""
    payload = _payload(run_events=[
        _call("c1", "http_post", {"headers": {"Authorization": f"Bearer {FAKE_KEY}"},
                                  "body": "秘密正文"}, "tc1"),
    ])
    assert FAKE_KEY not in _json(payload)
    assert "秘密正文" not in _json(payload)
    assert set(payload["tool_calls"][0]) == {"name", "status", "summary", "artifacts"}


def test_structured_tool_data_never_reaches_the_model() -> None:
    """`data` 是**未外置也可能很大**的结构化输出（overflow 阈值 2000），
    既可能撑爆 prompt 也可能夹带凭证 ⇒ 根本不投影。"""
    payload = _payload(run_events=[
        _call("c1", "run_shell", event_id="tc1"),
        _result("c1", data={"stdout": "Y" * 3000, "token": FAKE_KEY}, event_id="tr1"),
    ])
    assert "Y" * 100 not in _json(payload)
    assert FAKE_KEY not in _json(payload)
    assert payload["tool_calls"][0]["summary"] == "done"


def test_a_long_tool_summary_is_truncated() -> None:
    payload = _payload(run_events=[
        _call("c1", "run_shell", event_id="tc1"),
        _result("c1", message="z" * (MAX_TOOL_SUMMARY_CHARS + 200), event_id="tr1"),
    ])
    summary = payload["tool_calls"][0]["summary"]
    assert summary.startswith("z" * MAX_TOOL_SUMMARY_CHARS)
    assert summary.endswith("[truncated]")


def test_a_secret_inside_a_tool_summary_is_cut() -> None:
    payload = _payload(run_events=[
        _call("c1", "run_shell", event_id="tc1"),
        _result("c1", message=f"exported {FAKE_KEY}", event_id="tr1"),
    ])
    assert FAKE_KEY not in _json(payload)


def test_a_failed_tool_call_keeps_its_message_as_the_summary() -> None:
    """状态（`failure`）已经表达了结构化结论，摘要仍带上工具自己那句话——
    模型据此才知道"失败在哪儿"。"""
    payload = _payload(run_events=[
        _call("c1", "run_shell", event_id="tc1"),
        _result("c1", ok=False, message="timeout after 30s", event_id="tr1"),
    ])
    assert payload["tool_calls"][0]["status"] == "failure"
    assert payload["tool_calls"][0]["summary"] == "timeout after 30s"


def test_artifact_references_are_carried_but_artifact_content_is_not() -> None:
    payload = _payload(run_events=[
        _call("c1", "read_file", event_id="tc1"),
        _result("c1", artifact_ref="art-9f2c", message="…[truncated] use read_artifact(art-9f2c)",
                event_id="tr1"),
        _ev("artifact/externalized",
            {"artifact_id": "art-9f2c", "size": 91_000, "mime_type": "text/plain"}, "ae1"),
    ])
    assert payload["tool_calls"][0]["artifacts"] == ["art-9f2c"]
    # 体积 / mime / session_id 这些随附元数据不进模型——它们只是"这条引用的来历"。
    assert "91000" not in _json(payload)
    assert "text/plain" not in _json(payload)


def test_a_secret_inside_an_artifact_reference_drops_the_reference() -> None:
    """标识符也要过扫描器。整条丢弃而不是截断——给一个被截断的 id 等于递给模型一个
    看着像名字、其实引用不到东西的假引用。"""
    payload = _payload(run_events=[
        _call("c1", "read_file", event_id="tc1"),
        _result("c1", artifact_ref=f"art-{FAKE_KEY}", event_id="tr1"),
    ])
    assert payload["tool_calls"][0]["artifacts"] == []
    assert FAKE_KEY not in _json(payload)


def test_a_long_artifact_reference_is_carried_whole() -> None:
    """标识符不截断：带截断标记的 id 引用不到任何东西。"""
    ref = "art-" + "9f2c" * 100
    payload = _payload(run_events=[
        _call("c1", "read_file", event_id="tc1"),
        _result("c1", artifact_ref=ref, event_id="tr1"),
    ])
    assert payload["tool_calls"][0]["artifacts"] == [ref]


def test_tool_calls_pair_by_id_regardless_of_arrival_order() -> None:
    payload = _payload(run_events=[
        _result("c2", ok=False, message="late", event_id="tr2"),
        _call("c1", "read_file", event_id="tc1"),
        _result("c1", message="ok1", event_id="tr1"),
        _call("c2", "run_shell", event_id="tc2"),
    ])
    assert [(t["name"], t["status"]) for t in payload["tool_calls"]] == [
        ("read_file", "success"),
        ("run_shell", "failure"),
    ]


@pytest.mark.parametrize(
    "data",
    [
        {"tool_call_id": None, "tool_name": "x", "args": {}},
        {"tool_call_id": 7, "tool_name": "x", "args": {}},
        {"tool_name": "x", "args": {}},
        {},
    ],
)
def test_a_malformed_tool_call_is_skipped_without_breaking_the_projection(
    data: dict,
) -> None:
    payload = _payload(run_events=[_ev(TOOL_CALL, data, "tc1"), _user("问")])
    assert payload["tool_calls"] == []
    assert [m["text"] for m in payload["current_run"]] == ["问"]


def test_a_non_string_tool_name_degrades_to_empty_string() -> None:
    payload = _payload(run_events=[
        _ev(TOOL_CALL, {"tool_call_id": "c1", "tool_name": None, "args": {}}, "tc1"),
    ])
    assert payload["tool_calls"][0]["name"] == ""


def test_an_empty_tool_call_list_is_a_quiet_empty_list() -> None:
    assert _payload(run_events=[_user("问")])["tool_calls"] == []


# --------------------------------------------------------------------------------------
# 第 5 组：相似记忆（R2 "at most ten similar active memories"）
# --------------------------------------------------------------------------------------


def test_up_to_ten_similar_memories_are_projected_with_four_fields_only() -> None:
    payload = _payload(
        run_events=[], similar_memories=[_record(f"mem-{i}") for i in range(3)],
    )
    assert payload["similar_memories"] == [
        {"memory_id": f"mem-{i}", "kind": "semantic", "scope": "user_global",
         "content": "用户偏好用 pnpm 安装依赖"}
        for i in range(3)
    ]


def test_more_than_ten_similar_memories_keep_the_retrieval_order_head() -> None:
    """检索层已经排好序；投影层不重排、只截断——重排会让"投影层悄悄换了个排序口径"。"""
    payload = _payload(
        run_events=[], similar_memories=[_record(f"mem-{i}") for i in range(12)],
    )
    ids = [m["memory_id"] for m in payload["similar_memories"]]
    assert ids == [f"mem-{i}" for i in range(MAX_SIMILAR_MEMORIES)]


def test_memory_identity_fields_never_reach_the_model() -> None:
    """R2 末句：模型输出不得提供可信身份，投影也不得把身份喂给它——
    跨租户 / 跨用户的数据边界不该出现在记忆模型的视野里。"""
    payload = _payload(run_events=[], similar_memories=[_record()])
    text = _json(payload)
    assert "tenant-secret" not in text
    assert "user-secret" not in text
    assert "tenant_id" not in text


def test_memory_payload_and_evidence_never_reach_the_model() -> None:
    payload = _payload(run_events=[], similar_memories=[_record()])
    assert "包管理器" not in _json(payload)
    assert "hash" not in _json(payload)


def test_a_secret_smuggled_into_a_stored_memory_is_cut_before_the_model_sees_it() -> None:
    """写入侧本该拒掉它（R7），但投影是独立的第二道——存档里已有的脏数据不能靠
    "写的时候应该拒过了"过关。"""
    payload = _payload(
        run_events=[], similar_memories=[_record(content=f"用户的 key 是 {FAKE_KEY}")],
    )
    assert FAKE_KEY not in _json(payload)
    assert payload["similar_memories"][0]["content"].endswith(SECRET_PLACEHOLDER)


def test_memory_kind_and_scope_are_projected_as_plain_strings() -> None:
    payload = _payload(
        run_events=[],
        similar_memories=[
            _record("mem-e", kind="episodic"),
            _record("mem-p", kind="procedural", scope="project"),
        ],
    )
    assert [(m["kind"], m["scope"]) for m in payload["similar_memories"]] == [
        ("episodic", "user_global"),
        ("procedural", "project"),
    ]


def test_no_similar_memories_is_a_quiet_empty_list() -> None:
    assert _payload(run_events=[_user("问")])["similar_memories"] == []


# --------------------------------------------------------------------------------------
# 第 6 组：载荷形状与整体不变量（AC9 的探针形态）
# --------------------------------------------------------------------------------------


def test_the_payload_carries_exactly_the_four_contracted_keys() -> None:
    """多一个键 = 多一条没经过本模块边界的通道。"""
    payload = _payload(run_events=[_user("问")])
    assert set(payload) == {
        "current_run", "earlier_messages", "tool_calls", "similar_memories",
    }


def test_the_payload_is_json_serialisable() -> None:
    payload = _payload(
        run_events=[_user("问"), _assistant("答"), _call("c1", "t", event_id="tc1"),
                    _result("c1", event_id="tr1")],
        history=[_user("旧", "u0")],
        similar_memories=[_record()],
    )
    assert json.loads(_json(payload)) == payload


def test_the_projection_is_deterministic() -> None:
    kwargs: dict[str, Any] = {
        "run_events": [_user("问"), _assistant("答")],
        "history": [_user("旧", "u0")],
        "similar_memories": [_record()],
    }
    assert _payload(**kwargs) == _payload(**kwargs)


def test_an_everywhere_adversarial_input_leaks_no_secret() -> None:
    """AC9 的探针形态：把凭证埋在每一条通道上，整份序列化结果必须干净。

    埋在"通道"而不是"字段"上，是因为漏掉的往往是**某一条通道整个忘了过扫描器**。
    """
    payload = _payload(
        run_events=[
            _user(f"我的 key 是 {FAKE_KEY}"),
            _injected_user(f"样板里也有 {FAKE_KEY}"),
            _assistant(f"好的，我记下 {FAKE_JWT}"),
            _call("c1", "http_post", {"Authorization": f"Bearer {FAKE_KEY}"}, "tc1"),
            _result("c1", message=f"echo {FAKE_KEY}", data={"k": FAKE_KEY},
                    artifact_ref=f"art-{FAKE_KEY}", event_id="tr1"),
            _ev("reasoning/completed", {"content": FAKE_PRIVATE_KEY}, "rs"),
        ],
        history=[_user(f"上一轮 {FAKE_KEY}", "u0")],
        similar_memories=[_record(content=f"存档里的 {FAKE_KEY}")],
    )
    text = _json(payload)
    assert find_secret(text) is None
    assert "deadbeef" not in text
    assert "eyJhbGciOiJIUzI1NiJ9" not in text


def test_an_empty_run_produces_an_empty_payload() -> None:
    assert _payload(run_events=[]) == {
        "current_run": [], "earlier_messages": [], "tool_calls": [], "similar_memories": [],
    }


def test_the_projection_result_is_frozen() -> None:
    result = build_formation_input(run_events=[_user("问")])
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.tool_calls = ()  # type: ignore[misc]


def test_the_projection_result_holds_tuples_not_mutable_lists() -> None:
    """冻结的是**容器**，不是只有外层：否则调用方能就地改掉已经"交付"的输入。"""
    result = build_formation_input(run_events=[_user("问")])
    assert isinstance(result, FormationInput)
    assert isinstance(result.current_run, tuple)
    assert isinstance(result.earlier_messages, tuple)
    assert isinstance(result.tool_calls, tuple)
    assert isinstance(result.similar_memories, tuple)


def test_no_raw_session_event_dict_is_carried_into_the_payload() -> None:
    """投影是**重建**而不是裁剪事件 dict：`session_id`/`seq`/`event_id` 这些
    运行事实不该出现在模型输入里（它们既不帮模型判断，又扩大泄漏面）。"""
    payload = _payload(run_events=[_user("问"), _assistant("答")])
    text = _json(payload)
    for absent in ("session_id", "event_id", "seq", "schema_version", "injected_by"):
        assert absent not in text


def test_a_projected_tool_call_is_the_declared_shape() -> None:
    payload = _payload(run_events=[
        _call("c1", "run_shell", event_id="tc1"),
        _result("c1", event_id="tr1"),
    ])
    tool = ProjectedToolCall(
        name=payload["tool_calls"][0]["name"],
        status=payload["tool_calls"][0]["status"],
        summary=payload["tool_calls"][0]["summary"],
        artifacts=tuple(payload["tool_calls"][0]["artifacts"]),
    )
    assert tool.name == "run_shell"
