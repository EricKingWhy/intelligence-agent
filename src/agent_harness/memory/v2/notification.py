"""后台记忆形成的人面向摘要（#1353 M7）。

`summarize_background_review_actions` 及其全部辅助函数逐行移植自
NousResearch/hermes-agent ``agent/background_review.py``（commit
b324b1f7ed52369ab4bd3b1a03378729120b0f01，MIT，Copyright (c) 2025 Nous Research）。
纯函数、无 I/O、无全局状态：输入是"review agent 的消息列表 + 上一份快照"，输出是
给人看的若干行。

# 三档

- ``off``：什么都不报（返回空列表，调用方也不发事件）；
- ``on``：泛化一句（"Memory updated" / "User profile updated"），不泄露内容；
- ``verbose``：从**工具调用参数**里取内容预览（add/replace 取 ``content`` 前 120 字符，
  remove 取 ``old_text`` 前 60 字符）。

# Owlet 信封映射（票面点名要写清楚的地方）

上游的判据是 ``json.loads(tool_message["content"])`` 得到 dict 且 ``data["success"]``
为真。Owlet 的 `ToolResult` 是 ``{ok, message, data, error_code, ...}``，而
`MemoryTool` 的 ``data`` 里已经有 ``success: True``（`memory_tool._result` /
`_gate` 都显式写入）——所以**在消息层**把 Owlet 结果渲染成
``{"success": ..., "target": ..., "message": ..., "staged": ...}`` 就是全部映射：

- ``success`` ← ``ToolResult.ok``（不是 `ok` 字段本身：上游读的是 ``data.success``，
  保持同一个键名，纯函数就不用长分支）；
- ``target`` ← ``ToolResult.data["target"]``（``memory`` / ``user``）；
- ``message`` ← ``ToolResult.message``（markdown 存储层的 "Entry added." 等）；
- ``staged`` ← ``ToolResult.data["staged"]``；本仓 stage 的是**待审写入**，
  没有上游 fork 的 ``proposal_staged`` 语义，故 ``_action_lines`` 的 staged 分支
  在 Owlet 侧**永不**产出"已落地"的行——stage 未落地就绝不报成落地。

调用参数（``tool_calls[].function.arguments``）由调用方从 `MemoryJobResult.written`
的记录合成：``version == 1`` ⇒ ``add``、``version > 1`` ⇒ ``replace``、
``status == invalidated`` ⇒ ``remove``。

# 去重与"只认成功"

两条都是上游的判据，原样保留：

- 已在 ``prior_snapshot`` 里的 tool message（按 ``tool_call_id`` 或内容）不再报——
  继承来的结果不是这一轮的新工作；
- ``data["success"]`` 为假的 payload 直接跳过（失败不是"更新了"）。
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from typing import Any

from agent_harness.memory.v2.markdown_store import target_for

#: 上游 ``_MEMORY_OP_FORMATS``：动作 → (字形, 承载预览的字段, 预览长度)。
_MEMORY_OP_FORMATS: dict[str, tuple[str, str, int]] = {
    "add": ("➕", "content", 120),
    "replace": ("✏️", "content", 120),
    "remove": ("➖", "old_text", 60),
}

#: 上游 ``_CALL_DETAIL_DEFAULTS``：进入摘要的工具调用参数字段与缺省值。
_CALL_DETAIL_DEFAULTS = (
    ("action", "?"), ("target", "memory"), ("content", ""), ("old_text", ""), ("name", ""),
    ("old_string", ""), ("new_string", ""),
)

#: 上游 ``locales/en.yaml`` 的 ``display.review.*`` 文案（本仓没有 i18n 层，
#: 因此把英文原文收在这里；改文案只改这一处）。上游的 ``t()`` 查表已内联成 f-string。
_LABELS = {"skill": "Skill", "memory": "Memory", "user": "User profile"}


def _preview(text: str, limit: int) -> str:
    return text[:limit] + ("…" if len(text) > limit else "")


def _memory_op_line(label: str, action: str, fields: dict[str, str]) -> str | None:
    """一条 add/replace/remove 的 verbose 行；没有可预览文本时返回 None。"""
    glyph, field_name, limit = _MEMORY_OP_FORMATS.get(action) or (None, "", 0)
    text = (fields.get(field_name) or "") if glyph else ""
    if not text:
        return None
    return f"{label} {glyph} {_preview(text, limit)}"


def _verbose_memory_lines(label: str, detail: dict) -> list[str]:
    """``operations`` 可以是任意 JSON 值；只有 dict 列表可用。"""
    ops_raw = detail.get("operations")
    if isinstance(ops_raw, list) and ops_raw:
        lines = [
            _memory_op_line(label, op.get("action", ""), op)
            for op in ops_raw if isinstance(op, dict)
        ]
        return [line for line in lines if line]
    return [
        _memory_op_line(label, detail.get("action", ""), detail)
        or f"{label} updated"
    ]


def _collect_review_call_details(review_messages: list[dict]) -> tuple[set, dict]:
    """review-agent 的 tool_call id → 解析后的调用参数（只收 notify 工具）。

    结果 JSON 只说 "Entry added"；动作、目标与内容预览都在调用参数里。限定
    notify 工具，是为了不让恰好成功的辅助工具冒充记忆工作。
    """
    notify_tools = {"memory", "promote_skill"}
    all_tool_call_ids: set = set()
    call_details: dict = {}
    for msg in review_messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls", []) or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function", {}) or {}
            fn_name = fn.get("name", "")
            tcid = tc.get("id")
            if tcid:
                all_tool_call_ids.add(tcid)
            if fn_name not in notify_tools or not tcid:
                continue
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                args = {}
            call_details[tcid] = {
                "tool": fn_name, "operations": args.get("operations") or [],
                **{k: args.get(k, default) for k, default in _CALL_DETAIL_DEFAULTS},
            }
    return all_tool_call_ids, call_details


def _tool_messages(messages: list[dict]) -> Iterator[dict]:
    return (m for m in messages or [] if isinstance(m, dict) and m.get("role") == "tool")


def _prior_tool_keys(prior_snapshot: list[dict]) -> tuple[set, set]:
    """``prior_snapshot`` 里已存在的 ``(tool_call_ids, contents)``。"""
    priors = list(_tool_messages(prior_snapshot))
    ids = {m["tool_call_id"] for m in priors if m.get("tool_call_id")}
    contents = {
        m["content"] for m in priors
        if not m.get("tool_call_id") and isinstance(m.get("content"), str)
    }
    return ids, contents


def _action_lines(data: dict, detail: dict, verbose: bool) -> list[str]:
    """一条成功的 notify 工具结果对应的摘要行（无可报内容时为空列表）。"""
    if data.get("staged"):
        # 上游：fork 自己的 review 摘要不会回灌，所以无人值守的整合提案必须在这里
        # 露面，否则静默丢失（#105921）。Owlet 的 stage 是**待审写入**：它只在
        # `proposal_staged` 为真时报 message，绝不把"待审"说成"已落地"。
        return [data["message"]] if data.get("proposal_staged") and data.get("message") else []
    message = data.get("message", "")
    target = data.get("target", "") or detail.get("target", "")
    is_skill = detail.get("tool") == "promote_skill"
    if is_skill and "results" in data:
        # 请求过的操作不是"已写入"的证据（审批与原子回滚都可能让它们全没落地）。
        verbs = {
            "create": "created", "patch": "patched", "edit": "rewritten",
            "write_file": "written", "remove_file": "removed", "delete": "deleted",
        }
        results = data.get("results")
        if not data.get("operations_applied") or not isinstance(results, list):
            return []
        lines = []
        for result in results:
            if not isinstance(result, dict) or result.get("success") is not True:
                continue
            verb_key = verbs.get(result.get("action"))
            if verb_key and result.get("name"):
                path = f" ({result['file_path']})" if result.get("file_path") else ""
                lines.append(
                    f"Skill '{result['name']}' {verb_key}{path}")
        return lines
    lower = message.lower()
    if not verbose and (
        "created" in lower or "updated" in lower
        or (is_skill and any(word in lower for word in ("patched", "deleted", "written")))
    ):
        return [message]
    if not is_skill and not target:
        return []
    label_key = "skill" if is_skill else {"memory": "memory", "user": "user"}.get(target)
    label = _LABELS[label_key] if label_key else target
    if verbose:
        if is_skill:
            return [message] if message else [f"Skill {detail.get('action', '')}"]
        return _verbose_memory_lines(label, detail)
    hit = any(k in lower for k in ("added", "replaced", "removed", "applied")) or (
        target and "add" in lower
    )
    return [f"{label} updated"] if hit else []


def summarize_background_review_actions(
    review_messages: list[dict], prior_snapshot: list[dict], notification_mode: str = "on",
) -> list[str]:
    """一次后台 review 的人面向动作摘要（上游同名函数逐行）。

    只报 review agent 消息里**成功**的 memory / skill 工具结果，并跳过
    ``prior_snapshot`` 里已有的 tool message（继承来的结果不算新工作）。
    ``notification_mode``：``off`` → 空；``on`` → 泛化一句；``verbose`` → 内容预览。
    """
    mode = str(notification_mode or "on").lower()
    if mode == "off":
        return []
    verbose = mode == "verbose"
    existing_tool_call_ids, existing_tool_contents = _prior_tool_keys(prior_snapshot)
    all_tool_call_ids, call_details = _collect_review_call_details(review_messages)
    actions: list[str] = []
    for msg in _tool_messages(review_messages):
        tcid = msg.get("tool_call_id")
        if tcid:
            if tcid in existing_tool_call_ids or (all_tool_call_ids and tcid not in call_details):
                continue
        elif isinstance(msg.get("content"), str) and msg["content"] in existing_tool_contents:
            continue
        try:
            data = json.loads(msg.get("content", "{}"))
        except (json.JSONDecodeError, TypeError):
            continue
        # 包装型 MCP 服务器可能返回顶层 list/scalar；只有 dict 载荷带 success/_change。
        if not isinstance(data, dict) or not data.get("success"):
            continue
        actions.extend(_action_lines(data, call_details.get(tcid) or {}, verbose))
    return actions


# --------------------------------------------------------------------------------------
# Owlet 侧的信封合成（纯函数：把已提交的记录变成上游形状的消息列表）
# --------------------------------------------------------------------------------------


def _tool_target(record: Any) -> str:
    """与 markdown 权威面同一句话的分拣：规则只在 `markdown_store.target_for` 一处。"""
    return target_for(record.kind.value, record.tier.value)


def _call_arguments(record: Any) -> dict[str, Any]:
    """一条已提交记录 → 等价的 ``memory`` 工具调用参数（供 verbose 预览）。"""
    if record.status.value == "invalidated":
        return {"action": "remove", "target": _tool_target(record), "old_text": record.content}
    action = "add" if record.version == 1 else "replace"
    return {"action": action, "target": _tool_target(record), "content": record.content}


def review_messages_for(written: Sequence[Any]) -> list[dict[str, Any]]:
    """把一次 job 的已提交记录渲染成上游 review 消息形状。

    每条记录一对消息：assistant 的 ``tool_calls``（参数 = 等价 memory 调用）与
    对应的 tool 结果（Owlet 信封映射见模块 docstring）。``success`` 只对
    **已提交**记录为真——待审与降级的写入根本走不到这里，这正是"staged 未落地
    绝不报成已落地"的结构保证。
    """
    messages: list[dict[str, Any]] = []
    for index, record in enumerate(written):
        call_id = f"memory-{record.id}-{index}"
        target = _tool_target(record)
        arguments = _call_arguments(record)
        messages.append({
            "role": "assistant",
            "tool_calls": [{
                "id": call_id,
                "function": {"name": "memory",
                             "arguments": json.dumps(arguments, ensure_ascii=False)},
            }],
        })
        result = {
            "success": True,
            "target": target,
            "message": (
                "Entry removed." if record.status.value == "invalidated"
                else "Entry added." if record.version == 1
                else "Entry replaced."
            ),
        }
        messages.append({
            "role": "tool", "tool_call_id": call_id,
            "content": json.dumps(result, ensure_ascii=False),
        })
    return messages


__all__ = ["review_messages_for", "summarize_background_review_actions"]
