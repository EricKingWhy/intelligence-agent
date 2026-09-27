"""`#317` T9：stuck 暂停的三类恢复依据（ADR-0048 D7/D8）。

判据来源：`02 §5.3`（恢复前置：相关 steer / 环境或策略变更；**观测到的**，不是声明的）、
`03 §3.4` / `03 §5`（恢复请求的形状与 CAS；`resume_basis` 是声明，证据由服务端现算）。

三层都在这里：
- `agent/resume_evidence.py` 的两个摘要函数（环境 revision / 策略版本）与 steer 判据；
- `run_budget.stuck_resume_evidence`（**唯一**判据：哪条依据成立、缺哪条就 409）；
- `validate_resume` 的 stuck 分支（不要求点名 ceiling、但仍过 headroom）。

"用户说他改了环境"不构成证据这件事没有单独的用例：请求体里**根本没有**能放依据值的
字段（`resume_basis` 只是三条里的哪一条），所以"伪造"在形状上不可达。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_harness.agent.budget import BudgetConflict, BudgetRejection
from agent_harness.agent.resume_evidence import (
    MAX_REVISION_ENTRIES,
    ResumeEvidence,
    StuckEvidencePort,
    digest_policy_inputs,
    environment_revision,
    evidence_port,
    paused_policy_inputs,
    policy_inputs,
    policy_version_of,
    recorded_policy_inputs,
    relevant_steer_seq,
)
from agent_harness.agent.run_budget import (
    REASON_BUDGET_EXHAUSTED,
    REASON_STUCK,
    RESUME_BASIS_BUDGET_INCREASE,
    RESUME_BASIS_ENVIRONMENT_CHANGE,
    RESUME_BASIS_POLICY_CHANGE,
    RESUME_BASIS_RELEVANT_STEER,
    BudgetConsumed,
    PausedRun,
    RunLimits,
    describe_resume_requirements,
    stuck_resume_evidence,
    stuck_resume_requirements,
    validate_resume,
)
from agent_harness.session.event import (
    MODEL_CHANGED,
    RUN_PAUSED,
    STEER_REQUESTED,
    SessionEvent,
)

RUN_ID = "run-stuck"


def _paused(
    *, reason: str = REASON_STUCK, pause_seq: int = 7,
    stuck: dict | None = None, consumed_turns: int = 4,
) -> PausedRun:
    return PausedRun(
        run_id=RUN_ID, pause_seq=pause_seq, step_id=6, reason=reason,
        trigger_dimension="stuck.tool_failure_loop", version=2,
        consumed=BudgetConsumed(agent_turns=consumed_turns, model_requests=8),
        limits=RunLimits(max_agent_turns_total=20),
        local_fuse=None, continuation={}, closeout_source="deterministic",
        resume_requirements=(), stuck=stuck,
    )


#: 快照里那一套策略面。`policy_version` 由它算出——**两格同源是载荷合法的前提**
#: （`recorded_policy_inputs`：逐维值必须能重算出同一格的摘要），所以夹具不手抄摘要。
DEFAULT_POLICY_INPUTS: dict = {
    "permission_mode": "workspace-write", "model": None,
    "agent_profile": "coding", "reasoning_effort": None, "context_providers": None,
}


def _stuck_payload(**overrides) -> dict:
    inputs = overrides.pop("policy_inputs", None)
    recorded = dict(DEFAULT_POLICY_INPUTS)
    if inputs is not None:
        recorded.update(inputs)
    payload = {
        "pattern": "stuck.tool_failure_loop", "threshold": 3, "count": 6,
        "replan_count": 1, "fingerprint": "sha256:abc",
        "environment_revision": "sha256:env-old",
        "policy_version": digest_policy_inputs(recorded),
        "policy_inputs": recorded,
    }
    payload.update(overrides)
    return payload


def _steer(seq: int, *, run_id: str | None = RUN_ID, content: str = "换个做法") -> SessionEvent:
    return SessionEvent(
        seq=seq, type=STEER_REQUESTED,
        data={"steer_id": f"s{seq}", "content": content}, run_id=run_id,
    )


# ── 摘要函数 ──────────────────────────────────────────────────────────────


class TestEnvironmentRevision:
    """摘要 = 相对路径 + 类型 + 尺寸 + mtime_ns（**不含内容**，见 ADR-0048 残余 2）。

    用例显式给 mtime，不靠挂钟：Windows 的文件时间粒度约 15.6ms，同一 tick 内的两次
    写入会得到同一个 `st_mtime_ns`——那是"看不见变化"（fail-closed：恢复侧报 409），
    不是"误报变化"（那才是要防的方向）。
    """

    def _stamp(self, path: Path, *, mtime_ns: int) -> None:
        os.utime(path, ns=(mtime_ns, mtime_ns))

    def test_the_same_tree_gives_the_same_revision(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.txt").write_text("world", encoding="utf-8")
        first = environment_revision(tmp_path)
        assert first is not None
        assert first == environment_revision(tmp_path)
        assert first.startswith("sha256:")

    def test_a_new_file_changes_it(self, tmp_path: Path) -> None:
        first = environment_revision(tmp_path)
        (tmp_path / "new.txt").write_text("x", encoding="utf-8")
        assert environment_revision(tmp_path) != first

    def test_changed_content_changes_it(self, tmp_path: Path) -> None:
        target = tmp_path / "a.txt"
        target.write_text("one", encoding="utf-8")
        self._stamp(target, mtime_ns=1_700_000_000_000_000_000)
        first = environment_revision(tmp_path)
        target.write_text("two", encoding="utf-8")
        self._stamp(target, mtime_ns=1_700_000_001_000_000_000)
        assert environment_revision(tmp_path) != first

    def test_removed_content_changes_it(self, tmp_path: Path) -> None:
        target = tmp_path / "a.txt"
        target.write_text("one", encoding="utf-8")
        first = environment_revision(tmp_path)
        target.unlink()
        assert environment_revision(tmp_path) != first

    def test_an_unreadable_root_is_not_a_revision(self, tmp_path: Path) -> None:
        """读不到 ⇒ `None`（fail-closed：恢复侧按"无快照可比"拒，而不是当成"变了"）。"""
        assert environment_revision(tmp_path / "missing") is None
        assert environment_revision(tmp_path / "a.txt") is None
        empty = tmp_path / "empty"
        empty.mkdir()
        assert environment_revision(empty) is not None  # 空目录也是**可读的**状态
        assert environment_revision(empty) != environment_revision(tmp_path)

    def test_truncation_is_folded_into_the_digest(self, tmp_path: Path, monkeypatch) -> None:
        """条目超过上限 ⇒ 摘要里折进"已截断"，不是静默截断成"看起来一样"。"""
        for index in range(4):
            (tmp_path / f"f{index}.txt").write_text(str(index), encoding="utf-8")
        full = environment_revision(tmp_path)
        monkeypatch.setattr(
            "agent_harness.agent.resume_evidence.MAX_REVISION_ENTRIES", 2,
        )
        capped = environment_revision(tmp_path)
        assert capped != full, "截断必须改变摘要（否则超大目录的变化会被静默吞掉）"
        assert MAX_REVISION_ENTRIES >= 1


class TestPolicyVersion:
    def test_the_same_inputs_give_the_same_version(self) -> None:
        assert policy_version_of(permission_mode="workspace-write", model="m") == (
            policy_version_of(permission_mode="workspace-write", model="m")
        )

    def test_each_policy_input_moves_it(self) -> None:
        base = policy_version_of(permission_mode="workspace-write", model="m")
        assert policy_version_of(permission_mode="danger-full-access", model="m") != base
        assert policy_version_of(permission_mode="workspace-write", model="other") != base
        assert policy_version_of(
            permission_mode="workspace-write", model="m", agent_profile="p",
        ) != base
        assert policy_version_of(
            permission_mode="workspace-write", model="m", reasoning_effort="high",
        ) != base
        assert policy_version_of(
            permission_mode="workspace-write", model="m", context_providers=["a"],
        ) != base

    def test_provider_order_is_not_a_policy_change(self) -> None:
        assert policy_version_of(context_providers=["a", "b"]) == (
            policy_version_of(context_providers=["b", "a"])
        )

    def test_ceiling_dimensions_are_not_in_the_digest(self) -> None:
        """ceiling 类不入摘要（ADR-0048 D8）：否则抬一格 fuse 就能自己造出依据。

        `policy_version_of` 的签名里没有这些参数——从外部看就是"喂不进摘要"。
        """
        import inspect

        parameters = set(inspect.signature(policy_version_of).parameters)
        assert not parameters & {
            "max_agent_turns", "local_fuse_source", "max_total_tokens",
            "max_cost_usd", "deadline_at", "auto_approve",
        }


class TestRelevantSteer:
    def test_no_steer_after_the_pause_is_no_evidence(self) -> None:
        events = [_steer(3), _steer(5)]
        assert relevant_steer_seq(events, run_id=RUN_ID, pause_seq=7) is None

    def test_a_later_steer_for_this_run_counts(self) -> None:
        events = [_steer(3), _steer(9), _steer(11)]
        assert relevant_steer_seq(events, run_id=RUN_ID, pause_seq=7) == 11

    def test_a_session_level_steer_does_not_count(self) -> None:
        """`run_id=None` 的"会话级" steer **不算**依据（T9 审查 P2 的口径订正）。

        运行时 `_applicable_steers` 把它当陈旧 steer 丢弃（只认 run_id 相同），它只在
        run 走进**终态**时被当普通输入投递——stuck 暂停不是终态，所以那种 steer 这一次
        根本到不了模型：算成依据等于"没有新输入也放行"。两处现在共用
        `steer_applies_to_run`，所以这里与运行时同判。
        """
        from agent_harness.agent.resume_evidence import steer_applies_to_run

        assert relevant_steer_seq(
            [_steer(8, run_id=None)], run_id=RUN_ID, pause_seq=7,
        ) is None
        assert steer_applies_to_run(None, RUN_ID) is False
        assert steer_applies_to_run(RUN_ID, RUN_ID) is True
        assert steer_applies_to_run("run-other", RUN_ID) is False
        # 运行时的 run_id 恒非 None（`_drive` 里由 run/started 给）：两侧同判意味着
        # "谁也别想用一个没主儿的 steer 把恢复放行"。
        assert steer_applies_to_run(None, None) is False

    def test_another_runs_steer_does_not_count(self) -> None:
        assert relevant_steer_seq(
            [_steer(8, run_id="run-other")], run_id=RUN_ID, pause_seq=7,
        ) is None

    def test_the_boundary_is_strictly_after_the_pause(self) -> None:
        assert relevant_steer_seq([_steer(7)], run_id=RUN_ID, pause_seq=7) is None


class TestEvidencePort:
    def test_current_observes_both_values(self, tmp_path: Path) -> None:
        port = StuckEvidencePort(
            workspace=tmp_path, policy_version=policy_version_of(model="m"),
        )
        evidence = port.current()
        assert evidence.environment_revision == environment_revision(tmp_path)
        assert evidence.policy_version == policy_version_of(model="m")

    def test_an_unreadable_workspace_is_not_a_failure(self, tmp_path: Path) -> None:
        """端口故障不得变成异常（ADR-0048 D9）：读不到就是 `None`。"""
        port = StuckEvidencePort(workspace=tmp_path / "gone", policy_version="sha256:x")
        evidence = port.current()
        assert evidence.environment_revision is None
        assert evidence.policy_version == "sha256:x"


class TestPolicyInputsAreRecordedAndRestored:
    """`#317` T9 审查 P1：逐维值与摘要**同源**，恢复侧据此还原暂停时那一套策略。

    病灶：暂停侧记的是"那次执行生效的策略面"，恢复侧却只拿本次请求声明的 amend 重算
    ——两次请求的字段集合不同（CLI 的 resume 一个策略字段都不声明），摘要必不同，于是
    `policy_change` 在"其实什么都没变"的恢复上成立（fail-open）。修法是把逐维值也记进
    快照，恢复侧先还原再算。
    """

    def test_the_recorded_inputs_reproduce_the_recorded_digest(self) -> None:
        """`digest_policy_inputs(policy_inputs(...)) == policy_version_of(...)`。"""
        inputs = policy_inputs(
            permission_mode="danger-full-access", model="m", agent_profile="coding",
            reasoning_effort="deep", context_providers=["web", "memory"],
        )
        assert digest_policy_inputs(inputs) == policy_version_of(
            permission_mode="danger-full-access", model="m", agent_profile="coding",
            reasoning_effort="deep", context_providers=["memory", "web"],
        )
        port = evidence_port(
            workspace=None, permission_mode="workspace-write",
            model="m", agent_profile="coding",
        )
        evidence = port.current()
        assert evidence.policy_inputs is not None
        assert digest_policy_inputs(evidence.policy_inputs) == evidence.policy_version

    def test_the_pause_snapshot_is_read_back_as_values(self) -> None:
        """`paused_policy_inputs` 只认同 run 的 `run/paused`，返回的是**值**不是摘要。"""
        events = [
            SessionEvent(
                seq=9, type=RUN_PAUSED, session_id="s1", run_id=RUN_ID,
                data={
                    "reason": REASON_STUCK,
                    "stuck": _stuck_payload(
                        policy_inputs={"agent_profile": "coding"},
                    ),
                },
            ),
            SessionEvent(
                seq=10, type=RUN_PAUSED, session_id="s1", run_id="run-other",
                data={
                    "reason": REASON_STUCK,
                    "stuck": _stuck_payload(policy_inputs={"agent_profile": "main"}),
                },
            ),
        ]
        assert paused_policy_inputs(events, run_id=RUN_ID) == {
            "permission_mode": "workspace-write", "model": None, "agent_profile": "coding",
            "reasoning_effort": None, "context_providers": None,
        }
        assert paused_policy_inputs(events, run_id="run-nope") is None

    def test_a_second_pause_is_the_restore_source(self) -> None:
        """同一个 run 暂停两次 ⇒ 还原源是**最后一条**（与判据的基线同一条）。

        `#317` T9 二轮审查 P1：取第一条会让两侧比两个不同的快照——"什么都没改"照样能靠
        两个快照之差拿到依据（fail-open），而恢复腿会跑回**第一次**暂停的那套档位。
        """
        events = [
            SessionEvent(
                seq=9, type=RUN_PAUSED, session_id="s1", run_id=RUN_ID,
                data={"reason": REASON_STUCK,
                      "stuck": _stuck_payload(policy_inputs={"agent_profile": "coding"})},
            ),
            SessionEvent(
                seq=20, type=RUN_PAUSED, session_id="s1", run_id=RUN_ID,
                data={"reason": REASON_STUCK,
                      "stuck": _stuck_payload(policy_inputs={"agent_profile": "main"})},
            ),
        ]
        recorded = paused_policy_inputs(events, run_id=RUN_ID)
        assert recorded is not None and recorded["agent_profile"] == "main"

    def test_the_snapshot_must_reproduce_its_own_digest(self) -> None:
        """`recorded_policy_inputs`：形状合法 **且** 逐维值能重算出同一格的摘要。"""
        good = _stuck_payload()
        assert recorded_policy_inputs(good) == DEFAULT_POLICY_INPUTS
        # 摘要缺席 / 逐维值缺席 / 两格对不上 / 形状非法（list 当 profile）⇒ 都不可用
        assert recorded_policy_inputs(_stuck_payload(policy_version=None)) is None
        assert recorded_policy_inputs({"policy_version": "sha256:x"}) is None
        assert recorded_policy_inputs(
            _stuck_payload(policy_version="sha256:other"),
        ) is None
        assert recorded_policy_inputs(
            _stuck_payload(policy_inputs={"agent_profile": ["coding"]}),
        ) is None
        assert recorded_policy_inputs(None) is None

    def test_none_and_an_empty_provider_list_are_different_policy_faces(self) -> None:
        """`None`（未声明 ⇒ 装配全部 wired）与 `[]`（显式零 provider）是两种策略面。

        T9 二轮审查 P2：归一化成同一个值会让"从全量改成零"看上去没变，而把 `None` 还原成
        `[]` 会把恢复腿的 provider 丢光（暂停腿 3 个 wired ⇒ 恢复腿 0 个）。
        """
        default = policy_inputs(permission_mode="workspace-write")
        explicit = policy_inputs(permission_mode="workspace-write", context_providers=[])
        assert default["context_providers"] is None
        assert explicit["context_providers"] == []
        assert digest_policy_inputs(default) != digest_policy_inputs(explicit)

        from agent_harness.session.amend import AmendOptions
        from agent_harness.session.model_switch import restore_policy_inputs

        # 请求没声明 providers ⇒ 沿用快照：`None` 仍是 `None`（**不是** `[]`）。
        assert restore_policy_inputs(
            AmendOptions(), default, [],
        ).context_providers is None
        assert restore_policy_inputs(
            AmendOptions(), explicit, [],
        ).context_providers == []

    def test_a_budget_pause_has_no_policy_inputs_to_read_back(self) -> None:
        """非 stuck 暂停的载荷里没有这一格 ⇒ `None`（恢复侧没有可还原的东西）。"""
        events = [
            SessionEvent(
                seq=4, type=RUN_PAUSED, session_id="s1", run_id=RUN_ID,
                data={"reason": REASON_BUDGET_EXHAUSTED},
            ),
        ]
        assert paused_policy_inputs(events, run_id=RUN_ID) is None

    def test_a_request_that_declares_nothing_keeps_the_paused_policy(self) -> None:
        """空 amend ⇒ 还原成暂停时那一套 ⇒ 摘要相等 ⇒ `policy_change` 不成立。"""
        from agent_harness.session.amend import AmendOptions
        from agent_harness.session.model_switch import restore_policy_inputs

        recorded = {
            "permission_mode": "workspace-write", "model": "m",
            "agent_profile": "coding", "reasoning_effort": "deep",
            "context_providers": [],
        }
        restored = restore_policy_inputs(None, recorded, [])
        assert restored == AmendOptions(
            model="m", agent_profile="coding", reasoning_effort="deep",
            context_providers=[],
        )
        assert policy_version_of(
            permission_mode="workspace-write", model=restored.model,
            agent_profile=restored.agent_profile,
            reasoning_effort=restored.reasoning_effort,
            context_providers=restored.context_providers,
        ) == policy_version_of(
            permission_mode="workspace-write", model="m", agent_profile="coding",
            reasoning_effort="deep", context_providers=[],
        )

    def test_a_declared_input_wins_over_the_snapshot(self) -> None:
        """请求显式声明的维度优先——那才是用户真实的策略变更。"""
        from agent_harness.session.amend import AmendOptions
        from agent_harness.session.model_switch import restore_policy_inputs

        restored = restore_policy_inputs(
            AmendOptions(agent_profile="main"),
            {"model": "m", "agent_profile": "coding", "reasoning_effort": "deep"},
            [],
        )
        assert restored.agent_profile == "main"  # 声明优先
        assert restored.model == "m"  # 没声明的沿用快照
        assert restored.reasoning_effort == "deep"

    def test_a_session_model_switch_wins_over_the_snapshot(self) -> None:
        """暂停之后切过模型 ⇒ 快照里的模型不许盖掉它（那一跳正是合法的策略依据）。"""
        from agent_harness.session.amend import AmendOptions
        from agent_harness.session.model_switch import restore_policy_inputs

        switched = [
            SessionEvent(
                seq=12, type=MODEL_CHANGED, session_id="s1",
                data={"to_provider": "deepseek", "to_model_id": "m-new"},
            ),
        ]
        restored = restore_policy_inputs(
            AmendOptions(), {"model": "m-old", "agent_profile": "coding"}, switched,
        )
        # model 留在 None：让 `amend_with_session_model` 用会话派生的 m-new 补上。
        assert restored.model is None
        assert restored.agent_profile == "coding"


# ── 判据：`stuck_resume_evidence` ────────────────────────────────────────


class TestStuckResumeEvidence:
    def test_a_budget_pause_has_no_evidence_object(self) -> None:
        """预算 / deadline 暂停的恢复没有"依据"这一格：返回 `None`，载荷逐字不变。"""
        assert stuck_resume_evidence(
            _paused(reason=REASON_BUDGET_EXHAUSTED),
            resume_basis=RESUME_BASIS_BUDGET_INCREASE,
            evidence=ResumeEvidence(),
        ) is None

    def test_budget_increase_is_rejected_for_a_stuck_pause(self) -> None:
        with pytest.raises(BudgetConflict, match="budget_increase"):
            stuck_resume_evidence(
                _paused(stuck=_stuck_payload()),
                resume_basis=RESUME_BASIS_BUDGET_INCREASE,
                evidence=ResumeEvidence(environment_revision="sha256:env-new"),
            )

    def test_missing_evidence_is_rejected(self) -> None:
        with pytest.raises(BudgetConflict, match="没有可用的"):
            stuck_resume_evidence(
                _paused(stuck=_stuck_payload()),
                resume_basis=RESUME_BASIS_RELEVANT_STEER, evidence=None,
            )

    def test_relevant_steer_is_accepted_and_recorded(self) -> None:
        recorded = stuck_resume_evidence(
            _paused(stuck=_stuck_payload(), pause_seq=7),
            resume_basis=RESUME_BASIS_RELEVANT_STEER,
            evidence=ResumeEvidence(relevant_steer_seq=9),
        )
        assert recorded == {
            "basis": RESUME_BASIS_RELEVANT_STEER, "steer_seq": 9, "pause_seq": 7,
        }

    def test_relevant_steer_without_a_steer_is_rejected(self) -> None:
        with pytest.raises(BudgetConflict, match="没有"):
            stuck_resume_evidence(
                _paused(stuck=_stuck_payload(), pause_seq=7),
                resume_basis=RESUME_BASIS_RELEVANT_STEER,
                evidence=ResumeEvidence(relevant_steer_seq=None),
            )

    def test_an_unchanged_environment_is_rejected(self) -> None:
        with pytest.raises(BudgetConflict, match="相同"):
            stuck_resume_evidence(
                _paused(stuck=_stuck_payload(environment_revision="sha256:env-old")),
                resume_basis=RESUME_BASIS_ENVIRONMENT_CHANGE,
                evidence=ResumeEvidence(environment_revision="sha256:env-old"),
            )

    def test_a_changed_environment_is_accepted(self) -> None:
        recorded = stuck_resume_evidence(
            _paused(stuck=_stuck_payload(environment_revision="sha256:env-old")),
            resume_basis=RESUME_BASIS_ENVIRONMENT_CHANGE,
            evidence=ResumeEvidence(environment_revision="sha256:env-new"),
        )
        assert recorded == {
            "basis": RESUME_BASIS_ENVIRONMENT_CHANGE,
            "environment_revision": "sha256:env-new", "recorded": "sha256:env-old",
        }

    def test_a_missing_snapshot_is_rejected_not_assumed_changed(self) -> None:
        """暂停时没读到快照 ⇒ 该依据不可用（fail-closed），不是"当作变了"放行。"""
        with pytest.raises(BudgetConflict, match="无快照可比"):
            stuck_resume_evidence(
                _paused(stuck=_stuck_payload(policy_version=None)),
                resume_basis=RESUME_BASIS_POLICY_CHANGE,
                evidence=ResumeEvidence(policy_version="sha256:pol-new"),
            )

    def test_a_missing_observation_is_rejected(self) -> None:
        with pytest.raises(BudgetConflict, match="缺席"):
            stuck_resume_evidence(
                _paused(stuck=_stuck_payload()),
                resume_basis=RESUME_BASIS_ENVIRONMENT_CHANGE,
                evidence=ResumeEvidence(environment_revision=None),
            )

    def test_an_unchanged_policy_is_rejected(self) -> None:
        stuck = _stuck_payload()
        with pytest.raises(BudgetConflict, match="相同"):
            stuck_resume_evidence(
                _paused(stuck=stuck),
                resume_basis=RESUME_BASIS_POLICY_CHANGE,
                evidence=ResumeEvidence(policy_version=stuck["policy_version"]),
            )

    def test_a_changed_policy_is_accepted(self) -> None:
        recorded = stuck_resume_evidence(
            _paused(stuck=_stuck_payload()),
            resume_basis=RESUME_BASIS_POLICY_CHANGE,
            evidence=ResumeEvidence(policy_version="sha256:pol-new"),
        )
        assert recorded["basis"] == RESUME_BASIS_POLICY_CHANGE
        assert recorded["policy_version"] == "sha256:pol-new"

    def test_a_snapshot_without_per_dimension_inputs_is_rejected(self) -> None:
        """存量快照（本票之前写入的 JSONL：只记了摘要）⇒ 还原不回来 ⇒ fail-closed。

        `#317` T9 二轮审查 P1 的回归。照旧"现算再比"就是拿本次请求声明了什么当基线：
        CLI 一类不声明策略的恢复会把"省略"算成"变了"（原始 fail-open 在存量会话上复活）。
        """
        legacy = _stuck_payload(policy_inputs=None, policy_version="sha256:pol-legacy")
        legacy.pop("policy_inputs")
        assert legacy["policy_version"] == "sha256:pol-legacy"  # 摘要还在
        with pytest.raises(BudgetConflict, match="还原不回来"):
            stuck_resume_evidence(
                _paused(stuck=legacy),
                resume_basis=RESUME_BASIS_POLICY_CHANGE,
                evidence=ResumeEvidence(policy_version="sha256:pol-new"),
            )

    def test_a_snapshot_whose_inputs_do_not_reproduce_the_digest_is_rejected(self) -> None:
        """逐维值与摘要不同源（手改 / 跨算法漂移）⇒ 同样"还原不回来"。"""
        with pytest.raises(BudgetConflict, match="还原不回来"):
            stuck_resume_evidence(
                _paused(stuck=_stuck_payload(policy_version="sha256:pol-other")),
                resume_basis=RESUME_BASIS_POLICY_CHANGE,
                evidence=ResumeEvidence(policy_version="sha256:pol-new"),
            )

    def test_a_malformed_snapshot_is_a_conflict_not_a_crash(self) -> None:
        """逐维值形状非法（profile 被写成 list）⇒ 409，**不是** 500（T9 二轮审查 P2）。

        旧写法把快照值直接塞进 amend 再算摘要，`agent_profile=["not-a-string"]` 一路撞到
        `TypeError: unhashable type: 'list'`——恢复路径以异常收场，而不是以冲突收场。
        """
        malformed = _stuck_payload(policy_inputs={"agent_profile": ["not-a-string"]})
        # "不是 500"这条由 `pytest.raises` 自己承重：换任何别的异常类型本用例都红。
        with pytest.raises(BudgetConflict, match="还原不回来"):
            stuck_resume_evidence(
                _paused(stuck=malformed),
                resume_basis=RESUME_BASIS_POLICY_CHANGE,
                evidence=ResumeEvidence(policy_version="sha256:pol-new"),
            )

    @pytest.mark.parametrize(
        "malformed",
        [["sha256:env-old"], {"revision": "sha256:env-old"}, 7, "", 0],
        ids=["list", "dict", "int", "empty", "zero"],
    )
    def test_a_malformed_environment_cell_is_not_evidence(self, malformed) -> None:
        """环境格形状非法 ⇒ **不算依据**，清单也不许列它（`#317` 三轮审查 P3）。

        旧写法只判 `is not None`，所以"存在即已变"：一份被手改过的 JSONL 只要把那一格写成
        任何非空值（哪怕是 `["sha256:env-old"]`），`environment_change` 就无条件放行——
        恢复侧什么都没观测到，却被判成"环境变了"（fail-open），比 409 更坏。
        """
        stuck = _stuck_payload(environment_revision=malformed)
        assert RESUME_BASIS_ENVIRONMENT_CHANGE not in stuck_resume_requirements(stuck)
        assert not _admitted(
            stuck, RESUME_BASIS_ENVIRONMENT_CHANGE, _favorable_evidence(),
        )


# ── 依据可用性：清单必须与判据同源（`#317` T9 二轮审查 P2）────────────────


def _favorable_evidence() -> ResumeEvidence:
    """把三条依据都喂到嘴边的观测：只要快照那一格在，判据就该接受它。"""
    newer = {**DEFAULT_POLICY_INPUTS, "model": "other-model"}
    return ResumeEvidence(
        relevant_steer_seq=99,
        environment_revision="sha256:env-new",
        policy_version=digest_policy_inputs(newer),
        policy_inputs=newer,
    )


def _admitted(stuck: dict, basis: str, evidence: ResumeEvidence) -> bool:
    try:
        stuck_resume_evidence(_paused(stuck=stuck), resume_basis=basis, evidence=evidence)
    except BudgetConflict:
        return False
    return True


ALL_BASES = (
    RESUME_BASIS_RELEVANT_STEER,
    RESUME_BASIS_ENVIRONMENT_CHANGE,
    RESUME_BASIS_POLICY_CHANGE,
)


class TestAvailableResumeRequirements:
    """`resume_requirements` 列的是**判据不会恒拒**的依据。

    这一族钉的是"列一条永远 409 的依据"这个形状（`03 §5` / ADR-0044 D4 禁止的
    "暗示可安全续跑"；ADR-0048 D6）。**两个方向**都要成立：列了的真被接受，没列的真被拒
    ——只查一个方向，判据收紧而清单没跟（或反之）就看不见。
    """

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda stuck: None, id="full_snapshot"),
            pytest.param(
                lambda stuck: stuck.pop("environment_revision"), id="no_env_cell",
            ),
            pytest.param(
                lambda stuck: stuck.update(policy_version=None, policy_inputs=None),
                id="no_policy_cells",
            ),
            pytest.param(
                lambda stuck: stuck.pop("policy_inputs"), id="legacy_no_inputs",
            ),
            # 畸形快照（`#317` 三轮审查 P3）：`"存在即已变"` 会让任何非字符串值无条件放行，
            # 所以形状规则与 `recorded_policy_inputs` 同源——两侧都当"这一格没有"。
            pytest.param(
                lambda stuck: stuck.update(environment_revision=["sha256:env-old"]),
                id="malformed_env_cell",
            ),
            pytest.param(
                lambda stuck: stuck.update(environment_revision=""),
                id="empty_env_cell",
            ),
        ],
    )
    def test_the_advertised_bases_are_exactly_the_acceptable_ones(self, mutate) -> None:
        """列出来的每条都要**真被接受**，没列的一条都要**真被拒**（清单 = 判据的可用子集）。

        两侧跑的是同一份快照与同一组"喂到嘴边"的观测，所以这条等价关系是机械可判的；
        任何一侧单独改动（判据收紧而清单没跟、或清单多列一条）都会让本用例红。
        """
        stuck = _stuck_payload()
        mutate(stuck)
        advertised = stuck_resume_requirements(stuck)
        for basis in ALL_BASES:
            assert _admitted(stuck, basis, _favorable_evidence()) == (
                basis in advertised
            ), f"basis={basis}、清单={advertised}、快照={stuck}"

    def test_a_full_snapshot_offers_all_three(self) -> None:
        assert stuck_resume_requirements(_stuck_payload()) == ALL_BASES

    def test_a_legacy_snapshot_drops_only_the_policy_basis(self) -> None:
        """只记了摘要的存量快照（ADR-0048 残余 11）：环境那条照常可用。"""
        legacy = _stuck_payload()
        legacy.pop("policy_inputs")
        assert stuck_resume_requirements(legacy) == (
            RESUME_BASIS_RELEVANT_STEER,
            RESUME_BASIS_ENVIRONMENT_CHANGE,
        )

    def test_a_non_stuck_pause_has_no_requirements(self) -> None:
        """预算 / deadline 暂停没有前置依据（`03 §3.4`：非空只出现在 stuck）。"""
        assert stuck_resume_requirements(None) == ()
        assert stuck_resume_requirements({}) == ()

    def test_the_human_readable_list_is_rendered_from_the_same_set(self) -> None:
        """确定性 continuation 与 closeout 指令共用这一份渲染（`describe_resume_requirements`）。

        指令里写死三条，模型会照抄进 continuation；渲染自同一份可用集合，两处就不可能
        各说一套。
        """
        full = describe_resume_requirements(_stuck_payload())
        for basis in ALL_BASES:
            assert basis in full
        legacy = _stuck_payload()
        legacy.pop("policy_inputs")
        partial = describe_resume_requirements(legacy)
        assert RESUME_BASIS_ENVIRONMENT_CHANGE in partial
        assert RESUME_BASIS_POLICY_CHANGE not in partial
        assert "没有快照" in describe_resume_requirements(None)


# ── 判定：`validate_resume` 的 stuck 分支 ────────────────────────────────

class TestValidateResume:
    def test_stuck_resume_does_not_require_an_absolute_ceiling(self) -> None:
        """stuck 缺的不是额度：不点名 ceiling 也合法（沿用暂停快照）。"""
        effective = validate_resume(
            _paused(stuck=_stuck_payload()), run_id=RUN_ID, expected_version=2,
            limits=RunLimits(), resume_basis=RESUME_BASIS_RELEVANT_STEER,
            resume_evidence=ResumeEvidence(relevant_steer_seq=9),
        )
        assert effective.max_agent_turns_total == 20  # 沿用暂停时的值

    def test_stuck_resume_still_checks_headroom(self) -> None:
        """恢复了却立刻再停 ⇒ 409（比一次假恢复诚实）。"""
        with pytest.raises(BudgetConflict, match="放不下"):
            validate_resume(
                _paused(stuck=_stuck_payload(), consumed_turns=20),
                run_id=RUN_ID, expected_version=2, limits=RunLimits(),
                resume_basis=RESUME_BASIS_RELEVANT_STEER,
                resume_evidence=ResumeEvidence(relevant_steer_seq=9),
            )

    def test_a_stuck_pause_rejects_a_budget_increase_basis(self) -> None:
        with pytest.raises(BudgetConflict, match="budget_increase"):
            validate_resume(
                _paused(stuck=_stuck_payload()), run_id=RUN_ID, expected_version=2,
                limits=RunLimits(max_agent_turns_total=99),
                resume_basis=RESUME_BASIS_BUDGET_INCREASE,
                resume_evidence=ResumeEvidence(relevant_steer_seq=9),
            )

    def test_a_budget_pause_still_rejects_the_three_change_bases(self) -> None:
        """预算暂停不假装校验过"变更依据"（那是 stuck 的责任域）。"""
        with pytest.raises(BudgetConflict, match="只接受"):
            validate_resume(
                _paused(reason=REASON_BUDGET_EXHAUSTED), run_id=RUN_ID,
                expected_version=2, limits=RunLimits(max_agent_turns_total=99),
                resume_basis=RESUME_BASIS_RELEVANT_STEER,
                resume_evidence=ResumeEvidence(relevant_steer_seq=9),
            )

    def test_an_unknown_resume_basis_is_a_shape_error(self) -> None:
        with pytest.raises(BudgetRejection, match="未知 resume_basis"):
            validate_resume(
                _paused(stuck=_stuck_payload()), run_id=RUN_ID, expected_version=2,
                limits=RunLimits(), resume_basis="whatever",
            )

    def test_a_stale_version_is_a_conflict(self) -> None:
        with pytest.raises(BudgetConflict, match="过期"):
            validate_resume(
                _paused(stuck=_stuck_payload()), run_id=RUN_ID, expected_version=1,
                limits=RunLimits(), resume_basis=RESUME_BASIS_RELEVANT_STEER,
                resume_evidence=ResumeEvidence(relevant_steer_seq=9),
            )

    def test_a_deadline_pause_still_needs_a_future_instant(self) -> None:
        """deadline 暂停的既有语义没被 stuck 改动（`#315` 回归面）。"""
        from agent_harness.agent.run_budget import REASON_DEADLINE

        paused = _paused(reason=REASON_DEADLINE)
        with pytest.raises(BudgetConflict):
            validate_resume(
                paused, run_id=RUN_ID, expected_version=2,
                limits=RunLimits(deadline_at=datetime.now(UTC) - timedelta(minutes=1)),
                resume_basis=RESUME_BASIS_BUDGET_INCREASE,
            )
        effective = validate_resume(
            paused, run_id=RUN_ID, expected_version=2,
            limits=RunLimits(deadline_at=datetime.now(UTC) + timedelta(minutes=5)),
            resume_basis=RESUME_BASIS_BUDGET_INCREASE,
        )
        assert effective.deadline_at is not None
