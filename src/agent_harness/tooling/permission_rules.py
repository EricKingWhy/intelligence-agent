"""审批层权限规则引擎（#358 / W-14）——三层 allow / ask / deny 的路由启发式。

结构抄 Claude Code 的权限规则（一手来源 S1 https://code.claude.com/docs/en/permissions）：
- 三层 ``allow`` / ``ask`` / ``deny``；求值顺序 **deny → ask → allow**，first match 胜出；
- **deny 永远赢**（跨层 union：窄 allow 挖不动宽 deny）。
双轴默认语义（workspace-write + ask）抄 DeepSeek Harness（`DECISION-BRIEF.md` §三②）。

⚠ 诚实边界（铁律）：本模块是**审批层**的路由启发式，**不是安全边界**。它决定"要不要问人 /
要不要拒"，**不提供进程级隔离**——真正的 OS 隔离靠 Docker sandbox（`05 §5`）或路线 B（#729）。
证据：四家成熟产品一致证伪"字符串黑名单当安全边界"（Pi 文档亲口承认拦不住路径访问；
DSH 宁可 fail-closed 也不做字符串过滤）。破坏性命令分类器**已知可绕过**，例如：

- ``sh -c 'rm -rf /'``（首 token 是 ``sh``，不是 ``rm``）；
- 变量拼接／命令替换：``X=rm; $X -rf /``、``$(printf rm) -rf /``；
- 全路径：``/bin/rm -rf /``（首 token 不是裸 ``rm``）；
- 间接执行：``base64 -d | sh``、``python -c 'import shutil; ...'``、``eval ...``；
- 管道 / 重定向里藏破坏性命令（连接符使其归入"其余"→ ASK，仍可被人工批准）。

因此 deny 规则只做**审批路由 + 拒绝**，绝不宣称阻断；详见 `docs/adr/0051-*.md` 的 Honesty 章节。
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

__all__ = [
    "PermissionRule",
    "PermissionRuleSet",
    "RuleVerdict",
    "classify_bash_command",
    "default_rule_set",
]


class RuleVerdict(str, Enum):
    """一条规则（或一次求值）的裁决。

    取 ``str`` 子类：与 ``ToolSideEffect`` / ``ErrorCode`` 同理，日志可读、可序列化。
    ``NO_MATCH`` 是**没有规则命中**的哨兵，不是可安装的规则裁决——调用方见到它即回落到
    既有的 ``needs_approval`` 语义（本模块不替它下结论）。
    """

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"
    NO_MATCH = "no_match"


@dataclass(frozen=True)
class PermissionRule:
    """一条审批路由规则。

    ``match`` 是参数谓词（``None`` = 只按工具名命中）。``path_arg`` 非空时额外要求：
    该参数解析到 workspace **之外**才命中（R2 路径规则；workspace_root 缺失时跳过，
    见 :meth:`PermissionRuleSet.evaluate`）。``reason`` 可含 ``{path}`` / ``{resolved}`` /
    ``{command}`` / ``{rule}`` 格式化槽，由 :func:`_format_reason` 安全填充。
    """

    name: str
    tools: frozenset[str]
    verdict: RuleVerdict
    reason: str
    source: str
    match: Callable[[dict], bool] | None = None
    path_arg: str | None = None

    def applies_to(self, tool_name: str) -> bool:
        """工具名是否在本规则作用面内（``"*"`` 为通配）。"""
        return "*" in self.tools or tool_name in self.tools


@dataclass(frozen=True)
class PermissionRuleSet:
    """三层规则集合；``evaluate`` 是唯一求值入口。

    三层各按声明顺序扫描，层间顺序固定 deny → ask → allow。``first match`` 胜出 ⇒
    deny 层的任何命中都先于 ask / allow 返回。
    """

    allow: tuple[PermissionRule, ...] = ()
    ask: tuple[PermissionRule, ...] = ()
    deny: tuple[PermissionRule, ...] = ()

    def evaluate(
        self,
        tool_name: str,
        args: dict,
        *,
        workspace_root: Path | None = None,
    ) -> tuple[RuleVerdict, str, str | None]:
        """求一条调用的裁决；返回 ``(verdict, reason, rule_name)``。

        未命中任何规则 → ``(NO_MATCH, "", None)``。
        """
        for layer in (self.deny, self.ask, self.allow):
            for rule in layer:
                if not rule.applies_to(tool_name):
                    continue
                matched, resolved = self._matches(rule, args, workspace_root)
                if not matched:
                    continue
                return (
                    rule.verdict,
                    _format_reason(rule.reason, args, rule.name, resolved),
                    rule.name,
                )
        return RuleVerdict.NO_MATCH, "", None

    @staticmethod
    def _matches(
        rule: PermissionRule, args: dict, workspace_root: Path | None,
    ) -> tuple[bool, str | None]:
        """规则是否命中；第二个返回值为格式化 reason 用的 ``resolved``（无则 None）。"""
        if rule.match is not None and not rule.match(args):
            return False, None
        if rule.path_arg is None:
            return True, None
        raw = args.get(rule.path_arg)
        # 无路径参数 / 无 workspace_root ⇒ 本规则不适用（R2 只对"带 path 的文件工具 + 有根"生效）。
        if not isinstance(raw, str) or not raw or workspace_root is None:
            return False, None
        resolved = _resolve_outside(raw, workspace_root)
        if resolved is None:
            return False, None
        return True, resolved


def _resolve_outside(raw_path: str, workspace_root: Path) -> str | None:
    """路径是否解析到 workspace **之外**；是则返回解析后的绝对路径，否则 None。

    复用 ``sandbox/base.py::resolve_within_workspace`` 的同款语义（``Path.resolve()`` +
    ``is_relative_to``）——**不手写字符串前缀比对**（那既不准也是 theater）。解析异常
    （极端非法路径）按"判不出越界"处理（None，不命中该规则），不让一条坏路径把工具调用
    打成崩溃。
    """
    try:
        root = Path(workspace_root).resolve()
        path = Path(raw_path)
        resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if resolved.is_relative_to(root):
        return None
    return str(resolved)


def _format_reason(
    template: str, args: dict, rule_name: str, resolved: str | None,
) -> str:
    """填充 reason 里的格式化槽；缺键按空串处理（**不**因模板笔误抛 KeyError）。"""
    from collections import defaultdict

    context: defaultdict[str, str] = defaultdict(str)
    context["rule"] = rule_name
    command = args.get("command")
    if isinstance(command, str):
        context["command"] = command
    context["path"] = str(args.get("path", ""))
    if resolved is not None:
        context["resolved"] = resolved
    return template.format_map(context)


# ============================================================================
# bash 命令分类（审批层路由启发式，**不是**安全边界——见模块 docstring）
# ============================================================================

#: 提权包装器：`sudo ls` 的首 token 就是 `sudo`（不剥离包装）→ 一律 DENY。
_PRIVILEGE_COMMANDS = frozenset({"sudo", "doas", "runas", "su"})
#: 磁盘/分区破坏性工具 → DENY（`mkfs.*` 前缀另判）。
_DISK_COMMANDS = frozenset({"mkfs", "format", "dd", "diskpart"})
#: 只读子集白名单（**allowlist，不是黑名单**）——设计来源：Claude Code 内置只读命令集（S1）。
_READONLY_COMMANDS = frozenset({
    "ls", "dir", "cat", "type", "head", "tail", "more", "find", "grep", "findstr",
    "pwd", "echo", "whoami", "date", "hostname", "uname", "wc", "diff", "file",
    "stat", "tree", "printenv", "which",
})
#: shell 连接符：出现任一即**不算只读**（保守——连接符后的命令无法从首 token 判定）。
_SHELL_CONNECTORS = (";", "&&", "||", "|", "$(", "`", ">", "<", "&")


def classify_bash_command(command: str) -> RuleVerdict:
    """把一条 bash 命令路由成 DENY / ALLOW / ASK（审批层启发式，可被绕过）。

    解析用 ``shlex.split(posix=True)``；解析失败 / 空命令 → 保守按"其余"（ASK）。
    首 token 小写归一后判定：

    - **DENY**：提权（sudo/doas/runas/su）；``rm`` 且参数含递归旗（``-r``/``-R``/
      ``--recursive``，含 ``-rf`` 等合并短旗）；``mkfs``/``format``/``dd``/``diskpart``
      或以 ``mkfs.`` 开头。
    - **ALLOW**：首 token ∈ 只读白名单 **且** 命令不含任何 shell 连接符。
    - **ASK**：其余（含 ``git``——子命令读写难分；以及所有解析失败）。
    """
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return RuleVerdict.ASK
    if not tokens:
        return RuleVerdict.ASK

    first = tokens[0].lower()
    if first in _PRIVILEGE_COMMANDS or first in _DISK_COMMANDS or first.startswith("mkfs."):
        return RuleVerdict.DENY
    if first == "rm" and any(_is_recursive_flag(token) for token in tokens[1:]):
        return RuleVerdict.DENY
    if first in _READONLY_COMMANDS and not _has_shell_connector(command):
        return RuleVerdict.ALLOW
    return RuleVerdict.ASK


def _is_recursive_flag(token: str) -> bool:
    """``rm`` 的递归旗判定：``--recursive`` 或合并短旗里含 ``r``/``R``（如 ``-rf``）。"""
    if token == "--recursive":
        return True
    if token.startswith("--"):
        return False
    if not token.startswith("-") or len(token) < 2:
        return False
    return "r" in token[1:].lower()


def _has_shell_connector(command: str) -> bool:
    return any(connector in command for connector in _SHELL_CONNECTORS)


# ============================================================================
# default 矩阵（验收依据：DECISION-BRIEF.md §三②）
# ============================================================================


def _bash_verdict_is(verdict: RuleVerdict) -> Callable[[dict], bool]:
    """构造"bash 命令分类命中指定裁决"的谓词（R1/R3/R4 共用，单一分类源）。"""

    def _predicate(args: dict) -> bool:
        command = args.get("command")
        if not isinstance(command, str):
            return False
        return classify_bash_command(command) is verdict

    return _predicate


_DEFAULT_RULES = PermissionRuleSet(
    # deny 层先行（R1）：破坏性 bash——deny 永远赢，不可被 allow 覆盖。
    deny=(
        PermissionRule(
            name="deny-destructive-bash",
            tools=frozenset({"bash"}),
            verdict=RuleVerdict.DENY,
            reason=(
                "命中破坏性规则 '{rule}'：拒绝执行；这是审批层规则，不是 OS 隔离。"
                "deny 不可被 allow 覆盖。"
            ),
            source="claude-code:S1(deny 永远赢) + DECISION-BRIEF §三②",
            match=_bash_verdict_is(RuleVerdict.DENY),
        ),
    ),
    ask=(
        # R2：文件工具路径越出工作区 → ASK + 明确提示越界（DSH workspace-write 语义）。
        PermissionRule(
            name="ask-path-outside-workspace",
            tools=frozenset({"read", "write", "edit", "grep", "apply_patch", "glob"}),
            verdict=RuleVerdict.ASK,
            reason=(
                "⚠️ 路径越出工作区：'{path}' → '{resolved}'。"
                "注意：即使用户批准，sandbox 路径围墙仍会独立拒绝（审批≠越墙）。"
            ),
            source="DSH workspace-write + DECISION-BRIEF §三②",
            path_arg="path",
        ),
        # R4：bash 其余 → 逐次审批。
        PermissionRule(
            name="ask-bash",
            tools=frozenset({"bash"}),
            verdict=RuleVerdict.ASK,
            reason="Bash 命令需逐次审批。",
            source="claude-code:S1 + DSH ask",
            match=_bash_verdict_is(RuleVerdict.ASK),
        ),
        # R5：工作区内编辑 → 逐次审批（per-call，不默许整个 session）。
        PermissionRule(
            name="ask-workspace-edit",
            tools=frozenset({"write", "edit", "apply_patch"}),
            verdict=RuleVerdict.ASK,
            reason="文件编辑需逐次审批（per-call，不默许整个 session）。",
            source="claude-code:S1 写/编辑需问",
        ),
    ),
    allow=(
        # R3：bash 只读子集 → 免审批。
        PermissionRule(
            name="allow-readonly-bash",
            tools=frozenset({"bash"}),
            verdict=RuleVerdict.ALLOW,
            reason="只读命令，免审批。",
            source="claude-code:S1 内置只读命令集",
            match=_bash_verdict_is(RuleVerdict.ALLOW),
        ),
        # R6：工作区内读类 → 免审批。
        PermissionRule(
            name="allow-workspace-read",
            tools=frozenset({"read", "glob", "grep"}),
            verdict=RuleVerdict.ALLOW,
            reason="工作区内读取，免审批。",
            source="claude-code:S1 工作区内读免问",
        ),
    ),
)

_DEFAULT_RULE_SET: PermissionRuleSet | None = None


def default_rule_set() -> PermissionRuleSet:
    """模块级单例：默认权限矩阵（R1–R6）。

    惰性构造一次后复用——规则集是不可变值对象，重复构造没有意义，且单例让
    "default 矩阵就是这一份"成为可断言的事实（``default_rule_set() is default_rule_set()``）。
    """
    global _DEFAULT_RULE_SET
    if _DEFAULT_RULE_SET is None:
        _DEFAULT_RULE_SET = _DEFAULT_RULES
    return _DEFAULT_RULE_SET
