"""持久审批规则（approve_policy）：项目级「以后都允许」授权配置（#684 Phase 1，
#1487 迁出工作区）。

规则以人类可读 JSON 存**控制目录** ``<home>/.agent-harness/approve-policy/
<project_key>.json``（project_key = resolved 项目根的 sha256 hex）：按项目隔离、
人类可读可手改；删文件即清空。本模块只负责**规则存储与命中判定**；命中后的放行
与安装触发在 ``ToolExecutor`` 的审批闸门里（Tool 只有一条执行路径，不变量 #7），
不另开隐藏授权入口。

**为什么必须在工作区外（#1487，裁决 24-A）**：审批规则 = 授权凭据。#684 v1 把
它放 ``<project_root>/.agent-harness/approve-policy.json``——那正是 agent 执行
进程的领地（write/edit 直接可达、bash 至少同权）：被授权者能改写授权自己被
放行的条件，等于自授权。迁出后对执行进程的可达性逐引擎成立——write/edit 被
Sandbox 路径围栏拒（resolve 后必须在 workspace 内，symlink 绕到同样被拒）；
Docker 只挂 workspace 具名卷，host home 不进容器；local bash 与 harness 同
进程同权是**已知残余**（无 OS 隔离层的引擎无法靠目录位置防同权写，如实登记
不声称，见 ADR-0058）。验收口径 = 「执行进程确实写不到」，不是「文件搬了家」。

**env 覆盖缝**：``AGENT_HARNESS_APPROVE_POLICY_ROOT`` 是控制目录的唯一注入口
（默认 home 下）。测试隔离与部署钉根都走它——``ToolExecutor`` / CLI / Web 三处
构造点都在进程内构造 store，env 是它们共享的唯一定制面。

**旧位迁移（显式、严格）**：升级前工作区里的旧文件不再被 Runtime 读取
（``load`` 永不自动信任旧位——旧位在 agent 可达空间，静默导入 = 信任 agent
自建文件）。迁移唯一入口 = CLI ``approvals policy import-legacy``：按
``import_legacy()`` 的严格校验（created_at 必须非空且可按 ISO 8601 解析、
command 档 key 不得含换行）逐条导入、按 id 去重、幂等；控制文件在场后旧位
内容零效力。

对标（PORT DESIGN）：

- **Claude Code protected paths + ``~/.claude/projects``** —— 抄两个形态：保护
  对象是**实际执行进程**可达性而非目录名；per-project 用户级存储（项目哈希
  子路径）不在项目树内。
- **Claude Code ``settings.json``** —— 抄「人类可读 JSON、规则即字符串列表、
  可手改可 review」的文件形态；本票存位从项目级改用户级 per-project。
- **Codex ``acceptWithExecpolicyAmendment``** —— 抄「精确安装」语义：安装到盘上
  的规则必须来自 Runtime 从已校验参数派生的精确值（用户只能选粒度，不能手写规则
  文本）；渲染后含换行符的命令级规则**拒绝安装**（防换行走私，迁移导入同红线）。
- **ZCode ``exactCommand`` / ``commandPrefix``** —— 抄两档粒度：``EXACT``（完整
  身份键，含 args hash）与 ``COMMAND``（tool + command，不含 args hash）。
- 本仓先例：``Settings.provider_store_path``（``config.py``）早已用
  ``<home>/.agent-harness/`` 存跨项目控制面数据，本票与之同形。

安全红线（与 F21 / F22 同源，``docs/agents/684-design-proposal.md`` §8）：

1. 只有用户主动选「以后都允许」才安装规则（入口唯一，禁止静默创建）；
2. 只有 exact / command 两档，纯 equality 匹配，绝不做前缀 / 通配 / 文本相似
   （F22：系统不猜）；
3. 命中时重验工具当前 ``permission == permission_at_approval``（防提权）。

持久规则是**配置**，不是会话运行时状态：不并入 ``permission_policy``，也不受
#526 会话授权的 1800s TTL 约束（ADR-0041 D6 三概念分离）。

**项目根口径（v1 已知边界）**：v1 以**进程当前工作目录**（``Path.cwd()``）为项目根
——``ToolExecutor`` 回落、CLI / Web 管理面三处逐字一致，保证读写同一份控制文件。
**多 workspace 同进程**场景（同一进程内切换不同项目根）尚不支持，待后续票；
届时项目根必须由调用方显式传入、不再回落 cwd。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
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

#: 旧位（#684 v1 → #1487 迁出前）：项目根下的配置目录 / 文件名。迁移后只作
#: ``import_legacy()`` 的读取源与 ``legacy_present`` 探测目标，Runtime 不再读它。
_POLICY_DIRNAME = ".agent-harness"
_POLICY_FILENAME = "approve-policy.json"

#: 控制目录（#1487）：home 下的子目录名（与 ``provider_store_path`` 同根）。
_CONTROL_DIRNAME = "approve-policy"

#: 控制目录的唯一 env 注入口（默认 ``<home>/.agent-harness/approve-policy``）：
#: 测试隔离 + 部署钉根都走这里；三处生产构造点共享同一 env 缝。
_CONTROL_ENV = "AGENT_HARNESS_APPROVE_POLICY_ROOT"

#: 命令级规则里出现即拒绝安装的换行字符（Codex 语义：渲染后含换行的前缀不展示，
#: 从源头拒绝比渲染时过滤更难绕过——换行可把一条规则走私成多条 / 改变命令边界）。
_LINE_BREAKS = ("\n", "\r")


def default_control_root() -> Path:
    """控制目录（#1487）：``AGENT_HARNESS_APPROVE_POLICY_ROOT`` env 覆盖，默认
    ``<home>/.agent-harness/approve-policy``。

    env 是**唯一**注入口：三处生产构造点（``ToolExecutor`` / CLI / Web）都在进程
    内构造 store，没有可逐点传参的缝；测试隔离（不污染真实 home）与部署钉根
    （控制目录放指定卷）都走它。与 ``Settings.provider_store_path`` 同根
    （``<home>/.agent-harness/``），形态对标 ``~/.claude/projects``。

    空串是部署脚本变量展开失败的常见产物：**忽略并回落默认**（钉根被丢弃必须
    留 warning，不能静默）；``~`` 前缀会 ``expanduser`` 展开（env 值不经过 shell）。
    """
    override = os.environ.get(_CONTROL_ENV)
    if override:
        return Path(override).expanduser()
    if override is not None:
        logger.warning("%s 设为空串，忽略该覆盖并回落默认控制目录", _CONTROL_ENV)
    return Path.home() / _POLICY_DIRNAME / _CONTROL_DIRNAME


def project_key(project_root: Path | str) -> str:
    """控制目录内区分不同项目的文件名：resolved 根路径的 sha256 hex。

    为什么要 hash 而不是原文路径：跨平台文件名安全（Windows 盘符冒号 / 反斜杠 /
    Linux 路径分隔符都不能直接当文件名），且同一**存在**项目根的 symlink /
    大小写变体 resolve 后归一到同一 key（Windows 的 ``resolve`` 只规范存在路径
    的组件大小写，不存在的路径不归一）——三处构造点（显式传入 / ``cwd`` 回落，
    二者都指向存在路径）对同一项目算出同一份控制文件，不因传入口径差分家。
    """
    resolved = str(Path(project_root).resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()


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


@dataclass(frozen=True)
class LegacyImportResult:
    """``import_legacy()`` 的结果：成功导入的规则 + 跳过条数（不合规 / id 重复）。"""

    imported: list[ApprovePolicyRule]
    skipped: int


class LegacyImportError(Exception):
    """旧位文件在场但整体不可读 / 坏 JSON / 形态不对（迁移面专用，P3-1）。

    ``load`` 对坏数据 fail-closed（当没有规则）；迁移不能这么坍缩——命令输出
    会建议删除旧文件，把「读不动」报成「迁移完成可删除」会诱导用户删掉可能
    含可恢复规则的坏文件。CLI 捕获它：如实提示 + 退出码 2 + 不给删除建议。
    """


def _parse_legacy_rule(entry: object) -> ApprovePolicyRule | None:
    """严格解析一条旧位规则条目；不合规返回 ``None``（导入侧跳过并计数）。

    比 ``from_dict`` 更严的两道额外校验（#1487 迁移语义——旧位在 agent 可达
    空间，导入它就是把可达空间的内容升格为授权，必须挑明来历可审计）：

    - ``created_at`` 非空且可按 ISO 8601 解析（``Z`` 容忍）——每条规则都该是
      Runtime 安装的、有真实安装时刻；手搓 / agent 自建文件常缺这一格；
    - command 档 ``key`` 不得含换行（与 ``add_rule`` 同红线：换行可走私规则边界）。
    """
    if not isinstance(entry, dict):
        return None
    try:
        rule = ApprovePolicyRule.from_dict(entry)
    except (KeyError, TypeError, ValueError):
        return None
    if not rule.created_at:
        return None
    try:
        # 3.11+ 的 fromisoformat 原生容忍 ``Z`` 后缀（requires-python >= 3.11）。
        datetime.fromisoformat(rule.created_at)
    except ValueError:
        return None
    if rule.granularity is PolicyGranularity.COMMAND and any(
        br in rule.key for br in _LINE_BREAKS
    ):
        return None
    return rule


class ApprovePolicyStore:
    """控制目录 ``<control_root>/<project_key>.json`` 的读写与命中判定（#1487）。

    ``control_root`` 缺省走 ``default_control_root()``（env 缝 → home 默认）；
    旧位 ``legacy_path`` 只作 ``import_legacy()`` 的读取源，``load`` 永不读它。

    每次 ``load`` 都现读文件（不缓存）：规则是配置，人工删改 / ``remove`` 后必须
    立刻生效，不依赖进程重启。坏数据 fail-closed——读不动就当成没有规则
    （回落到默认逐调用审批），绝不臆造放行。
    """

    def __init__(
        self,
        project_root: Path | str,
        *,
        control_root: Path | str | None = None,
    ) -> None:
        self._project_root = Path(project_root)
        if control_root is not None:
            # 空串 / Path() 的 str 形态是 "."（A 轴审查 P3-2）：控制文件会被放回
            # 当前目录——正是本票要迁出的 agent 领地，宁可 fail-fast 不静默接受。
            root = Path(control_root)
            if not str(root).strip() or str(root) == ".":
                raise ValueError(
                    "control_root 不能为空值（会把控制文件放回当前目录，agent 领地）"
                )
        else:
            root = default_control_root()
        self.path = root / f"{project_key(self._project_root)}.json"
        self.legacy_path = self._project_root / _POLICY_DIRNAME / _POLICY_FILENAME

    @property
    def legacy_present(self) -> bool:
        """旧工作区路径是否存在规则文件（CLI 迁移提示用；不影响任何授权判定）。"""
        return self.legacy_path.exists()

    def _load_entries(self, path: Path) -> list:
        """读 ``path`` 的 rules 原始条目；文件缺失 / 损坏 → ``[]``（fail-closed）。"""
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            logger.warning("读取审批规则文件失败，按无规则处理：%s", path)
            return []
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("审批规则文件不是合法 JSON，按无规则处理：%s", path)
            return []
        entries = payload.get("rules") if isinstance(payload, dict) else None
        return entries if isinstance(entries, list) else []

    def load(self) -> list[ApprovePolicyRule]:
        """读控制文件规则；缺失 → ``[]``，损坏 → 记 warning 并返回 ``[]``（fail-closed）。

        ``except`` 同时收 ``OSError`` 与 ``ValueError``：``read_text`` 对非 UTF-8
        字节抛 ``UnicodeDecodeError``（``ValueError`` 子类，**不是** ``OSError``）；
        人工手改 / 编码损坏的配置文件不得把正在跑的 run 顶崩（fail-closed 回落逐调用审批)。
        旧位 ``legacy_path`` **不在**读取范围（#1487）：静默读旧位 = 信任 agent
        可达空间里的自建文件；迁移只能走显式 ``import_legacy()``。
        """
        rules: list[ApprovePolicyRule] = []
        for entry in self._load_entries(self.path):
            try:
                rules.append(ApprovePolicyRule.from_dict(entry))
            except (KeyError, TypeError, ValueError):
                logger.warning("审批规则条目不可解析，跳过：%r", entry)
        return rules

    def import_legacy(self) -> LegacyImportResult:
        """把旧工作区规则文件**显式**迁移进控制目录（#1487）——严格校验 + 按 id 去重。

        调用方是 CLI ``approvals policy import-legacy``（唯一入口，用户手势）。
        逐条经 ``_parse_legacy_rule`` 严格校验；不合规条目跳过并计数，不中断整批。
        控制侧已有同 id 规则时**控制侧胜**（旧位不改写已有授权）；文件内重复 id
        只导入首条。幂等：重复执行时全为重复 id，零导入、零重复。整批经文件锁
        串行化（与 ``add_rule`` / ``remove_rule`` 同一把锁，防并发丢更新）。

        旧文件**在场但整体读不动 / 坏 JSON / 形态不对**时抛 ``LegacyImportError``
        （A 轴审查 P3-1）：迁移输出会建议用户删除旧文件，「读不动」必须与
        「没有规则」区分开——把坏文件谎报成「迁移完成可删除」是数据丢失路径。
        """
        entries = self._read_legacy_strict()
        if not entries:
            return LegacyImportResult(imported=[], skipped=0)
        imported: list[ApprovePolicyRule] = []
        skipped = 0
        with _policy_lock(self.path):
            rules = self.load()
            known_ids = {rule.id for rule in rules}
            for entry in entries:
                rule = _parse_legacy_rule(entry)
                if rule is None or rule.id in known_ids:
                    skipped += 1
                    logger.warning("旧审批规则条目未导入（不合规或 id 重复）：%r", entry)
                    continue
                rules.append(rule)
                known_ids.add(rule.id)
                imported.append(rule)
            if imported:
                self.save(rules)
        return LegacyImportResult(imported=imported, skipped=skipped)

    def _read_legacy_strict(self) -> list:
        """读旧位 rules 原始条目；缺失 → ``[]``，在场但读不动 → ``LegacyImportError``。

        与 ``load`` 的 fail-closed（读不动当没有规则）刻意不同：迁移是**销毁性
        建议的入口**（命令输出提示可删旧文件），必须把「读不动」如实抛给调用方。
        """
        if not self.legacy_present:
            return []
        try:
            raw = self.legacy_path.read_text(encoding="utf-8")
        except (OSError, ValueError) as e:
            raise LegacyImportError(f"旧规则文件读取失败：{e}") from e
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            raise LegacyImportError(f"旧规则文件不是合法 JSON：{e}") from e
        entries = payload.get("rules") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            raise LegacyImportError('旧规则文件顶层不是 {"rules": [...]} 形态')
        return entries

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
    "LegacyImportError",
    "LegacyImportResult",
    "PolicyGranularity",
    "default_control_root",
    "project_key",
]
