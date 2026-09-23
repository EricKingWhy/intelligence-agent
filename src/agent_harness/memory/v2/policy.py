"""#298 / MEM-V2-2：Formation 候选的**政策执法**（R4 / R5 / R6 / R7）。

`formation.py` 管**形状**（合不合契约），本模块管**政策**（合不合规矩）。分开是 R7 的直接
要求：秘密 / 敏感策略必须是**模型之外**的运行时边界，所以它只能长在"形状已经合法"的
对象上——如果政策塞进 schema，模型自查就成了唯一防线。

纯函数：输入是候选 + 本轮事件 + 一个运行时持有的同意信号，输出"接受哪些 / 拒绝哪些"。
不调模型、不读时钟、不写盘。四个 R 各自对应一段：

| R | 内容 | 本模块的执法点 |
| --- | --- | --- |
| R4 | 总 5 / Semantic ≤3 / Episodic ≤2 / Procedural ≤1，按 durable value 截断 | `_rank_and_cap` |
| R5 | Procedural 需两条独立事件，除非用户明确陈述规则 | `_meets_procedural_threshold` |
| R6 | USER/profile 事实需直接用户证据或显式确认 | `_has_user_authority` |
| R7 | 秘密一律拒；敏感需显式记住请求 | `find_secret` + `_reject` 前两条 |

# 三条需要登记的取舍

## 1. 秘密：运行时自带扫描器，不看模型自陈

`Sensitivity.SECRET` 是模型的**收紧**信号（它说是就拒）；但"模型说是 ordinary"不构成放行——
`find_secret` 是独立于模型的确定性扫描（前缀 / 结构）。这是 R7"独立于模型分类"在**秘密**
这一类上唯一站得住的落地方式。

## 2. 敏感：运行时**不自建** 9 类分类器（承认的能力边界）

§5.7.2 的 9 类敏感（健康 / 财务 / 证件 / 精确位置 / 生物特征 / 亲密 / 政治宗教 / 法律 / 未成年人）
**没有**运行时检测器，只有模型的 `sensitivity` 自陈。理由：词表不是分类器——一份关键词表
在这 9 类上既会大面积误报（正常记忆全被拒）又必然漏报，声称"运行时独立分类了"等于给自己
发一张假绿灯。所以这里执法的方向是**授权**而非**检测**：

- 同意信号（`explicit_remember`）由运行时持有，模型输出里**没有位置**能表达它；
- 自动形成路径上它恒为 False ⇒ 所有 `sensitive` 候选都被拒（AC3 的"unauthorized-sensitive"）；
- 形状层已经保证"带类目 ⇔ 自陈 sensitive"，所以模型无法用"打个类目再自称 ordinary"绕过。

"运行时对 9 类做独立检测"是一处**已知缺口**，登记在 ADR-0043，不在此处假装补齐。

## 3. 证据角色：回查事件，不信模型给的 `role`

`CandidateEvidence.role` 是模型自由填的字符串。用户事实的权威（R6）与 Procedural 的豁免（R5）
都必须建立在"这句话是用户说的"之上，所以角色由 `resolve_evidence_source` 按 `event_id`
回查事件类型判定；模型把工具结果标成 `role="user"` 不会让它变成用户证据。
回查不到的 `event_id` 一律按"无法确立权威"处理（fail closed）——它同样可能指向前几轮或
召回记忆，那两种情况下运行时也无法替它背书。

# 一个刻意的不对称：先政策、后名额

被政策拒掉的候选**不占** R4 的名额。否则两条带密钥的垃圾候选就能把三条合法记忆挤出去——
名额是给"可写入的内容"用的，不是给"模型输出的条数"用的。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from agent_harness.memory.extractor import _is_runtime_injected
from agent_harness.memory.v2.formation import FormationCandidate, Sensitivity
from agent_harness.memory.v2.types import (
    MemoryKind,
    MemoryTier,
    SemanticCategory,
    SemanticPayload,
)
from agent_harness.session import (
    MODEL_COMPLETED,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
    SessionEvent,
)

# --------------------------------------------------------------------------------------
# R4：候选上限（PRD §5.2.3）
# --------------------------------------------------------------------------------------

#: §5.2.3 的总量上限。注意四个数不是"3+2+1=6 里的子集"——总量 5 **严格小于**分档之和，
#: 所以两条约束都必须真的生效（`test_the_total_cap_binds_even_when_every_per_kind_cap_allows`
#: 就是钉这一点的）。
CANDIDATE_CAP_TOTAL = 5

#: §5.2.3 的分档上限。
CANDIDATE_CAP_BY_KIND: Mapping[MemoryKind, int] = MappingProxyType({
    MemoryKind.SEMANTIC: 3,
    MemoryKind.EPISODIC: 2,
    MemoryKind.PROCEDURAL: 1,
})


# --------------------------------------------------------------------------------------
# R7 前半：秘密扫描器（运行时自带，独立于模型分类）
# --------------------------------------------------------------------------------------


class SecretKind(str, Enum):
    """命中的秘密类型。值会进日志 / 事件，所以是稳定字符串而不是 `repr`。"""

    PRIVATE_KEY = "private_key"
    PROVIDER_TOKEN = "provider_token"
    BEARER_TOKEN = "bearer_token"
    JWT = "jwt"
    CREDENTIAL_URL = "credential_url"
    PASSWORD_ASSIGNMENT = "password_assignment"


#: 常见供应商 token 的**字面前缀**。用前缀而不是"高熵字符串"启发式：熵判据在 500 字符的
#: 短文本上几乎必然误报（任意被截断的哈希、base64 都会中），而过宽的误报在这里的代价是
#: **永久拒掉一条合法记忆**。
_TOKEN_PREFIXES = (
    "sk-", "sk_live_", "sk_test_", "pk_live_", "pk_test_", "rk_live_",
    "ghp_", "gho_", "ghs_", "ghu_", "github_pat_", "glpat-", "hf_",
    "xoxb-", "xoxp-", "xoxa-", "xoxr-", "xoxs-",
)

#: 顺序 = 优先归因顺序（先命中先返回）。**刻意不做熵启发式**（见 `_TOKEN_PREFIXES` 注释）。
_SECRET_PATTERNS: tuple[tuple[SecretKind, re.Pattern[str]], ...] = (
    (SecretKind.PRIVATE_KEY, re.compile(r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----")),
    (SecretKind.PRIVATE_KEY, re.compile(r"\bssh-(?:rsa|ed25519|dss)\s+[A-Za-z0-9+/=]{32,}")),
    (SecretKind.PROVIDER_TOKEN, re.compile(
        r"\b(?:" + "|".join(re.escape(prefix) for prefix in _TOKEN_PREFIXES) + r")"
        r"[A-Za-z0-9_\-]{8,}")),
    (SecretKind.PROVIDER_TOKEN, re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (SecretKind.PROVIDER_TOKEN, re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    (SecretKind.PROVIDER_TOKEN, re.compile(r"\bya29\.[0-9A-Za-z_\-]{20,}")),
    (SecretKind.CREDENTIAL_URL, re.compile(
        r"\b[a-z][a-z0-9+.\-]{1,20}://[^/\s:@]{1,64}:[^/\s:@]{1,64}@")),
    (SecretKind.JWT, re.compile(
        r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")),
    (SecretKind.BEARER_TOKEN, re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}")),
    (SecretKind.PASSWORD_ASSIGNMENT, re.compile(
        r"(?i)\b(?:password|passwd|pwd|passphrase|secret|api[_\- ]?key|"
        r"access[_\- ]?token|auth[_\- ]?token|client[_\- ]?secret|private[_\- ]?key)\b"
        r"\s*[:=]\s*\S{4,}")),
)


def find_secret(*texts: str) -> SecretKind | None:
    """在给定文本里找第一处秘密；找不到返回 `None`。

    接受多段文本（内容 / payload 字段 / 证据摘录）是为了一次扫完一条候选——
    逐面各扫一次会让"某个面忘了扫"变成一条静默的绕过路径（T4 组装模型输入时同样用它）。
    """
    for text in texts:
        if not text:
            continue
        for kind, pattern in _SECRET_PATTERNS:
            if pattern.search(text):
                return kind
    return None


# --------------------------------------------------------------------------------------
# 证据角色（R6 的地基）：回查事件，不信模型的 `role`
# --------------------------------------------------------------------------------------


class EvidenceSource(str, Enum):
    """一条证据在**运行时**眼里的来源。

    `OTHER` 同时承担两种情形：事件类型不在下表（如 `run/completed`），
    以及用户消息是 runtime 注入的样板（`injected_by`，沿用 V1 判定）。
    两者对 R6 / R5 的效果相同——都不构成"用户说过"。
    """

    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    OTHER = "other"


_EVENT_SOURCES: Mapping[str, EvidenceSource] = MappingProxyType({
    USER_MESSAGE: EvidenceSource.USER,
    MODEL_COMPLETED: EvidenceSource.ASSISTANT,
    TOOL_CALL: EvidenceSource.TOOL,
    TOOL_RESULT: EvidenceSource.TOOL,
})


def resolve_evidence_source(event: SessionEvent) -> EvidenceSource:
    """按事件类型判定角色；注入的用户消息降级为 `OTHER`。"""
    source = _EVENT_SOURCES.get(event.type, EvidenceSource.OTHER)
    if source is EvidenceSource.USER and _is_runtime_injected(event):
        return EvidenceSource.OTHER
    return source


# --------------------------------------------------------------------------------------
# 判定结果
# --------------------------------------------------------------------------------------


class PolicyRejection(str, Enum):
    """拒绝的稳定归因码（会进 job outcome / 事件，所以是稳定字符串）。"""

    SECRET = "secret"
    SENSITIVE_WITHOUT_CONSENT = "sensitive_without_consent"
    PROCEDURAL_THRESHOLD_NOT_MET = "procedural_threshold_not_met"
    USER_FACT_WITHOUT_USER_EVIDENCE = "user_fact_without_user_evidence"
    #: 名额不足（R4），**不是**对内容的否定判断——同一条候选在更空的批次里会被接受。
    OVER_CAP = "over_cap"


@dataclass(frozen=True, slots=True)
class RejectedCandidate:
    candidate: FormationCandidate
    reason: PolicyRejection


@dataclass(frozen=True, slots=True)
class CandidateSelection:
    """裁决后的候选集。两个字段合起来恰好覆盖输入的每一条（不重不漏）。"""

    #: 允许进入 adjudication 的候选，**按 durable value 降序**（同分保持模型顺序）。
    accepted: tuple[FormationCandidate, ...]
    rejected: tuple[RejectedCandidate, ...]


# --------------------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------------------


def durable_value(candidate: FormationCandidate) -> float:
    """R4 的"durable value"排序键。

    PRD 只要求"按 durable value 排序"，没有给它一个数值定义（§7.3 明确"精确排序公式"
    属实现自由）。这里取 `importance × strength`：两者都在候选里、都是 [0,1]，
    乘法让"很重要但没把握"排在"次重要但很确定"之后——这正是要裁掉的那一类。
    """
    return candidate.importance * candidate.strength


def select_candidates(
    candidates: Sequence[FormationCandidate], *,
    events: Iterable[SessionEvent],
    explicit_remember: bool = False,
) -> CandidateSelection:
    """政策执法 + 名额截断。

    `events` 是本轮（以及投影里可回查的）事件，用来把证据的 `event_id` 解析成运行时角色。
    **它是必填的关键字参数**：默认成空集合会让所有用户事实静默变成"没有用户证据"
    ——一处沉默的过严比一处沉默的过松更难发现，所以不给自己留这个口子。

    `explicit_remember` 是**运行时**持有的同意信号（`Remember X` 那条命令路径的产物，
    PRD §5.6.1）。自动形成路径上恒为 False。模型输出里没有任何字段能影响它。
    """
    sources: dict[str, EvidenceSource] = {
        event.event_id: resolve_evidence_source(event) for event in events
    }
    admissible: list[tuple[int, FormationCandidate]] = []
    rejected: list[RejectedCandidate] = []
    for index, candidate in enumerate(candidates):
        reason = _reject(candidate, sources, explicit_remember=explicit_remember)
        if reason is None:
            admissible.append((index, candidate))
        else:
            rejected.append(RejectedCandidate(candidate=candidate, reason=reason))

    accepted = _rank_and_cap(admissible, rejected)
    return CandidateSelection(accepted=tuple(accepted), rejected=tuple(rejected))


def _reject(
    candidate: FormationCandidate, sources: Mapping[str, EvidenceSource], *,
    explicit_remember: bool,
) -> PolicyRejection | None:
    """逐条判据，**顺序即优先级**（先命中先返回）。

    1. 秘密最优先：它不可被任何后续理由"兜住"，即使用户要求记住也不行（§5.7.1）——
       所以放在同意检查之前，否则"用户要求记住密钥"会先被记成敏感同意、再被放行。
    2. 敏感同意次之：这是"能不能写"的前置条件，写在内容规则之前更符合直觉。
    3. 用户事实权威：R6。
    4. Procedural 门槛：R5。
    """
    if candidate.sensitivity is Sensitivity.SECRET or _secret_in(candidate) is not None:
        return PolicyRejection.SECRET
    if candidate.sensitivity is Sensitivity.SENSITIVE and not explicit_remember:
        return PolicyRejection.SENSITIVE_WITHOUT_CONSENT
    if _is_user_fact(candidate) and not _has_user_authority(
        candidate, sources, explicit_remember
    ):
        return PolicyRejection.USER_FACT_WITHOUT_USER_EVIDENCE
    if candidate.kind is MemoryKind.PROCEDURAL and not _meets_procedural_threshold(
        candidate, sources
    ):
        return PolicyRejection.PROCEDURAL_THRESHOLD_NOT_MET
    return None


def _rank_and_cap(
    admissible: list[tuple[int, FormationCandidate]], rejected: list[RejectedCandidate],
) -> list[FormationCandidate]:
    """按 durable value 降序贪心取，直到任一条上限用尽（PRD §5.2.4 的"截断在裁决之前"）。

    贪心而非"先按档取再按总量砍"：后者会让一个高档位候选抢掉名额后又被整档裁掉，
    结果是**总量没满却丢了候选**。同分由 `sorted` 的稳定性保证沿用模型顺序。
    """
    accepted: list[FormationCandidate] = []
    used: dict[MemoryKind, int] = {}
    for _index, candidate in sorted(
        admissible, key=lambda item: -durable_value(item[1])
    ):
        if len(accepted) >= CANDIDATE_CAP_TOTAL or (
            used.get(candidate.kind, 0) >= CANDIDATE_CAP_BY_KIND[candidate.kind]
        ):
            rejected.append(RejectedCandidate(candidate=candidate, reason=PolicyRejection.OVER_CAP))
            continue
        accepted.append(candidate)
        used[candidate.kind] = used.get(candidate.kind, 0) + 1
    return accepted


# --------------------------------------------------------------------------------------
# 判据实现
# --------------------------------------------------------------------------------------


def _texts(candidate: FormationCandidate) -> Iterator[str]:
    """候选里所有会**落盘或被展示**的文本面。

    `project_id` 也在内：它是要写进记录的字段，密钥藏在那儿同样是泄漏。
    """
    yield candidate.content
    yield candidate.project_id or ""
    for value in candidate.payload.model_dump().values():
        if isinstance(value, str):
            yield value
    for item in candidate.evidence:
        yield item.excerpt


def _secret_in(candidate: FormationCandidate) -> SecretKind | None:
    return find_secret(*_texts(candidate))


def _is_user_fact(candidate: FormationCandidate) -> bool:
    """§4.4 的"USER/profile fact"在类型上的两种形态。

    profile 档位（§4.2 只承载 user_global 的 semantic）与 `category=profile` 的 semantic
    是同一件事的两个入口；只挡一个会让另一条路直通。
    """
    if candidate.kind is not MemoryKind.SEMANTIC:
        return False
    if candidate.tier is MemoryTier.PROFILE:
        return True
    payload = candidate.payload
    return (
        isinstance(payload, SemanticPayload)
        and payload.category is SemanticCategory.PROFILE
    )


def _has_user_authority(
    candidate: FormationCandidate, sources: Mapping[str, EvidenceSource], explicit_remember: bool,
) -> bool:
    """有直接用户证据，或有显式用户确认（`Remember X`）。

    回查不到的事件算"没有用户证据"：它可能是前几轮的、也可能是召回记忆，两种情况下
    运行时都无法确认那句话出自用户。宁可少记一条，不可替模型背书。
    """
    if explicit_remember:
        return True
    return any(
        sources.get(item.event_id) is EvidenceSource.USER for item in candidate.evidence
    )


def _meets_procedural_threshold(
    candidate: FormationCandidate, sources: Mapping[str, EvidenceSource],
) -> bool:
    """R5：两条**独立**事件，或用户明确陈述了规则。

    "独立"是运行时能确定性判定的那一半——数**不同**的 `event_id`。同一事件被引用两次
    不是两条事件，这是本条最容易漏掉的一面（用例专门钉它）。

    "成功 / 纠正"是另一半，运行时**判不了**：本仓 `tool/result` 不带 `ok`/`error` 字段，
    从事件形状看不出一次行动是否成功。把"读不出成功就算没成功"硬写成判据，会得到一条
    比 PRD 更弱的第二契约——所以那一半留给形成模型的判断，本模块只登记这条边界
    （ADR-0043），不假装已执法。
    """
    if any(sources.get(item.event_id) is EvidenceSource.USER for item in candidate.evidence):
        return True
    return len({item.event_id for item in candidate.evidence}) >= 2
