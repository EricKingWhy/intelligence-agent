"""SKILL.md 静态 lint（#529 §5.1）：沉淀闭环的验证门第一版（lint + 人审，重放 DEFER）。

六条规则（§5.1，全部程序化可测试）：
1. frontmatter 可解析且 name/description 非空——**复用 parse_skill_markdown**
   （依据 F：单真相，避免两套校验漂移）；
2. 触发条件非空：when_to_use 非空，或 description 含触发语——description 当
   触发问题优化（依据 C skill-creator 的静态可检查子集）；
3. 危险动作标注：正文命中危险模式（删库/删文件/外发网络）必须显式声明
   allowed-tools——危险动作显式化而非藏正文，对标依据 C 反僵化精神；
4. 大小上限 64KB（MAX_SKILL_BYTES，写入侧语义，discovery.py 定义）；
5. name 冲突：与既有 catalog 重名 → 阻断并提示走更新分支（oh-my-pi shadowed
   语义显式化，依据 B）；
6. 反僵化：正文 ALWAYS/NEVER 全大写指令词 → 警告不阻断（依据 C 子集）。

输出结构化结果：errors（阻断注册）/ warnings（不阻断）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from agent_harness.skills.discovery import MAX_SKILL_BYTES, parse_skill_markdown

#: 触发语提示词（规则 2 的启发面）：when_to_use 缺席时，description 命中任一
#: （英文大小写不敏感）即视为"含触发条件"。启发式，宁松勿严——触发语判断
#: 失手由人审门兜底（§5.2）。
TRIGGER_HINTS: tuple[str, ...] = (
    "use when", "when to", "use this", "if you need", "when you",
    "触发", "当用户", "当需要", "需要时", "适用于", "用于", "何时",
)

#: 危险动作模式表（规则 3，§5.1"模式表可配置"）：正文命中且未声明
#: allowed-tools → 阻断。删库 / 删文件 / 强推 / SQL 落库 / 外发网络。
DANGEROUS_BODY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"rm\s+-[a-z]*r[a-z]*f|rm\s+-[a-z]*f[a-z]*r"),
    re.compile(r"git\s+push\b[^\n]*--force"),
    re.compile(r"drop\s+(table|database)", re.IGNORECASE),
    re.compile(r"删除(数据库|整个目录|所有文件|全部文件|系统文件)"),
    re.compile(r"\b(?:curl|wget)\s"),
    re.compile(r"\b(?:POST|PUT)\s+https?://"),
)

#: 反僵化警告（规则 6）：正文全大写 ALWAYS/NEVER 指令词（依据 C：模型对
#: 教条式大写指令过度服从，skill-creator 明文禁止）。
_ANTI_RIGIDITY_RE = re.compile(r"\b(ALWAYS|NEVER)\b")


@dataclass
class LintResult:
    """lint 结果：errors 阻断注册，warnings 只提示。"""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """无阻断错误（warnings 不影响）。"""
        return not self.errors


def lint_skill(
    path: Path,
    existing_names: Iterable[str] = frozenset(),
    *,
    dangerous_patterns: tuple[re.Pattern[str], ...] | None = None,
) -> LintResult:
    """对单个 SKILL.md 跑六条规则（§5.1）；只读分析，绝不改文件。"""
    result = LintResult()
    path = Path(path)

    # 规则 4：尺寸上限先于任何读盘（与发现侧同一"stat 先于 read"纪律，
    # 上限是写入侧 MAX_SKILL_BYTES——64KB，不是发现侧的 1MB）。
    try:
        size = path.stat().st_size
    except OSError as error:
        result.errors.append(f"{path}: unreadable ({type(error).__name__})")
        return result
    if size > MAX_SKILL_BYTES:
        result.errors.append(f"{path}: too large (> {MAX_SKILL_BYTES} bytes)")
        return result

    # 规则 1：frontmatter 可解析且 name/description 非空——直接复用解析器。
    entry, parse_errors = parse_skill_markdown(path)
    if entry is None:
        result.errors.extend(parse_errors)
        return result

    # 规则 2：触发条件非空。
    if not entry.when_to_use:
        description = entry.description.lower()
        if not any(hint in description or hint in entry.description for hint in TRIGGER_HINTS):
            result.errors.append(
                f"{path}: skill '{entry.name}' 缺少触发条件"
                f"（when_to_use 为空且 description 无触发语）"
            )

    # 规则 3：危险动作必须显式声明 allowed-tools。
    patterns = DANGEROUS_BODY_PATTERNS if dangerous_patterns is None else dangerous_patterns
    try:
        body = entry.load_body()
    except (OSError, UnicodeDecodeError) as error:
        result.errors.append(f"{path}: body unreadable ({type(error).__name__})")
        return result
    allowed_tools = entry.meta.get("allowed-tools")
    declared = bool(allowed_tools) if isinstance(allowed_tools, (str, list, tuple)) else False
    if not declared and any(pattern.search(body) for pattern in patterns):
        result.errors.append(
            f"{path}: skill '{entry.name}' body contains dangerous actions "
            f"(destructive filesystem / outbound network patterns) but declares no "
            f"allowed-tools — declare them explicitly or remove the actions"
        )

    # 规则 5：name 冲突 → 阻断 + 提示走更新分支。
    if entry.name in set(existing_names):
        result.errors.append(
            f"{path}: skill '{entry.name}' already exists in the catalog — "
            f"refusing to register a duplicate, use the update branch instead"
        )

    # 规则 6：ALWAYS/NEVER 全大写 → 警告不阻断。
    for match in _ANTI_RIGIDITY_RE.finditer(body):
        result.warnings.append(
            f"{path}: body uses uppercase '{match.group(1)}' "
            f"(anti-rigidity: prefer contextual guidance over absolute commands)"
        )
    return result
