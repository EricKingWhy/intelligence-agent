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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_harness.session.event import STEER_REQUESTED, SessionEvent

#: 单次 walk 采集的最大条目数（防超大目录把暂停拖住；见模块 docstring 的口径 3）。
MAX_REVISION_ENTRIES = 20000
_DIGEST_CHARS = 16
_FIELD = "\x1f"


@dataclass(frozen=True)
class ResumeEvidence:
    """恢复请求这一刻**观测到**的三类依据（`None` = 观测不到 ⇒ 该依据不可用）。"""

    #: 暂停之后、可作用于该 run 的 steer 请求的 seq（`None` = 没有这样的 steer）。
    relevant_steer_seq: int | None = None
    environment_revision: str | None = None
    policy_version: str | None = None

    def as_projection(self) -> dict[str, Any]:
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

    def current(self) -> ResumeEvidence:
        return ResumeEvidence(
            environment_revision=environment_revision(self.workspace),
            policy_version=self.policy_version,
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
    """
    payload = json.dumps(
        {
            "permission_mode": permission_mode,
            "model": model,
            "agent_profile": agent_profile,
            "reasoning_effort": reasoning_effort,
            "context_providers": sorted(context_providers or ()),
        },
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]


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
    """
    return StuckEvidencePort(
        workspace=workspace,
        policy_version=policy_version_of(
            permission_mode=(
                None if permission_mode is None
                else getattr(permission_mode, "value", permission_mode)
            ),
            model=model,
            agent_profile=agent_profile,
            reasoning_effort=reasoning_effort,
            context_providers=context_providers,
        ),
    )


def relevant_steer_seq(
    events: Iterable[SessionEvent],
    *,
    run_id: str | None,
    pause_seq: int,
) -> int | None:
    """暂停之后**可作用于该 run** 的 steer 请求的 seq（最晚一条）。

    "相关"的机械口径（ADR-0048 D7）：`steer/requested` 的 `seq > pause_seq`，且它的
    `run_id` 相同或为 `None`（会话级 steer，运行时按 `_applicable_steers` 的既有口径
    认它）。内容非空与否**不**在这里判——steer 的正文属于用户输入，不是本函数的判据。
    """
    latest: int | None = None
    for event in events:
        if event.type != STEER_REQUESTED or event.seq <= pause_seq:
            continue
        if event.run_id is not None and event.run_id != run_id:
            continue
        if latest is None or event.seq > latest:
            latest = event.seq
    return latest
