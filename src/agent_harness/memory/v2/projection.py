"""#298 / MEM-V2-2：交给记忆模型的**安全投影**（R2 / AC9）。

`formation.py` 管模型**怎么答**，本模块管模型**看得到什么**。两件事都必须是运行时
的确定行为：R7 要求秘密策略是模型之外的边界，AC9 把同一条要求延伸到了输入侧
（"no secret reaches model input"）。

纯函数：输入是本次 run 的事件 + 更早的历史 + 检索层已经排好序的相似记忆，输出是
`FormationInput`。不调模型、不读时钟、不写盘、不碰 `ArtifactStore`。

# R2 是排除性约束，所以实现是"重建"而不是"裁剪"

§5.2.2 列的是**必须缺席**的东西：raw large tool output、artifact content、credentials、
hidden reasoning、unrestricted full history。这类约束用"从事件 dict 里删几个 key"来满足
是错的——`SessionEvent.data` 会长出新 key，裁剪式实现会静默放行新字段。所以这里从事件里
**只读**它需要的四类东西，其余一律不进入对象图：

| 通道 | 来源 | 形状 |
| --- | --- | --- |
| `current_run` | 本次 run 的 `user/message` / `model/completed` 文本 | `{role, text}` |
| `earlier_messages` | history 里**最近 8 条**同类消息 | `{role, text}` |
| `tool_calls` | `tool/call` 的**工具名** + `tool/result` 的**状态 / 消息 / artifact_ref** | `{name, status, summary, artifacts}` |
| `similar_memories` | 检索层的 `MemoryRecordV2`，只取 4 个字段 | `{memory_id, kind, scope, content}` |

**刻意不投影的东西**（每一条都对应一个测试）：工具 `args`（模型自己写的，最容易夹带
`api_key=`）、`ToolResult.data`（结构化的原始输出，未外置也可能上千字符）、`artifact/*`
事件的体积与 mime、`reasoning/*`、以及全部事件信封字段（`event_id` / `seq` / `session_id`）
与记录身份字段（`tenant_id` / `user_id`）。最后两类既是噪声又是泄漏面——模型不需要
"这条记忆属于谁"才能判断该不该形成记忆。

# 凭证：从命中点整段砍，而不是替换命中片段

`policy.cut_at_secret` 决定切在哪儿（理由写在它那里：多行私钥的正文不在匹配范围内，
span 级替换会留下可用的残片）。这里只补两条顺序约束，两条都有判别式用例：

1. **先切凭证、后截断**。反过来会把 key 的**前缀**（`sk-`）当成普通长文本截进 prompt
   ——那时扫描器已经看不到完整前缀，帮不上忙。
2. 所有字符串字段走两个入口中的一个，**没有第三个**：散文走 `_safe_text`（切凭证 + 截断），
   标识符（artifact ref / memory id）走 `_safe_identifier`（切凭证 + **不截断**——截断会把 id
   变成"看着像名字、引用不到东西"的假引用）。逐字段各写一遍必然漏掉一个，而"漏掉的那个"
   正好是新增字段时的默认结局。

# 边界：为什么更早消息取尾部、相似记忆取头部

历史是时间序，越近越相关 ⇒ 取最后 8 条；相似记忆的顺序由检索层按相关性排好，投影层
**不重排** ⇒ 取前 10 条。把两处"统一"成同一种取法，一定会让其中一处丢掉该进模型的内容。

# 已知缺口（登记 ADR-0043）

- 当前 run 的条数**不限**：R2 没给上限，run 本身有限，外层边界是 R10 的 32k 输入 token
  预算（T5 执行）。这里加一条"顺手"的条数上限会砍掉 run 的尾部——恰恰是刚发生、最该
  形成记忆的那几步。
- 图片类内容不在投影范围内（它们以 artifact 形式存在，只投影引用，不读内容）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agent_harness.memory.extractor import _is_runtime_injected
from agent_harness.memory.v2.policy import (
    ToolOutcome,
    cut_at_secret,
    read_tool_result,
    resolve_tool_outcome,
)
from agent_harness.memory.v2.types import MemoryRecordV2
from agent_harness.session import (
    MODEL_COMPLETED,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
    SessionEvent,
)

# --------------------------------------------------------------------------------------
# 预算（R2 的数字 + 两个文本上界）
# --------------------------------------------------------------------------------------

#: R2："at most eight earlier user/assistant messages"。
MAX_EARLIER_MESSAGES = 8

#: R2："at most ten similar active memories"。
MAX_SIMILAR_MEMORIES = 10

#: 单条对话文本的上界。与 V1 `_MAX_EXTRACT_EVENT_CHARS` 同值但**独立设定**：
#: 那个数是给"事件级 clip"用的，这个是给"消息文本"用的，各自可调不会互相拖拽。
MAX_MESSAGE_CHARS = 1000

#: 单条工具摘要 / 工具名 / 标识符的上界。与证据摘录的 300（§6.1）同量级：
#: 它是**摘要**，不是内容。
MAX_TOOL_SUMMARY_CHARS = 300

#: 命中凭证后留下的占位符。不含命中的类型——那是给日志 / 事件的元数据，模型不需要。
SECRET_PLACEHOLDER = "[已移除：含凭证]"

_TRUNCATION_MARKER = "…[truncated]"


# --------------------------------------------------------------------------------------
# 投影结果
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProjectedMessage:
    """一条进模型的对话文本。`role` 取 `"user"` / `"assistant"` 两个值之一。"""

    role: str
    text: str


@dataclass(frozen=True, slots=True)
class ProjectedToolCall:
    """一次工具执行的**名称 / 状态 / 摘要 / artifact 引用**。

    `status` 复用 `policy.ToolOutcome`：`MISSING`＝没有对应的结果事件，
    `UNKNOWN`＝结果事件在但读不出可判定的 `ok`。两者都**不算成功**——R5 的
    "两条独立成功事件"不能靠一条没跑完、或一条形态污染的结果凑出来。
    """

    name: str
    status: ToolOutcome
    summary: str
    artifacts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProjectedMemory:
    """一条相似 active 记忆的**最小可见面**：内容 + 三个标识。

    没有 `tenant_id` / `user_id` / `status` / `payload` / `evidence` / 版本字段：模型判断
    "该不该形成记忆"不需要它们，而多喂一份身份就多一条越界面（R2 末句反过来也成立——
    模型输出不得提供可信身份，输入也不该把身份喂进去）。
    """

    memory_id: str
    kind: str
    scope: str
    content: str


@dataclass(frozen=True, slots=True)
class FormationInput:
    """一次形成 job 可以交给模型的全部输入。"""

    current_run: tuple[ProjectedMessage, ...] = ()
    earlier_messages: tuple[ProjectedMessage, ...] = ()
    tool_calls: tuple[ProjectedToolCall, ...] = ()
    similar_memories: tuple[ProjectedMemory, ...] = ()

    def to_prompt_payload(self) -> dict[str, Any]:
        """序列化成可 JSON 化的字典；AC9 的断言对象就是它的 `json.dumps` 结果。

        键与顺序都固定 ⇒ 同一份输入永远得到同一份载荷（可重放的 prompt）。
        """
        return {
            "current_run": [
                {"role": item.role, "text": item.text} for item in self.current_run
            ],
            "earlier_messages": [
                {"role": item.role, "text": item.text}
                for item in self.earlier_messages
            ],
            "tool_calls": [
                {
                    "name": item.name,
                    "status": item.status.value,
                    "summary": item.summary,
                    "artifacts": list(item.artifacts),
                }
                for item in self.tool_calls
            ],
            "similar_memories": [
                {
                    "memory_id": item.memory_id,
                    "kind": item.kind,
                    "scope": item.scope,
                    "content": item.content,
                }
                for item in self.similar_memories
            ],
        }


# --------------------------------------------------------------------------------------
# 文本净化（所有字符串字段的唯一入口）
# --------------------------------------------------------------------------------------


def _safe_text(raw: object, limit: int) -> str:
    """把一个任意形状的字段变成安全文本：非 `str` 空串、含凭证砍掉、超长截断。

    先切凭证再截断是硬要求（见模块 docstring 第 1 条）——反过来会让被截断的 key 前缀
    绕过扫描器。非 `str` 退化成空串而不是抛错：恢复链上的一行坏数据只该损失该字段，
    不该 brick 整个 job。
    """
    if not isinstance(raw, str) or not raw:
        return ""
    text, hit = cut_at_secret(raw)
    if hit is not None:
        return text + SECRET_PLACEHOLDER
    if len(text) > limit:
        return text[:limit] + _TRUNCATION_MARKER
    return text


def _safe_identifier(raw: object) -> str:
    """标识符（artifact ref / memory id）：**不截断**，含凭证则整体作废。

    与 `_safe_text` 的区别是刻意的：给一个被截断的 id 加上截断标记，等于递给模型一个
    **看着像名字但引用不到东西**的假引用——比不引用更坏。含凭证的 id 同样不能用
    （那种 id 本身就不该存在），返回空串让调用方整条丢弃。
    """
    if not isinstance(raw, str) or not raw:
        return ""
    text, hit = cut_at_secret(raw)
    return "" if hit is not None else text


def _data_of(event: SessionEvent) -> Mapping[str, Any]:
    """事件的 data；形状污染时退化成空映射（与 `_safe_text` 同一容错口径）。"""
    return event.data if isinstance(event.data, Mapping) else {}


# --------------------------------------------------------------------------------------
# 逐通道投影
# --------------------------------------------------------------------------------------


def _project_messages(events: Iterable[SessionEvent]) -> list[ProjectedMessage]:
    """把事件序列投影成对话文本；非消息事件与运行期注入的样板一律跳过。

    跳过注入的样板与 `resolve_evidence_source` 同一口径（`injected_by` 非空）：那是运行时
    的脚手架，不是用户说的话——把它当对话喂回去，等于请模型从样板里形成记忆。
    """
    messages: list[ProjectedMessage] = []
    for event in events:
        if event.type == USER_MESSAGE:
            if _is_runtime_injected(event):
                continue
            role = "user"
        elif event.type == MODEL_COMPLETED:
            role = "assistant"
        else:
            continue
        messages.append(
            ProjectedMessage(
                role=role,
                text=_safe_text(_data_of(event).get("content"), MAX_MESSAGE_CHARS),
            )
        )
    return messages


def _project_tool_calls(events: Iterable[SessionEvent]) -> list[ProjectedToolCall]:
    """按 `tool_call_id` 把 `tool/call` 与 `tool/result` 配对。

    顺序取 `tool/call` 的出现顺序（那是模型发起调用的顺序）。结果事件先于调用事件到达
    也能配对——配对是按 id 建的索引，不依赖到达顺序。
    """
    results: dict[str, SessionEvent] = {}
    for event in events:
        if event.type != TOOL_RESULT:
            continue
        tool_call_id = _data_of(event).get("tool_call_id")
        if isinstance(tool_call_id, str) and tool_call_id and tool_call_id not in results:
            results[tool_call_id] = event

    projected: list[ProjectedToolCall] = []
    for event in events:
        if event.type != TOOL_CALL:
            continue
        tool_call_id = _data_of(event).get("tool_call_id")
        # 配对键缺失 / 形状非法 ⇒ 跳过该条。合成一个占位 id 会让两条不同的调用撞到
        # 同一个"结果"，把状态张冠李戴。
        if not isinstance(tool_call_id, str) or not tool_call_id:
            continue
        result = results.get(tool_call_id)
        view = read_tool_result(result) if result is not None else None
        artifact_ref = "" if view is None else _safe_identifier(view.artifact_ref)
        projected.append(
            ProjectedToolCall(
                name=_safe_text(
                    _data_of(event).get("tool_name"), MAX_TOOL_SUMMARY_CHARS
                ),
                # 结果事件在但解析不出 ⇒ UNKNOWN（`resolve_tool_outcome` 的判据）。
                status=(
                    ToolOutcome.MISSING
                    if result is None
                    else resolve_tool_outcome(result)
                ),
                summary=_safe_text(
                    view.message if view is not None else "", MAX_TOOL_SUMMARY_CHARS
                ),
                artifacts=(artifact_ref,) if artifact_ref else (),
            )
        )
    return projected


def _project_memories(records: Iterable[MemoryRecordV2]) -> list[ProjectedMemory]:
    """取**前** `MAX_SIMILAR_MEMORIES` 条：调用方的顺序就是检索层的相关性顺序。"""
    projected: list[ProjectedMemory] = []
    for record in records:
        if len(projected) >= MAX_SIMILAR_MEMORIES:
            break
        projected.append(
            ProjectedMemory(
                memory_id=_safe_identifier(record.id),
                kind=record.kind.value,
                scope=record.scope.value,
                # 存档里已有的脏数据（写入侧本该拒掉，但投影是独立的第二道）也要过扫描器。
                content=_safe_text(record.content, MAX_MESSAGE_CHARS),
            )
        )
    return projected


def build_formation_input(
    run_events: Sequence[SessionEvent],
    *,
    history: Sequence[SessionEvent] = (),
    similar_memories: Iterable[MemoryRecordV2] = (),
) -> FormationInput:
    """组装一次形成 job 的模型输入（R2）。

    - `run_events`：**本次 run** 的事件（调用方用 `Session.since(run_start)` 切）；
    - `history`：本 run **之前**的历史事件；只取其中最近 8 条 user/assistant 消息；
    - `similar_memories`：检索层给出的相似 active 记忆；只取前 10 条。

    三个入参互不重叠是**调用方的责任**：`history` 里若混入 run 内的事件，同一句话会既
    出现在 `current_run` 又出现在 `earlier_messages`。去重需要投影层自己判 run 边界，而
    run 边界已经由调用方的切片表达（在这里再判一次就是第二套 run 边界判定）。
    """
    return FormationInput(
        current_run=tuple(_project_messages(run_events)),
        earlier_messages=tuple(_project_messages(history)[-MAX_EARLIER_MESSAGES:]),
        tool_calls=tuple(_project_tool_calls(run_events)),
        similar_memories=tuple(_project_memories(similar_memories)),
    )
