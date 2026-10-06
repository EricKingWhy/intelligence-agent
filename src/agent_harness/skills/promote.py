"""Skill 沉淀（#529 §3 判据 + §4 草稿生成 + §4 状态机）。

闭环的第一段：成功 run 里"什么算可沉淀"（§3 五条判据，全部函数化可测试）、
候选 skill 草稿怎么生成（§4：标准 SKILL.md 形状的 prompt 模板）、草稿如何
走到生效（§4 状态机 draft → lint-pass → human-approved → registered）。

边界（不变量）：
- 草稿只落 staging（``<workspace>/skills/.staging/<name>/SKILL.md``），**永不
  进 catalog**——staging 在单层扫描的第二层，结构上就不可见（§9-7 有测试钉住）；
- 人审是唯一生效门：human-approved 必须经显式 approve（§5.2），register 只
  从 human-approved 出发；任一步失败回 draft（可改）或丢弃；
- memory 与 skill 正交（§7）：判据输入复用 MemoryExtractor 的裁剪逻辑，但不
  写 memory store；skill 写失败不影响 memory（部分成功语义，测试见
  test_skills_promote_tool.py 的端到端用例）。
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from agent_harness.memory.extractor import MemoryExtractor
from agent_harness.session.event import (
    RUN_COMPLETED,
    RUN_FAILED,
    TASK_ACCEPTED,
    TASK_PLAN_UPDATED,
    TOOL_RESULT,
    SessionEvent,
)
from agent_harness.skills.discovery import (
    SkillCatalogEntry,
    SkillDiscovery,
    parse_skill_markdown,
    resolve_within,
)
from agent_harness.skills.lint import DANGEROUS_BODY_PATTERNS, LintResult, lint_skill

# ── 状态机词汇（§4）──────────────────────────────────────────────────────────

STATUS_DRAFT = "draft"
STATUS_LINT_PASS = "lint-pass"
STATUS_HUMAN_APPROVED = "human-approved"
STATUS_REGISTERED = "registered"

#: 可复现性声明标记（§3-3"在正文标注前置条件"形态的静态检查点）。
_PRECONDITION_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"前置条件|前置要求|运行前提"),
    re.compile(r"preconditions?", re.IGNORECASE),
)

# ── 负面清单模式（§3-5）：凭证 / 密钥 / 隐私。命中即一票否决。 ────────────────
#: 危险动作"未标注"的判定不在这里另写模式表——复用 lint 的 DANGEROUS_BODY_PATTERNS
#: （单真相，依据 F 同款纪律），本模块只负责"有模式命中且无 allowed-tools 声明"的组合。
_NEGATIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("credential", re.compile(
        r"(?i)\b(api[_-]?key|secret|password|passwd|access[_-]?token|auth[_-]?token)\b"
        r"\s*[:=]\s*['\"]?[^\s'\"]{4,}")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("bearer-token", re.compile(r"(?i)\bbearer\s+[a-z0-9._\-]{16,}")),
    ("privacy-id", re.compile(r"\b1[3-9]\d{9}\b|\b\d{17}[\dXx]\b")),  # 手机号 / 身份证号
)


@dataclass(frozen=True)
class CriteriaVerdict:
    """判据评估结果：ok=可提议；failed=未满足判据的可观察原因（不静默）。"""

    ok: bool
    failed: list[str] = field(default_factory=list)

    @classmethod
    def pass_(cls) -> CriteriaVerdict:
        return cls(ok=True)


@dataclass(frozen=True)
class ProposeOutcome:
    """propose 入口结果：accepted=False 时 reasons 说明被哪条判据挡下。"""

    accepted: bool
    name: str | None = None
    reasons: list[str] = field(default_factory=list)
    staging_path: Path | None = None


@dataclass
class DraftRecord:
    """staging 草稿的状态机记录（staging 文件本身是内容真相，本记录只管状态）。"""

    name: str
    status: str = STATUS_DRAFT
    lint: LintResult | None = None
    source_run_id: str | None = None
    confirmed_by: str | None = None


# ── §3 判据（五条，逐条函数化可测试）────────────────────────────────────────


def goal_achieved(events: list[SessionEvent]) -> bool:
    """判据 1（§3-1）目标达成：run 收口成功 **且**（plan 全部完成 或 用户接受）。

    `run/completed` 只说明 Runtime 收口（event.py 既有注释），必要非充分；
    plan 完成看最后一条 task/plan_updated 的 items 全 completed，用户确认成功
    看 task/accepted。失败 run 直接不满足。
    """
    types = [e.type for e in events]
    if RUN_FAILED in types or RUN_COMPLETED not in types:
        return False
    if any(e.type == TASK_ACCEPTED for e in events):
        return True
    plan_events = [e for e in events if e.type == TASK_PLAN_UPDATED]
    if not plan_events:
        return False
    items = plan_events[-1].data.get("items") or []
    return bool(items) and all(item.get("status") == "completed" for item in items
                               if isinstance(item, dict))


def evidence_sufficient(events: list[SessionEvent]) -> bool:
    """判据 2（§3-2）证据充分：event stream 里存在成功 tool 结果——关键步骤有
    tool 结果支撑，非纯模型臆想。空转 run（无 tool 调用）在此被挡下（§9-1）。

    tool/result 的 content 是 ToolResult.model_dump_json()（runtime 落盘契约），
    解析失败的条目不算证据（宁可漏提，不把臆想当证据）。
    """
    for event in events:
        if event.type != TOOL_RESULT:
            continue
        content = event.data.get("content") if isinstance(event.data, dict) else None
        try:
            result = json.loads(content) if isinstance(content, str) else None
        except ValueError:
            continue
        if isinstance(result, dict) and result.get("ok") is True:
            return True
    return False


def preconditions_declared(draft_text: str) -> bool:
    """判据 3（§3-3）可复现性（静态可查子集）：正文声明了前置条件。

    §3-3 允许"含外部偶然因素…或在正文标注前置条件"——第一版统一取后者：
    prompt 模板要求 CONTEXT 段必写前置条件（无则写"无特殊前置条件"），这里
    静态检查标记存在。语义级"是否真的可复现"由人审门兜底（§5.2）。
    """
    return any(pattern.search(draft_text) for pattern in _PRECONDITION_MARKERS)


def find_duplicate(
    name: str, description: str, existing: list[tuple[str, str]],
) -> str | None:
    """判据 4（§3-4）去重：与既有 skill 的 name 或 description 重复 → 返回既有名。

    description 相似度取保守子集：规范化（casefold + 空白压缩）后相等或互为
    包含才算——宁可漏判由 lint 规则 5 / 人审兜底，不误杀正当 skill。调用方
    （propose）据返回值分流：name 同名且 project 来源 = 更新分支入口（§10-5）；
    其余重复拒绝并指引（§3-4）。
    """
    for existing_name, existing_description in existing:
        if name == existing_name:
            return existing_name
        norm_new = re.sub(r"\s+", "", description.casefold())
        norm_old = re.sub(r"\s+", "", existing_description.casefold())
        if norm_new and norm_old and (norm_new == norm_old or norm_new in norm_old or norm_old in norm_new):
            return existing_name
    return None


def negative_vetoes(draft_text: str, *, one_off: bool = False,
                    allowed_tools_declared: bool = False) -> list[str]:
    """判据 5（§3-5）负面清单，一票否决，返回否决原因列表（空 = 通过）。

    - 凭证/密钥/用户隐私出现在草稿里 → 否决（隐私不进 skill 文件）；
    - 不可逆危险动作（删库/外发网络等）未显式标注 → 否决——模式表复用
      lint.DANGEROUS_BODY_PATTERNS（单真相），与 lint 规则 3 同判据，只在
      propose 阶段提前拦（同一草稿两道门，挡得早一点）；
    - 单次偶发成功（one_off 标记）→ 否决（§3-5/§3-3：运气不是经验）。
    """
    vetoes: list[str] = []
    for label, pattern in _NEGATIVE_PATTERNS:
        if pattern.search(draft_text):
            vetoes.append(f"负面清单[{label}]：草稿含凭证/密钥/隐私信息，不沉淀")
    if not allowed_tools_declared and any(
            pattern.search(draft_text) for pattern in DANGEROUS_BODY_PATTERNS):
        vetoes.append("负面清单[danger]：草稿含不可逆危险动作但未声明 allowed-tools，不沉淀")
    if one_off:
        vetoes.append("负面清单[one-off]：单次偶发成功不沉淀（§3-5）")
    return vetoes


def evaluate_criteria(
    events: list[SessionEvent],
    draft_text: str,
    *,
    name: str,
    description: str,
    existing: list[tuple[str, str]] | None = None,
    one_off: bool = False,
    allowed_tools_declared: bool = False,
) -> CriteriaVerdict:
    """§3 五条判据的复合评估：全过 → ok；任何一条不过 → failed 带原因。"""
    failed: list[str] = []
    if not goal_achieved(events):
        failed.append("判据1 目标达成：run 未收口成功，或 plan 未完成且无用户确认（§3-1）")
    if not evidence_sufficient(events):
        failed.append("判据2 证据充分：event stream 无成功 tool 结果支撑（§3-2）")
    if not preconditions_declared(draft_text):
        failed.append("判据3 可复现：正文未声明前置条件（§3-3）")
    if existing is not None:
        duplicate = find_duplicate(name, description, existing)
        if duplicate is not None:
            failed.append(f"判据4 去重：与既有 skill '{duplicate}' 重复，走更新分支（§3-4）")
    failed.extend(negative_vetoes(draft_text, one_off=one_off,
                                  allowed_tools_declared=allowed_tools_declared))
    return CriteriaVerdict(ok=not failed, failed=failed)


# ── §4 草稿生成 prompt 模板 ──────────────────────────────────────────────────

_DRAFT_PROMPT_TEMPLATE = """\
你正在把一次成功运行的经验沉淀为可复用的 Skill。请严格输出一个标准 SKILL.md 文件内容。

## 输入

### 本次运行的 event stream（已经裁剪，有界）
{events_json}

### 记忆整理（consolidation）产出
{consolidation}

### 既有 skill（去重扫描结果：name — description）
{existing}

## 输出要求

1. 第一行起是 `---` 围栏的 YAML frontmatter：
   - `name`（必备）：小写字母/数字开头，可含连字符/下划线，≤64 字符；
   - `description`（必备）：一句话说明这个技能做什么；
   - `when_to_use`（推荐）：触发条件（什么时候该用它）；
   - `allowed-tools`（推荐）：执行该技能需要的工具声明。
2. 正文按三段组织（用 `##` 标题）：
   - `## CONTEXT`：背景与**前置条件**（必写；无特殊前置条件也要明说）；
   - `## INSTRUCTIONS`：可复现的操作步骤；
   - `## EXAMPLES`：至少一个具体例子。
3. 红线：不得包含凭证、密钥、用户隐私；危险动作（删库/删文件/外发网络）必须
   通过 `allowed-tools` 显式声明；单次偶发、依赖外部偶然因素的经验不沉淀。
4. 与"既有 skill"去重：若经验已被某个既有 skill 覆盖，不要输出新草稿，
   说明应更新哪个既有 skill。
"""


def build_draft_prompt(
    events: list[SessionEvent],
    *,
    consolidation: str = "",
    existing: list[tuple[str, str]] | None = None,
) -> str:
    """草稿生成 prompt 模板（§4）：输入 = extractor 裁剪的 event stream +
    consolidation 产出 + 去重扫描结果；输出要求 = 标准 SKILL.md 形状。

    事件裁剪复用 MemoryExtractor._clip_events（§7：不另写一套 event 扫描）——
    单事件 1000 字符 / 总量 50 条的有界化在那里单点定义。
    """
    clipped = MemoryExtractor._clip_events(events)
    existing = existing or []
    lines = [f"- {name} — {description}" for name, description in existing]
    return _DRAFT_PROMPT_TEMPLATE.format(
        events_json=json.dumps(clipped, ensure_ascii=False, indent=2),
        consolidation=consolidation.strip() or "（无）",
        existing="\n".join(lines) if lines else "（无）",
    )


# ── §4 状态机：draft → lint-pass → human-approved → registered ───────────────


class SkillPromoter:
    """沉淀状态机 owner：staging 草稿的唯一管理面。

    staging 落盘 ``<staging_root>/<name>/SKILL.md``（wiring 传
    ``<workspace>/skills/.staging``）。staging_root 在 skill 目录的**第二层**，
    discovery 的单层扫描结构上扫不到——未确认草稿永不进 catalog 不靠纪律靠结构。
    """

    def __init__(self, discovery: SkillDiscovery, *, staging_root: Path) -> None:
        self._discovery = discovery
        self._staging_root = Path(staging_root)
        self._records: dict[str, DraftRecord] = {}

    # ── 查询 ──

    def status(self, name: str) -> str | None:
        record = self._records.get(name)
        return record.status if record is not None else None

    def drafts(self) -> list[DraftRecord]:
        return list(self._records.values())

    @property
    def staging_root(self) -> Path:
        return self._staging_root

    # ── 状态转移 ──

    def propose(
        self,
        draft_text: str,
        *,
        events: list[SessionEvent],
        one_off: bool = False,
        existing: list[tuple[str, str]] | None = None,
        source_run_id: str | None = None,
    ) -> ProposeOutcome:
        """判据评估 + 草稿落 staging（status=draft）。

        判据不过 → 拒绝且**零落盘**（连拒绝的草稿都不留残骸）；草稿本身解析
        不出合法 frontmatter（必备 name/description）同样拒绝——那是 lint 规则 1
        的前置形状，propose 不收连形状都不对的草稿。

        更新分支（§3-4/§10-5，审查 P2 处置）：name 与既有 skill 相同**且其来源
        在 project skill 目录** = 更新分支的合法入口（模型重写全文 + lint +
        人审，与新建同流程，注册时发 skill/updated）；其余重复（description
        相似、global/manual 来源同名）仍拒绝并指引。global 同名拒绝同时守住
        §6.1——闭环不写 global，放行只会制造 shadow 全局技能的冲突条目。
        """
        entry, parse_errors = self._parse_draft(draft_text)
        if entry is None:
            return ProposeOutcome(accepted=False, reasons=list(parse_errors))
        catalog_entries = self._discovery.catalog().entries
        if existing is None:
            existing = [(e.name, e.description) for e in catalog_entries]
        duplicate = find_duplicate(entry.name, entry.description, existing)
        update_target = (
            duplicate is not None and duplicate == entry.name
            and self._project_owned(duplicate, catalog_entries)
        )
        allowed_tools_declared = bool(entry.meta.get("allowed-tools"))
        verdict = evaluate_criteria(
            events, draft_text, name=entry.name, description=entry.description,
            existing=None if update_target else existing,
            one_off=one_off, allowed_tools_declared=allowed_tools_declared,
        )
        if not verdict.ok:
            return ProposeOutcome(accepted=False, name=entry.name, reasons=verdict.failed)

        staging_dir = self._staging_root / entry.name
        staging_dir.mkdir(parents=True, exist_ok=True)
        (staging_dir / "SKILL.md").write_text(draft_text, encoding="utf-8")
        self._records[entry.name] = DraftRecord(
            name=entry.name, status=STATUS_DRAFT, source_run_id=source_run_id,
        )
        return ProposeOutcome(accepted=True, name=entry.name,
                              staging_path=staging_dir / "SKILL.md")

    def run_lint(self, name: str) -> LintResult:
        """draft → lint-pass；lint 失败回 draft（可改后重跑 lint，§4 状态机）。

        规则 5（name 冲突）只拦"不可更新"的同名——global/manual 来源（审查 P2
        处置：project 同名是更新分支的合法目标 §10-5，不在 lint 处堵死，否则
        设计 §3-4/§5.1 承诺的更新分支永远不可达，SKILL_UPDATED 成死路径）。
        """
        record = self._require(name, STATUS_DRAFT, extra={STATUS_LINT_PASS})
        project_dir = self._discovery.project_dir
        blocked_names = {
            e.name for e in self._discovery.catalog().entries
            if project_dir is None or not resolve_within(e.source_path, project_dir)
        }
        result = lint_skill(self._staging_path(name), existing_names=blocked_names)
        record.lint = result
        record.status = STATUS_LINT_PASS if result.ok else STATUS_DRAFT
        return result

    def approve(self, name: str, *, confirmed_by: str) -> None:
        """lint-pass → human-approved：人审门（§5.2），必须显式确认，无默认路径。"""
        record = self._require(name, STATUS_LINT_PASS, action="approve（需先 lint-pass）")
        record.status = STATUS_HUMAN_APPROVED
        record.confirmed_by = confirmed_by

    def register(self, name: str, *, confirmed_by: str) -> SkillCatalogEntry:
        """human-approved → registered：经 discovery.register 写入 project 目录
        并刷新 registry（§6.1 一等接缝在 discovery 内部）。

        失败 → 状态回 draft、staging 保留（可修后重试）；成功 → staging 残留
        清理（文件即真相已在 project 目录）。
        """
        record = self._require(name, STATUS_HUMAN_APPROVED,
                               action="register（需先 lint-pass + 人审确认）")
        # 直接解析真实 staging 文件（不是临时探针）：serialize_skill_markdown 写盘时
        # 会经 entry.load_body() 回读 source_path——必须是注册动作全程存活的路径。
        entry, parse_errors = parse_skill_markdown(self._staging_path(name))
        if entry is None:
            record.status = STATUS_DRAFT
            raise ValueError(f"staging draft '{name}' no longer parses: {parse_errors}")
        try:
            self._discovery.register(entry)
        except BaseException:
            record.status = STATUS_DRAFT  # 回 draft：可修后重试，staging 保留
            raise
        record.status = STATUS_REGISTERED
        record.confirmed_by = confirmed_by
        self._discard_file(name)
        # 返回刷新后 catalog 里的投影条目（source_path 指向 project 目录真相），
        # 而不是 staging 解析产物——staging 文件马上会被清理，别让调用方拿到
        # 指向已删路径的 entry。
        return next(
            (e for e in self._discovery.catalog().entries if e.name == entry.name), entry,
        )

    def discard(self, name: str) -> bool:
        """丢弃草稿（staging 目录删除 + 状态记录清除）。"""
        record = self._records.pop(name, None)
        self._discard_file(name)
        return record is not None

    def cleanup_staging(self) -> list[str]:
        """清空 staging 残留（§9-7）：包括内存记录之外的孤儿目录（重启残留）。"""
        removed = [name for name in self._records if self._discard_file(name)]
        self._records.clear()
        if self._staging_root.exists():
            for child in sorted(self._staging_root.iterdir()):
                if child.is_dir():
                    removed.append(child.name)
                    shutil.rmtree(child, ignore_errors=True)
        return sorted(set(removed))

    # ── 内部 ──

    def _project_owned(self, name: str, catalog_entries: list[SkillCatalogEntry]) -> bool:
        """既有同名条目是否落在 project skill 目录（闭环可写的更新目标）。

        global/manual 来源一律 False——闭环不写 global（§6.1），同名草稿只能
        被拒绝而不是悄悄 shadow 全局技能（lint 规则 5 的 shadowed 语义）。
        """
        project_dir = self._discovery.project_dir
        if project_dir is None:
            return False
        entry = next((e for e in catalog_entries if e.name == name), None)
        return entry is not None and resolve_within(entry.source_path, project_dir)

    def _parse_draft(self, draft_text: str) -> tuple[SkillCatalogEntry | None, list[str]]:
        staging_dir = self._staging_root / ".pending-parse"
        staging_dir.mkdir(parents=True, exist_ok=True)
        # 唯一探针名（审查 P2 处置）：固定 SKILL.md 在并发 propose 下写/读/删互踩，
        # 且 finally 的 rmdir 会撞出 OSError 掩盖真实异常——探针名唯一 + rmdir
        # 容错（空则顺手清，有并发探针则留下次清）。
        probe = staging_dir / f"SKILL.{uuid.uuid4().hex}.md"
        try:
            probe.write_text(draft_text, encoding="utf-8")
            return parse_skill_markdown(probe)
        finally:
            probe.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                staging_dir.rmdir()

    def _staging_path(self, name: str) -> Path:
        return self._staging_root / name / "SKILL.md"

    def _discard_file(self, name: str) -> bool:
        staging_dir = self._staging_root / name
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
            return True
        return False

    def _require(self, name: str, status: str, *, extra: set[str] | None = None,
                 action: str | None = None) -> DraftRecord:
        record = self._records.get(name)
        if record is None or not (record.status == status or (extra and record.status in extra)):
            current = record.status if record else "absent"
            detail = f"（当前状态：{current}）"
            if action:
                detail = f"{action} {detail}"
            raise ValueError(f"skill '{name}' 状态转移被拒绝：需要 {status}，{detail}")
        return record
