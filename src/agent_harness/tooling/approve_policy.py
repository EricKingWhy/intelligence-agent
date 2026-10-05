"""持久审批规则（approve_policy）：项目级「以后都允许」授权配置（#684 Phase 1）。

规则以人类可读 JSON 存项目根 ``.agent-harness/approve-policy.json``，可提交、
可 diff、可 code review；删文件即清空。本模块只负责**规则存储与命中判定**；
命中后的放行与安装触发在 ``ToolExecutor`` 的审批闸门里（Tool 只有一条执行路径，
不变量 #7），不另开隐藏授权入口。

对标（PORT DESIGN）：

- **Claude Code ``settings.json``** —— 抄「项目级配置文件」这一形态：项目根一个
  可提交的 JSON、规则即字符串列表、人类可读可手改。本票 v1 只做项目共享级
  （``.agent-harness/approve-policy.json``），用户级 / 个人级留扩展位。
- **Codex ``acceptWithExecpolicyAmendment``** —— 抄「精确安装」语义：安装到盘上
  的规则必须来自 Runtime 从已校验参数派生的精确值（用户只能选粒度，不能手写规则
  文本）；渲染后含换行符的命令级规则**拒绝安装**（防换行走私）。
- **ZCode ``exactCommand`` / ``commandPrefix``** —— 抄两档粒度：``EXACT``（完整
  身份键，含 args hash）与 ``COMMAND``（tool + command，不含 args hash）。

安全红线（与 F21 / F22 同源，``docs/agents/684-design-proposal.md`` §8）：

1. 只有用户主动选「以后都允许」才安装规则（入口唯一，禁止静默创建）；
2. 只有 exact / command 两档，纯 equality 匹配，绝不做前缀 / 通配 / 文本相似
   （F22：系统不猜）；
3. 命中时重验工具当前 ``permission == permission_at_approval``（防提权）。

持久规则是**配置**，不是会话运行时状态：不并入 ``permission_policy``，也不受
#526 会话授权的 1800s TTL 约束（ADR-0041 D6 三概念分离）。

**项目根口径（v1 已知边界）**：v1 以**进程当前工作目录**（``Path.cwd()``）为项目根
——``ToolExecutor`` 回落、CLI / Web 管理面三处逐字一致，保证读写同一份
``.agent-harness/approve-policy.json``。**多 workspace 同进程**场景（同一进程内切换
不同项目根）尚不支持，待后续票；届时项目根必须由调用方显式传入、不再回落 cwd。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from agent_harness.tooling.approval import ApprovalIdentity
from agent_harness.tooling.contract import ToolPermission

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows 无 fcntl
    fcntl = None  # type: ignore[assignment]

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX 无 msvcrt
    msvcrt = None  # type: ignore[assignment]

logger = logging.getLogger("agent_harness.tooling.approve_policy")

#: 项目根下的配置目录 / 文件名（对标 Claude Code ``.claude/settings.json`` 的项目级形态）。
_POLICY_DIRNAME = ".agent-harness"
_POLICY_FILENAME = "approve-policy.json"

#: 命令级规则里出现即拒绝安装的换行字符（Codex 语义：渲染后含换行的前缀不展示，
#: 从源头拒绝比渲染时过滤更难绕过——换行可把一条规则走私成多条 / 改变命令边界）。
_LINE_BREAKS = ("\n", "\r")


class PolicyGranularity(str, Enum):
    """持久 allow 规则的粒度（对标 ZCode ``exactCommand`` / ``commandPrefix``）。

    为什么 str Enum：与 ``PermissionDecision`` / ``PermissionPolicy`` 同理——JSON
    可读、日志可读、序列化无需额外编码。

    为什么只有两档（F22）：多一档（前缀 / 通配 / 相似）就多一处「系统替用户猜」
    的模糊匹配面。用户只能在这两档里显式选，规则内容仍由 Runtime 精确派生。
    """

    EXACT = "exact"  # 完整身份键（含 args hash）
    COMMAND = "command"  # tool + command，不含 args hash


@dataclass(frozen=True)
class ApprovePolicyRule:
    """一条持久 allow 规则（不可变值对象，frozen）。

    - ``id``：规则唯一标识（撤销 / Web 列表按它定位）；
    - ``tool``：工具名（command 档按 tool + key 匹配）；
    - ``key``：exact 档 = ``ApprovalIdentity.key()`` 全串；command 档 = canonical
      （即去掉 args hash 的 command）；
    - ``granularity``：exact / command；
    - ``permission_at_approval``：批准时工具权限，命中时重验（防提权）；
    - ``created_at``：安装时刻（ISO 8601，审计链）。
    """

    id: str
    tool: str
    key: str
    granularity: PolicyGranularity
    permission_at_approval: ToolPermission
    created_at: str

    def to_dict(self) -> dict[str, str]:
        """序列化成落盘 JSON 的一格（枚举取 ``.value``，保持文件人类可读）。"""
        return {
            "id": self.id,
            "tool": self.tool,
            "key": self.key,
            "granularity": self.granularity.value,
            "permission_at_approval": self.permission_at_approval.value,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ApprovePolicyRule:
        """从落盘 JSON 一格还原；字段缺失 / 枚举非法时抛 ``ValueError``（由调用方跳过）。"""
        return cls(
            id=str(data["id"]),
            tool=str(data["tool"]),
            key=str(data["key"]),
            granularity=PolicyGranularity(data["granularity"]),
            permission_at_approval=ToolPermission(data["permission_at_approval"]),
            created_at=str(data["created_at"]),
        )


class ApprovePolicyStore:
    """``project_root/.agent-harness/approve-policy.json`` 的读写与命中判定。

    每次 ``load`` 都现读文件（不缓存）：规则是配置，人工删改 / ``remove`` 后必须
    立刻生效，不依赖进程重启。坏数据 fail-closed——读不动就当成没有规则
    （回落到默认逐调用审批），绝不臆造放行。
    """

    def __init__(self, project_root: Path | str) -> None:
        self._project_root = Path(project_root)
        self.path = self._project_root / _POLICY_DIRNAME / _POLICY_FILENAME

    def load(self) -> list[ApprovePolicyRule]:
        """读规则；文件缺失 → ``[]``，损坏 → 记 warning 并返回 ``[]``（fail-closed）。

        ``except`` 同时收 ``OSError`` 与 ``ValueError``：``read_text`` 对非 UTF-8
        字节抛 ``UnicodeDecodeError``（``ValueError`` 子类，**不是** ``OSError``）；
        人工手改 / 编码损坏的配置文件不得把正在跑的 run 顶崩（fail-closed 回落逐调用审批）。
        """
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            logger.warning("读取审批规则文件失败，按无规则处理：%s", self.path)
            return []
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("审批规则文件不是合法 JSON，按无规则处理：%s", self.path)
            return []
        entries = payload.get("rules") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            return []
        rules: list[ApprovePolicyRule] = []
        for entry in entries:
            try:
                rules.append(ApprovePolicyRule.from_dict(entry))
            except (KeyError, TypeError, ValueError):
                logger.warning("审批规则条目不可解析，跳过：%r", entry)
        return rules

    def save(self, rules: list[ApprovePolicyRule]) -> None:
        """原子落盘（临时文件 + ``os.replace``）：崩溃不产生半截文件丢规则。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"rules": [rule.to_dict() for rule in rules]}
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        _atomic_write(self.path, text)

    def find_match(self, identity: ApprovalIdentity) -> ApprovePolicyRule | None:
        """返回与本次调用匹配的持久规则；无命中 → ``None``。

        匹配规则（F22，纯 equality，无模糊）：
        - exact 档：``rule.key == identity.key()`` 全等；
        - command 档：``rule.tool == identity.tool_name`` 且
          ``rule.key == identity.canonical``（不含 args hash）。

        两档都先过 ``permission_at_approval == identity.permission``（防提权：
        批准后工具权限被抬高则旧规则失效，必须重新审批）。
        """
        for rule in self.load():
            if rule.permission_at_approval != identity.permission:
                continue
            if rule.granularity is PolicyGranularity.EXACT:
                if rule.key == identity.key():
                    return rule
            elif rule.tool == identity.tool_name and rule.key == identity.canonical:
                return rule
        return None

    def add_rule(self, rule: ApprovePolicyRule) -> ApprovePolicyRule:
        """安装一条规则；命令级 key 含换行符直接拒绝（Codex 语义）。

        含换行符的命令级规则抛 ``ValueError`` 且**不落盘**——换行可把一条规则
        走私成多条 / 改变命令边界；命令级前缀必须逐字匹配一条命令。exact 档
        不做此限制：完整身份键本就逐字锁死，无法用换行扩大覆盖面。
        """
        if rule.granularity is PolicyGranularity.COMMAND and any(
            br in rule.key for br in _LINE_BREAKS
        ):
            raise ValueError(
                "命令级审批规则含换行符，拒绝安装（防换行走私，对齐 Codex）"
            )
        with _policy_lock(self.path):
            rules = self.load()
            rules.append(rule)
            self.save(rules)
        return rule

    def remove_rule(self, rule_id: str) -> bool:
        """撤销一条规则（幂等）：删除成功 → ``True``，不存在 → ``False``（不报错）。"""
        with _policy_lock(self.path):
            rules = self.load()
            remaining = [rule for rule in rules if rule.id != rule_id]
            if len(remaining) == len(rules):
                return False
            self.save(remaining)
            return True


@contextmanager
def _policy_lock(path: Path) -> Iterator[None]:
    """跨进程「读-改-写」文件锁（#684 P2-2）。

    ``add_rule`` / ``remove_rule`` 是 ``load() → 改 → save()``：两个进程（或两个
    线程）并发时会互相覆盖——后写者赢，先写者的规则静默丢失。锁必须**独立于数据
    文件**：``save`` 用 ``os.replace`` 换掉数据文件的 inode，锁在旧 inode 上会失去
    意义，故用同目录的 ``<name>.lock`` 旁路文件。

    - **POSIX**：``fcntl.flock(LOCK_EX)``。flock 锁随打开的文件描述关联，**同一进程
      内两次独立 open 也互斥** ⇒ 线程并发同样被串行化。
    - **Windows**：``msvcrt.locking(LK_LOCK)``（阻塞式，最多重试 10 次）。
    - 两种原语都不可用：退化为**无锁**（单进程假设），不抛错——锁是防丢更新的
      加固，不是安全边界，缺锁不得反过来阻断读写。
    """
    lock_path = path.parent / (path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is not None:
        with open(lock_path, "w", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is not None:  # pragma: no cover - 仅 Windows
        with open(lock_path, "a+b") as handle:
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                with suppress(OSError):
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    yield  # pragma: no cover - 无锁原语平台


def _atomic_write(path: Path, text: str) -> None:
    """原子替换写入：同目录建临时文件 → 写入 → ``os.replace`` 覆盖。

    直接 ``write_text`` 在写到一半崩溃 / 断电时会把原文件截断，规则全丢
    （「防止数据丢失的错误处理」红线）。``os.replace`` 在同一文件系统上是原子操作。
    """
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".approve-policy-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_name)
        raise


__all__ = [
    "ApprovePolicyRule",
    "ApprovePolicyStore",
    "PolicyGranularity",
]
