from dataclasses import replace
from types import SimpleNamespace

from agent_harness.session import MODEL_COMPLETED, USER_MESSAGE
from evaluation.memory_v2_public_benchmarks import (
    PublicBenchmarkCase,
    PublicSession,
    PublicTurn,
)
from scripts.run_memory_v2_public_smoke import (
    _case_speakers,
    _relevant_injected_hit_ids,
    _safe_job_reason_code,
    _safe_model_output_failure_kind,
    _turn_event_type,
    finalize_smoke_report,
    select_smoke_case,
    token_f1,
)


def _case(case_id: str, *, size: int, abstention: bool = False, evidence: bool = True):
    return PublicBenchmarkCase(
        benchmark="longmemeval",
        case_id=case_id,
        category="single-session",
        sessions=(PublicSession(
            session_id=f"session-{case_id}", timestamp=None,
            turns=(PublicTurn(role="user", content="x" * size),),
        ),),
        question="Which fact?",
        expected_answer=None if abstention else "a fact",
        relevant_session_ids=(f"session-{case_id}",) if evidence else (),
        expected_abstention=abstention,
    )


def test_public_smoke_selects_smallest_answerable_case_with_evidence():
    cases = [
        _case("large", size=20),
        _case("abstention", size=1, abstention=True),
        _case("no-evidence", size=1, evidence=False),
        _case("small", size=5),
    ]

    assert select_smoke_case(cases).case_id == "small"


def test_public_smoke_selection_fails_when_no_answerable_evidence_exists():
    try:
        select_smoke_case([_case("abstention", size=1, abstention=True)])
    except ValueError as error:
        assert "no answerable case" in str(error)
    else:
        raise AssertionError("selection accepted a benchmark with no eligible case")


def test_public_smoke_selection_requires_a_nonempty_relevant_session():
    empty = _case("empty", size=1)
    empty = replace(empty, sessions=(PublicSession(
        session_id="session-empty", timestamp=None, turns=(),
    ),))
    populated = _case("populated", size=5)

    assert select_smoke_case([empty, populated]).case_id == "populated"


def test_public_smoke_reason_aggregation_keeps_only_stable_codes():
    assert _safe_job_reason_code("invalid_model_output") == "invalid_model_output"
    assert _safe_job_reason_code(None) == "none"
    assert _safe_job_reason_code("model response content") == "other"
    assert _safe_model_output_failure_kind("invalid_json") == "invalid_json"
    assert _safe_model_output_failure_kind("private response text") == "other"


def test_smoke_chain_counts_only_injected_hits_from_annotated_sessions():
    relevant = SimpleNamespace(record=SimpleNamespace(
        id="relevant-memory", source_session_id="local-relevant",
    ))
    unrelated = SimpleNamespace(record=SimpleNamespace(
        id="unrelated-memory", source_session_id="local-unrelated",
    ))

    assert _relevant_injected_hit_ids(
        [relevant, unrelated],
        injected_ids={"relevant-memory", "unrelated-memory"},
        active_ids={"relevant-memory", "unrelated-memory"},
        local_to_source={
            "local-relevant": "annotated-session",
            "local-unrelated": "other-session",
        },
        relevant_session_ids=("annotated-session",),
    ) == {"relevant-memory"}


def test_locomo_speaker_persona_stays_consistent_across_sessions():
    case = replace(
        _case("locomo", size=1),
        benchmark="locomo",
        sessions=(
            PublicSession(
                session_id="session-1", timestamp=None,
                turns=(PublicTurn(role="speaker-a", content="a"),
                       PublicTurn(role="speaker-b", content="b")),
            ),
            PublicSession(
                session_id="session-2", timestamp=None,
                turns=(PublicTurn(role="speaker-b", content="b"),
                       PublicTurn(role="speaker-a", content="a")),
            ),
        ),
    )
    speakers = _case_speakers(case)

    assert speakers == ["speaker-a", "speaker-b"]
    assert _turn_event_type("locomo", "speaker-a", speakers) == USER_MESSAGE
    assert _turn_event_type("locomo", "speaker-b", speakers) == MODEL_COMPLETED


def test_public_smoke_report_fails_when_real_chain_is_not_observed():
    report = {
        "status": "completed",
        "blocking": False,
        "failures": [],
        "smoke": {
            "chain_verified": False,
            "answer_f1": 1.0,
            "answer_f1_threshold": 0.5,
        },
    }

    assert finalize_smoke_report(report)["status"] == "failed"
    assert report["failures"] == ["memory_v2_chain_not_observed"]


def test_public_smoke_report_fails_below_answer_quality_threshold():
    report = {
        "status": "completed",
        "blocking": False,
        "failures": [],
        "smoke": {
            "chain_verified": True,
            "answer_f1": 0.49,
            "answer_f1_threshold": 0.5,
        },
    }

    assert finalize_smoke_report(report)["status"] == "failed"
    assert report["failures"] == ["answer_quality_below_smoke_threshold"]


def test_smoke_token_f1_is_normalized_and_bounded():
    assert token_f1("A fact!", "a fact") == 1.0
    assert 0.0 < token_f1("a fact and more", "a fact") < 1.0
    assert token_f1("unrelated", "a fact") == 0.0
