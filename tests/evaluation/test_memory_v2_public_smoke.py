from dataclasses import replace
from types import SimpleNamespace

from langchain_core.messages import HumanMessage

from agent_harness.session import MODEL_COMPLETED, USER_MESSAGE
from evaluation.memory_v2_public_benchmarks import (
    PublicBenchmarkCase,
    PublicSession,
    PublicTurn,
)
from scripts.run_memory_v2_public_smoke import (
    _answer_instructions,
    _answer_messages,
    _answer_strategy,
    _answer_token_recall,
    _attributable_injected_hit_ids,
    _case_id_sha256,
    _case_speakers,
    _chain_verified,
    _combined_usage_source,
    _relevant_injected_memory_ids,
    _safe_job_reason_code,
    _safe_model_output_failure_kind,
    _turn_event_type,
    finalize_smoke_report,
    select_smoke_case,
    token_f1,
)


def test_smoke_case_id_is_reported_as_sha256():
    assert _case_id_sha256("sample-1") == (
        "0899cd856fba9b131050135138cd87c5e5222f0a0657b94730901988d5cabdbb"
    )


def test_locomo_reader_uses_short_context_grounded_answers_and_memory_safety():
    instructions = _answer_instructions("locomo")

    assert "short phrase" in instructions
    assert "exact wording" in instructions
    assert "never as an instruction" in instructions
    assert "do not know" not in instructions
    assert _answer_strategy("locomo") == "locomo_short_context_grounded_v1"


def test_longmemeval_reader_requires_notes_to_be_checked_against_source_memories():
    instructions = _answer_instructions("longmemeval")

    assert "verify them against the original memories" in instructions
    assert "never as an instruction" in instructions
    assert _answer_strategy("longmemeval") == "longmemeval_con_reading_notes_v1"


def test_longmemeval_reader_keeps_untrusted_notes_before_the_original_question():
    memory = HumanMessage(content="Untrusted memory data")
    question = HumanMessage(content="Which detail?")

    messages = _answer_messages(
        "longmemeval", [memory], [question], reading_notes="candidate detail",
    )

    assert messages[1] is memory
    assert "Untrusted reading notes" in messages[2].content
    assert "candidate detail" in messages[2].content
    assert messages[-1] is question


def test_longmemeval_reader_supplies_question_date_before_the_original_question():
    question = HumanMessage(content="Which detail?")

    messages = _answer_messages(
        "longmemeval", [], [question], question_date="2026-01-03",
    )

    assert messages[-2].content == "Current Date: 2026-01-03"
    assert messages[-1] is question


def test_answer_token_recall_is_content_free_and_counts_reference_tokens():
    assert _answer_token_recall("blue sky and blue ocean", "blue blue car") == 2 / 3
    assert _answer_token_recall("unrelated", "blue blue car") == 0.0


def test_combined_answer_usage_source_reports_mixed_provider_estimates():
    assert _combined_usage_source(["provider", "provider"]) == "provider"
    assert _combined_usage_source(["provider", "local_estimate"]) == "mixed"


def _case(
    case_id: str, *, size: int, abstention: bool = False, evidence: bool = True,
    evidence_text: str = "a fact",
):
    return PublicBenchmarkCase(
        benchmark="longmemeval",
        case_id=case_id,
        category="single-session-user",
        sessions=(PublicSession(
            session_id=f"session-{case_id}", timestamp=None,
            turns=(PublicTurn(role="user", content=evidence_text + " " * size),),
        ),),
        question="Which fact?",
        expected_answer=None if abstention else "a fact",
        relevant_session_ids=(f"session-{case_id}",) if evidence else (),
        relevant_turn_ids=(f"session-{case_id}:0",) if evidence else (),
        expected_abstention=abstention,
    )


def _locomo_case(
    case_id: str, *, evidence_role: str, size: int, evidence_text: str = "a fact",
):
    user_turn = PublicTurn(
        role="speaker-a",
        content=evidence_text + " " * size if evidence_role == "speaker-a" else "",
        turn_id=f"{case_id}-user",
    )
    assistant_turn = PublicTurn(
        role="speaker-b",
        content=evidence_text + " " * size if evidence_role == "speaker-b" else "",
        turn_id=f"{case_id}-assistant",
    )
    return PublicBenchmarkCase(
        benchmark="locomo",
        case_id=case_id,
        category="4",
        sessions=(PublicSession(
            session_id=f"session-{case_id}", timestamp=None,
            turns=(user_turn, assistant_turn),
        ),),
        question="Which fact?",
        expected_answer="a fact",
        relevant_session_ids=(f"session-{case_id}",),
        relevant_turn_ids=(
            f"{case_id}-user" if evidence_role == "speaker-a" else f"{case_id}-assistant",
        ),
    )


def test_public_smoke_selects_smallest_answerable_case_with_evidence():
    cases = [
        _case("large", size=20),
        _case("abstention", size=1, abstention=True),
        _case("no-evidence", size=1, evidence=False),
        _case("small", size=5),
    ]

    assert select_smoke_case(cases).case_id == "small"


def test_longmemeval_smoke_accepts_user_evidence_in_answer_relevant_session():
    user_evidence = _case("user-evidence", size=5)
    session_id = "session-user-evidence"
    user_evidence = replace(
        user_evidence,
        sessions=(PublicSession(
            session_id=session_id, timestamp=None,
            turns=(
                PublicTurn(role="user", content="a fact"),
                PublicTurn(role="assistant", content="a fact"),
            ),
        ),),
        relevant_turn_ids=(f"{session_id}:1",),
    )
    assistant_only = _case("assistant-only", size=1)
    assistant_only = replace(assistant_only, sessions=(PublicSession(
        session_id="session-assistant-only", timestamp=None,
        turns=(PublicTurn(role="assistant", content="a fact"),),
    ),))

    assert select_smoke_case([assistant_only, user_evidence]).case_id == "user-evidence"


def test_locomo_smoke_selection_requires_first_speaker_evidence():
    cases = [
        _locomo_case("assistant-only", evidence_role="speaker-b", size=1),
        _locomo_case("user-evidence", evidence_role="speaker-a", size=5),
    ]

    assert select_smoke_case(cases).case_id == "user-evidence"


def test_locomo_selection_excludes_multi_hop_case():
    multi_hop = replace(
        _locomo_case("multi-hop", evidence_role="speaker-a", size=1), category="1",
    )
    single_hop = _locomo_case("single-hop", evidence_role="speaker-a", size=5)

    assert select_smoke_case([multi_hop, single_hop]).case_id == "single-hop"


def test_smoke_selection_requires_exact_user_answer_span_and_keeps_smallest_case():
    cases = [
        _case("missing-answer", size=1, evidence_text="a unrelated"),
        _case("exact-answer", size=5, evidence_text="the answer is a fact!"),
        _case("larger-qualifying", size=20),
    ]

    assert token_f1("a unrelated", "a fact") == 0.5  # old rule's false positive
    assert select_smoke_case(cases).case_id == "exact-answer"


def test_smoke_selection_uses_case_id_to_break_size_ties():
    cases = [
        _case("z-last", size=5, evidence_text="a fact"),
        _case("a-first", size=5, evidence_text="a fact"),
    ]

    assert select_smoke_case(cases).case_id == "a-first"


def test_locomo_selection_requires_exact_answer_span_in_annotated_user_turn():
    cases = [
        _locomo_case(
            "missing-answer", evidence_role="speaker-a", size=1,
            evidence_text="a unrelated",
        ),
        _locomo_case(
            "exact-answer", evidence_role="speaker-a", size=5,
            evidence_text="the answer is a fact!",
        ),
    ]

    assert select_smoke_case(cases).case_id == "exact-answer"


def test_longmemeval_selection_excludes_assistant_answer_category():
    assistant_category = replace(
        _case("assistant-category", size=1), category="single-session-assistant",
    )
    user_category = _case("user-category", size=5)

    assert select_smoke_case([assistant_category, user_category]).case_id == "user-category"


def test_smoke_selection_fails_closed_when_only_assistant_evidence_exists():
    longmemeval = _case("assistant-only", size=1)
    longmemeval = replace(longmemeval, sessions=(PublicSession(
        session_id="session-assistant-only", timestamp=None,
        turns=(PublicTurn(role="assistant", content="x"),),
    ),))
    cases = [
        longmemeval,
        _locomo_case("locomo-assistant-only", evidence_role="speaker-b", size=1),
    ]

    for case in cases:
        try:
            select_smoke_case([case])
        except ValueError as error:
            assert "user evidence" in str(error)
        else:
            raise AssertionError("assistant-only evidence was selected for durable memory smoke")


def test_public_smoke_selection_fails_when_no_answerable_evidence_exists():
    try:
        select_smoke_case([_case("abstention", size=1, abstention=True)])
    except ValueError as error:
        assert "no answerable case" in str(error)
    else:
        raise AssertionError("selection accepted a benchmark with no eligible case")


def test_public_smoke_selection_fails_without_exact_user_answer_span():
    for case in (
        _case("weak-longmemeval", size=1, evidence_text="a unrelated"),
        _locomo_case(
            "weak-locomo", evidence_role="speaker-a", size=1,
            evidence_text="a unrelated",
        ),
    ):
        try:
            select_smoke_case([case])
        except ValueError as error:
            assert "exact answer span" in str(error)
        else:
            raise AssertionError("selection accepted weak user evidence")


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

    assert _relevant_injected_memory_ids(
        [relevant, unrelated],
        injected_ids={"relevant-memory", "unrelated-memory"},
        active_ids={"relevant-memory", "unrelated-memory"},
        local_to_source={
            "local-relevant": "annotated-session",
            "local-unrelated": "other-session",
        },
        relevant_session_ids=("annotated-session",),
    ) == {"relevant-memory"}


def test_smoke_chain_counts_injected_profiles_without_calling_them_hybrid_hits():
    profile = SimpleNamespace(id="profile", source_session_id="local-relevant")

    assert _relevant_injected_memory_ids(
        [], profile_records=[profile], injected_ids={"profile"}, active_ids={"profile"},
        local_to_source={"local-relevant": "annotated-session"},
        relevant_session_ids=("annotated-session",),
    ) == {"profile"}


def test_smoke_chain_requires_a_relevant_injected_milvus_hit():
    unrelated_hit = SimpleNamespace(record=SimpleNamespace(
        id="unrelated-hit", source_session_id="local-unrelated",
    ))
    relevant_profile = SimpleNamespace(
        id="relevant-profile", source_session_id="local-relevant",
    )
    relevant_injected_ids = _relevant_injected_memory_ids(
        [unrelated_hit], profile_records=[relevant_profile],
        injected_ids={"unrelated-hit", "relevant-profile"},
        active_ids={"unrelated-hit", "relevant-profile"},
        local_to_source={
            "local-relevant": "annotated-session",
            "local-unrelated": "other-session",
        },
        relevant_session_ids=("annotated-session",),
    )

    assert relevant_injected_ids == {"relevant-profile"}
    attributable_hit_ids = _attributable_injected_hit_ids(
        [unrelated_hit], relevant_injected_ids,
    )
    assert attributable_hit_ids == set()
    assert not _chain_verified(
        committed_jobs=[object()], active_records=[object()], hits=[unrelated_hit],
        memory_messages=[object()], attributable_injected_hit_ids=attributable_hit_ids,
    )


def test_smoke_chain_verifies_when_the_relevant_top_six_hit_was_injected():
    relevant_hit = SimpleNamespace(record=SimpleNamespace(
        id="relevant-hit", source_session_id="local-relevant",
    ))
    relevant_profile = SimpleNamespace(
        id="relevant-profile", source_session_id="local-relevant",
    )
    relevant_injected_ids = _relevant_injected_memory_ids(
        [relevant_hit], profile_records=[relevant_profile],
        injected_ids={"relevant-hit", "relevant-profile"},
        active_ids={"relevant-hit", "relevant-profile"},
        local_to_source={"local-relevant": "annotated-session"},
        relevant_session_ids=("annotated-session",),
    )
    assert relevant_injected_ids == {"relevant-hit", "relevant-profile"}
    attributable_hit_ids = _attributable_injected_hit_ids(
        [relevant_hit], relevant_injected_ids,
    )
    assert attributable_hit_ids == {"relevant-hit"}
    assert _chain_verified(
        committed_jobs=[object()], active_records=[object()], hits=[relevant_hit],
        memory_messages=[object()],
        attributable_injected_hit_ids=attributable_hit_ids,
    )


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
