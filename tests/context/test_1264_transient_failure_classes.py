"""#1264（Shape A）：`_TRANSIENT_ERROR_CLASSES` 必须是 `_SUMMARY_ERROR_CLASSES` 的**穷尽二分**。

runtime 的"再压一次"判定完全建立在这份二分上：词表加项而二分没同步 ⇒ 新词条会被
默认归入"结构性"（保守，不重试），或反过来把结构性失败误判成可重试（会白烧一次
摘要请求）。所以二分是**机械守卫**，不是注释里的君子协定。
"""

from __future__ import annotations

from agent_harness.context.compactor import (
    _SUMMARY_ERROR_CLASSES,
    _TRANSIENT_ERROR_CLASSES,
)


def test_transient_is_a_subset_of_the_bounded_vocabulary() -> None:
    assert _TRANSIENT_ERROR_CLASSES <= set(_SUMMARY_ERROR_CLASSES)


def test_transient_and_structural_partition_the_vocabulary_exactly() -> None:
    structural = set(_SUMMARY_ERROR_CLASSES) - _TRANSIENT_ERROR_CLASSES
    assert _TRANSIENT_ERROR_CLASSES | structural == set(_SUMMARY_ERROR_CLASSES)
    assert not (_TRANSIENT_ERROR_CLASSES & structural), "二分不得重叠"
    assert len(_SUMMARY_ERROR_CLASSES) == len(set(_SUMMARY_ERROR_CLASSES)), "词表不得有重复项"


def test_structurally_hopeless_classes_are_never_retriable() -> None:
    """再压必然同结果的那几条必须落在"结构性"侧（写死以防二分被改反）。"""
    for error_class in (
        "summary_not_smaller",
        "target_not_reached",
        "source_range_unavailable",
        "preflight_request_exceeds_hard_limit",
        "deterministic_truncation_fallback",
        "chunk_bound_released",
        "no_compactable_early_turn",
    ):
        assert error_class not in _TRANSIENT_ERROR_CLASSES, error_class


def test_model_side_jitter_is_retriable() -> None:
    for error_class in ("timeout", "transport_error", "empty_summary", "non_text"):
        assert error_class in _TRANSIENT_ERROR_CLASSES, error_class
