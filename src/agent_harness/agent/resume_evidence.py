"""Stuck 暂停的恢复依据：**现场观测**出来的三类证据（ADR-0048 D7/D8）。

`02 §5.3` 的恢复前置是三者其一：**相关** steer 事件、**比暂停快照更新**的工作区/环境
revision、**比暂停快照更新**的 policy/profile 版本。本模块是这三件事**唯一**的观测点
（`04 §9.1` 同一种纪律：判定只写一遍）：暂停侧把观测值写进 `run/paused.data.stuck`，
恢复侧用**同一份函数**在请求这一刻重算再比较。

三条口径（都是不可伪造性的来源）：

1. **观测，不是声明**——用户说"我改了环境"不构成证据（`#317` Must Not Do）；只有本模块算
   出来的值算数，请求体里也没有任何位置能塞进一个依据值。
2. **摘要不是全序**——"更新"的机械口径只能是**不等于快照**（ADR-0048 残余 1）。两侧都由
   服务端计算，客户端无从挑选。
3. **fail-closed**——观测不到（目录不存在 / 无策略输入 / 快照缺值）⇒ 该依据不可用，
   恢复被 409 拒绝，而不是放行。

工作区清单的代价与截断（ADR-0048 残余 2）：一次全树 walk（相对路径 + 尺寸 + mtime_ns），
条目数超过上限就停止采集并把"已截断"折进摘要——截断只会让"变过"更难被观测到
（fail-closed），不会伪造出"变过"。
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_harness.session.event import RUN_PAUSED, STEER_REQUESTED, SessionEvent
from agent_harness.tooling.contract import PermissionPolicy

#: 单次 walk 采集的最大条目数（防超大目录把暂停拖住；见模块 docstring 的口径 3）。
MAX_REVISION_ENTRIES = 20000
_DIGEST_CHARS = 16
_FIELD = "\x1f"

#: 策略面五维（`policy_version_of` 的输入集）。暂停快照**逐维**记下它们，恢复侧据此
#: 还原——摘要与逐维值必须同生共死，否则"两侧算的是同一套输入"只是句口号（`#317`
#: T9 审查 P1）。
POLICY_INPUT_FIELDS: tuple[str, ...] = (
    "permission_mode",
    "model",
    "agent_profile",
    "reasoning_effort",
    "context_providers",
)


def policy_inputs(
    *,
    permission_mode: str | None = None,
    model: str | None = None,
    agent_profile: str | None = None,
    reasoning_effort: str | None = None,
    context_providers: Sequence[str] | None = None,
) -> dict[str, Any]:
    """生效策略面的**规范化输入**（摘要与"逐维还原"共用这一份口径）。

    五维各归一化一次（权限档取枚举值、providers 排序），返回的 dict 就是
    `run/paused.data.stuck.policy_inputs` 的内容；摘要 = 它的哈希。两者同源 ⇒ 拿快照里的
    `policy_inputs` 必能重算出快照里记着的那个 `policy_version`，恢复侧据此还原
    （ADR-0048 D6/D8）。
    """
    return {
        "permission_mode": (
            None if permission_mode is None
            else getattr(permission_mode, "value", permission_mode)
        ),
        "model": model,
        "agent_profile": agent_profile,
        "reasoning_effort": reasoning_effort,
        # `None` 与 `[]` 是**两种不同的策略面**：前者是"没声明 ⇒ 装配全部 wired provider"
        # （`assembly` 的口径），后者是"显式零 provider"。归一化成一个值会让"从全量改成
        # 零 provider"这种真实变更看上去没变（fail-closed 的死角），而恢复侧把 `[]` 还原
        # 回去还会把暂停腿的 provider 全部丢光（T9 审查 P2：`None` 被还原成 `[]` ⇒ 恢复腿
        # 零 provider）。所以这里记**原值**，摘要按原值算（`null` 与 `[]` 天然不同）。
        "context_providers": (
            None if context_providers is None else sorted(context_providers)
        ),
    }


def digest_policy_inputs(inputs: Mapping[str, Any]) -> str:
    """策略面输入 ⇒ 摘要（本模块是唯一算法；口径见 `policy_version_of`）。"""
    payload = json.dumps(
        dict(inputs), sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]


def recorded_policy_inputs(stuck: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """暂停载荷里的逐维策略面——**可用**才返回，否则 `None`（该依据 fail-closed）。

    可用 = 形状合法 **且** 逐维值能重算出同一份载荷里记着的 `policy_version`：

    - 形状：五个维度都在，前四维 `str | None`，`context_providers` 为 `list[str] | None`
      （畸形 JSONL 里的 list / dict / bool 一律算不可用——不是 500，是"这份快照不能用作
      依据"）；
    - 同源：`digest_policy_inputs(值) == policy_version`。

    第二条挡两类快照：只记了摘要的旧载荷，与逐维值 / 摘要对不上的载荷——两类都还原不回来
    （机制、代价与残余见 ADR-0048 D6 / 残余 11）。

    判据只有这一份：`run_budget.stuck_resume_evidence` 拿它判"能不能用"，恢复侧拿它决定
    "能不能还原"。两边各写一遍就是下一个漂移点。
    """
    if not isinstance(stuck, Mapping):
        return None
    digest = stuck.get("policy_version")
    if not isinstance(digest, str) or not digest:
        return None
    recorded = stuck.get("policy_inputs")
    if not isinstance(recorded, Mapping):
        return None
    inputs: dict[str, Any] = {}
    for field in POLICY_INPUT_FIELDS:
        if field not in recorded:
            return None
        value = recorded[field]
        if field == "context_providers":
            if value is None:
                inputs[field] = None
            elif isinstance(value, (list, tuple)) and all(
                isinstance(item, str) for item in value
            ):
                inputs[field] = list(value)
            else:
                return None
        elif value is not None and not isinstance(value, str):
            return None
        else:
            inputs[field] = value
    if digest_policy_inputs(inputs) != digest:
        return None
    return inputs


def recorded_environment_revision(stuck: Mapping[str, Any] | None) -> str | None:
    """暂停载荷里的环境修订——**可用**才返回，否则 `None`（该依据 fail-closed）。

    可用 = 非空字符串。畸形 JSONL 里的 list / dict / 数字一律算"这份快照没有这一格"：
    与 `recorded_policy_inputs` 的形状规则同因（不可用的快照不是 500，是"不能用作依据"），
    而"存在即已变"会让任何畸形值都无条件放行 `environment_change`（`#317` 三轮审查 P3）。

    与 `recorded_policy_inputs` 一样只有这一份：`run_budget` 的判据与依据清单都调它。
    """
    if not isinstance(stuck, Mapping):
        return None
    value = stuck.get("environment_revision")
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True)
class ResumeEvidence:
    """恢复请求这一刻**观测到**的三类依据（`None` = 观测不到 ⇒ 该依据不可用）。"""

    #: 暂停之后、可作用于该 run 的 steer 请求的 seq（`None` = 没有这样的 steer）。
    relevant_steer_seq: int | None = None
    environment_revision: str | None = None
    policy_version: str | None = None
    #: 本次观测的生效策略面**逐维值**（`policy_version` 的输入）。恢复侧把它交给
    #: `model_switch.restore_policy_inputs` 还原暂停时那一套（`#317` T9 审查 P1）。
    policy_inputs: Mapping[str, Any] | None = None

    def as_projection(self) -> dict[str, Any]:
        """客户端 / durable 投影：**不含** `policy_inputs`。

        逐维策略面是内部的"还原来源"，它已经在 `run/paused.data.stuck` 里（runtime 从
        `StuckEvidencePort.policy_inputs` 直接写进去）。再往别的投影里复制一份就是同一个
        事实的第二条读取路径——`#317` T9 审查 P2 点过这个形状（投影与原始载荷两处读）。
        """
        return {
            "relevant_steer_seq": self.relevant_steer_seq,
            "environment_revision": self.environment_revision,
            "policy_version": self.policy_version,
        }


@dataclass(frozen=True)
class StuckEvidencePort:
    """注入 Runtime 的只读证据端口：runtime 只在**暂停那一刻**读一次（`current()`）。

    `policy_version` 在装配时算好（一次执行期间策略不变）；`workspace` 只存路径，
    环境 revision 在 `current()` 当场 walk——"暂停那一刻的环境"才是快照要记的东西。
    """

    workspace: Path | None = None
    policy_version: str | None = None
    #: `policy_version` 的输入（同一次计算的两个投影，见 `evidence_port`）。
    policy_inputs: Mapping[str, Any] | None = None

    def current(self) -> ResumeEvidence:
        return ResumeEvidence(
            environment_revision=environment_revision(self.workspace),
            policy_version=self.policy_version,
            policy_inputs=self.policy_inputs,
        )


def environment_revision(root: Path | str | None) -> str | None:
    """工作区/环境 revision = 树清单摘要（相对路径 + 类型 + 尺寸 + mtime_ns）。

    读不到（未给路径 / 不是目录 / walk 中途 I/O 错）⇒ `None`。符号链接不跟随
    （`follow_symlinks=False`），既避免环，也让"链接目标变了"不算这次工作区变化。
    """
    if root is None:
        return None
    base = Path(root)
    if not base.is_dir():
        return None
    entries: list[str] = []
    truncated = False
    pending: list[str] = [str(base)]
    try:
        while pending and not truncated:
            current = pending.pop()
            with os.scandir(current) as iterator:
                for item in iterator:
                    if len(entries) >= MAX_REVISION_ENTRIES:
                        truncated = True
                        break
                    try:
                        relative = os.path.relpath(item.path, base)
                        if item.is_dir(follow_symlinks=False):
                            entries.append(f"d{_FIELD}{relative}")
                            pending.append(item.path)
                        else:
                            info = item.stat(follow_symlinks=False)
                            entries.append(
                                f"f{_FIELD}{relative}{_FIELD}"
                                f"{info.st_size}{_FIELD}{info.st_mtime_ns}"
                            )
                    except OSError:
                        continue
    except OSError:
        return None
    entries.sort()
    digest = hashlib.sha256()
    for line in entries:
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    if truncated:
        digest.update(b"<truncated>")
    return "sha256:" + digest.hexdigest()[:_DIGEST_CHARS]


def policy_version_of(
    *,
    permission_mode: str | None = None,
    model: str | None = None,
    agent_profile: str | None = None,
    reasoning_effort: str | None = None,
    context_providers: Sequence[str] | None = None,
) -> str:
    """生效策略 / profile 版本的摘要（本模块是唯一算法）。

    输入集 = 一次执行**生效的**策略面：权限档、模型（含 reasoning effort）、agent
    profile、context providers。同一份输入恒得同一个值；任何一项变了就是另一次策略
    ⇒ 与暂停快照不同 ⇒ `policy_change` 成立。

    `context_providers` **排序后**入摘要：选择器的语义是"名字集合筛选"
    （`assembly._select_context_providers`），请求里的书写顺序不是策略。

    **不含 `auto_approve`**（它是审批路由的声明，不是运行时比较的策略面；而且创建路径
    只拿得到请求值、恢复路径只拿得到事件派生值，未显式声明时两者不同名——把它算进摘要
    会让同一套策略算出一个"变了"，那是一次**假**的 `policy_change`，即 fail-open；
    代价与残余见 ADR-0048 §4）。

    **不含 ceiling 类**（local fuse 的值与来源、run 作用域四维）。它们是**预算**不是策略：
    规格明确规定 stuck 暂停"抬高预算不是有效依据"（ADR-0048 D7 据此把 `budget_increase`
    判 409）。ceiling 一旦进摘要，恢复请求只要顺手把 `local_max_agent_turns` 抬一格就能
    **自己造出**一个 `policy_change`——同一个漏洞换一扇侧门。fuse 仍照常参与 `limits`
    快照与 headroom 判定，只是不构成"策略变了"。

    **版本不是计数器**（ADR-0048 残余 1）：这里没有"第几版"这样的单调量，只有"是不是
    同一套策略"。所以"更新"只能判"不等于"，且两侧都必须由服务端计算。

    **摘要与逐维值同生共死**：暂停侧写进快照的不止这个摘要，还有 `policy_inputs`
    （同一份 `policy_inputs` 的哈希就是这里的返回值）。恢复侧先把快照里的逐维值还原成
    自己这副 amend（`session/model_switch.restore_policy_inputs`）再算摘要——"同一套
    输入"于是有了机械保证，而不是靠两侧各自"记得传全字段"。
    """
    return digest_policy_inputs(
        policy_inputs(
            permission_mode=permission_mode,
            model=model,
            agent_profile=agent_profile,
            reasoning_effort=reasoning_effort,
            context_providers=context_providers,
        )
    )


def evidence_port(
    *,
    workspace: Path | None,
    permission_mode: str | None = None,
    model: str | None = None,
    agent_profile: str | None = None,
    reasoning_effort: str | None = None,
    context_providers: Sequence[str] | None = None,
) -> StuckEvidencePort:
    """构造观测端口：**唯一**构造点（创建 / 续聊 / CLI 三条链路都走它）。

    为什么必须是唯一一处：暂停侧写进 `run/paused.data.stuck` 的 policy 值与恢复侧的
    重算值都由服务端算（不可伪造性的来源），而"两侧算的是同一套输入"这件事**只能**靠
    "只有一处组装输入"来保证——某一侧少传一项（例如 CLI 建 run 时不知道 session 层的
    amend）、或把 `None` 与"运行时默认档"归一化成两种写法，就会得出一个**假**的
    `policy_change`（其实没变，却被算成变了 ⇒ 无依据也放行 = fail-open）。

    档位取**生效值**而不是"声明值"：运行时未声明时按 `WORKSPACE_WRITE` 跑
    （`assembly.build_runtime` 的默认参数），恢复侧也从同一常量出发重算
    （`session/service.py._stuck_evidence_port` 的调用点）。

    构造点唯一**不等于**两侧喂进来的字段集合相同（CLI 的 resume 一个策略字段都不声明），
    所以"两侧同源"靠的是**记下来 + 还原**：暂停侧把这份输入逐维写进快照，恢复侧先还原再算
    （`model_switch.restore_policy_inputs`）。机制与理由见 ADR-0048 D6/D8。
    """
    inputs = policy_inputs(
        permission_mode=permission_mode,
        model=model,
        agent_profile=agent_profile,
        reasoning_effort=reasoning_effort,
        context_providers=context_providers,
    )
    return StuckEvidencePort(
        workspace=workspace,
        policy_version=digest_policy_inputs(inputs),
        policy_inputs=inputs,
    )


def delegated_child_evidence_port(
    spec_name: str, *, workspace: Path | str | None = None,
) -> StuckEvidencePort:
    """委派子 run 的**自身**生效策略面端口（ADR-0048 残余 15 / #370）——唯一口径。

    子 run 暂停时记的是 child 自己的策略面，四个答案只在这里写一遍；恢复侧
    （`service.resume_and_launch`：`_effective_permission_mode` 派生档位 + #372 强制
    `agent_profile` = 子 spec + 快照还原四维）对 child 会话**必然重算出同一套值**：

    - `permission_mode` = 默认档：child 会话事件流没有 permission 声明，恢复侧派生
      None 后回落**同一个**默认档。父级 launch 的档位经 executor 闭包对 child 生效
      （决策 11 权限传递），但那是父的策略面流经执行器——child 没声明过它，记父档
      会让恢复侧重算（只能算出默认档）必然对不上："什么都没变"也算变了 = fail-open。
    - `model` / `reasoning_effort` / `context_providers` = None：子层无独立声明
      （模型链继承父级，Factory 决策 14；委派不写 `model/changed`，那是 fork 的
      `inherit_parent_model` 语义）。省略恢复请求字段不算策略变更；显式声明任一维
      才构成 child 自己的 `policy_change`。
    - `agent_profile` = spec.name：child 授权由它自己的 AgentSpec 决定（#372 的
      恢复入口按 session/started 的 agent_id 强制同一档位）。

    `workspace`（#608，残余 15 的环境半）：委派方传**父会话的 cwd 锚**
    （`provider._parent_cwd()`）——与写进子 SESSION_STARTED 的 cwd 字段、恢复侧
    `Path(persisted_cwd)` 读回的是**同一事件字段**（child 与父共用同一棵
    Session-scoped workspace，spec 10 §9），两侧同源是构造保证不是数值巧合。
    父无锚 ⇒ None ⇒ 环境格如实缺席，`environment_change` 判据 fail-closed
    （无快照可比 ⇒ 409，判据零改动）。
    """
    return evidence_port(
        workspace=Path(workspace) if workspace is not None else None,
        permission_mode=PermissionPolicy.WORKSPACE_WRITE,
        model=None,
        agent_profile=spec_name,
        reasoning_effort=None,
        context_providers=None,
    )


def steer_applies_to_run(steer_run_id: str | None, run_id: str | None) -> bool:
    """这条 steer 是否**可作用于**这个 run（`#317`：运行时与恢复判定共用的唯一口径）。

    口径就是运行时的 `_applicable_steers`：**只认 run_id 相同**。`run_id=None`
    （"会话级"）不是"任一 run 都算"——运行时把它当陈旧 steer 丢弃（落 warning），
    它只会在 run 走到**终态**时被当普通输入投递；而 stuck 暂停不是终态（`03 §3.4`），
    所以那种 steer 在这一次恢复里永远到不了模型 ⇒ 不能算恢复依据（fail-closed：
    依据必须是"确实会被这次执行采用的新输入"）。

    抽出来是因为**两侧各写一遍口径已经漂过一次**：恢复判定曾接受 `run_id=None`
    （ADR-0048 D7 的旧措辞还写着"运行时按同一口径认它"），而运行时从来不认——
    于是"没有新输入"的恢复被放行（T9 审查 P2）。现在两处都调这个函数。
    """
    return run_id is not None and steer_run_id == run_id


def paused_policy_inputs(
    events: Iterable[SessionEvent], *, run_id: str | None,
) -> dict[str, Any] | None:
    """取回那个暂停 run 记下的**逐维策略面**（`run/paused.data.stuck.policy_inputs`）。

    返回的是**值**而不是摘要：恢复侧要拿它重建 amend（摘要只能比较，还原不回来）。
    只认同 run 的 `run/paused`，且**取最后一条**——与 `run_budget.latest_paused_run` 给
    判据的基线必须是同一条（同一个 run 可以暂停多次；取错了就是拿两个不同快照相减，
    ADR-0048 D6）。

    不可用（非 stuck 暂停 / 只记了摘要的旧快照 / 逐维值与摘要对不上）⇒ `None`：
    恢复侧没有可还原的东西；判据侧也按同一口径拒绝 `policy_change`
    （`stuck_resume_evidence` 的 fail-closed 一侧），两侧不会一个放行一个不还原。
    """
    latest: SessionEvent | None = None
    for event in events:
        if event.type != RUN_PAUSED:
            continue
        if run_id is not None and event.run_id != run_id:
            continue
        latest = event
    if latest is None:
        return None
    stuck = (latest.data or {}).get("stuck")
    return recorded_policy_inputs(stuck if isinstance(stuck, Mapping) else None)


def relevant_steer_seq(
    events: Iterable[SessionEvent],
    *,
    run_id: str | None,
    pause_seq: int,
) -> int | None:
    """暂停之后**可作用于该 run** 的 steer 请求的 seq（最晚一条）。

    "相关"的机械口径（ADR-0048 D7）：`steer/requested` 的 `seq > pause_seq`，且
    `steer_applies_to_run` 认它（**run_id 相同**，与运行时 `_applicable_steers`
    同一口径、同一函数）。内容非空与否**不**在这里判——steer 的正文属于用户输入，
    不是本函数的判据。
    """
    latest: int | None = None
    for event in events:
        if event.type != STEER_REQUESTED or event.seq <= pause_seq:
            continue
        if not steer_applies_to_run(event.run_id, run_id):
            continue
        if latest is None or event.seq > latest:
            latest = event.seq
    return latest
