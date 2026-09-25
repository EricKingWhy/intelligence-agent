"""Pure helpers for the frozen MEM-V2 cross-session Recall@k evaluation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

DATASET_PATH = Path(__file__).with_name("datasets") / "memory_v2_project_cross_session_v1.json"


def load_dataset(path: Path = DATASET_PATH) -> dict:
    dataset = json.loads(path.read_text(encoding="utf-8"))
    memory_ids = [memory["id"] for memory in dataset["memories"]]
    query_ids = [query["id"] for query in dataset["queries"]]
    if len(memory_ids) != len(set(memory_ids)) or len(query_ids) != len(set(query_ids)):
        raise ValueError("frozen recall dataset IDs must be unique")
    if dataset["scope"] != "project" or dataset["source_session_id"].startswith(
        dataset["query_session_prefix"]
    ):
        raise ValueError("frozen recall dataset must cross project sessions")
    known_memory_ids = set(memory_ids)
    if any(
        not query["relevant_ids"] or not set(query["relevant_ids"]) <= known_memory_ids
        for query in dataset["queries"]
    ):
        raise ValueError("every query must declare known relevant memories")
    return dataset


def dataset_sha256(path: Path = DATASET_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def recall_at_k(
    dataset: dict, ranked_ids: dict[str, Sequence[str]], *, k: int | None = None,
) -> dict:
    """Return micro Recall@k and expected-item ranks without query or memory text."""
    k = dataset["k"] if k is None else k
    if k <= 0:
        raise ValueError("k must be positive")
    queries = dataset["queries"]
    query_ids = {query["id"] for query in queries}
    if set(ranked_ids) != query_ids:
        raise ValueError("ranked results must cover each frozen query exactly once")

    relevant_count = 0
    hit_count = 0
    ranks: dict[str, int | None] = {}
    for query in queries:
        relevant = set(query["relevant_ids"])
        ranked = list(ranked_ids[query["id"]])[:k]
        relevant_count += len(relevant)
        hit_count += len(relevant.intersection(ranked))
        ranks[query["id"]] = next(
            (rank for rank, memory_id in enumerate(ranked, start=1) if memory_id in relevant),
            None,
        )
    return {
        "k": k,
        "hits": hit_count,
        "relevant": relevant_count,
        "recall": hit_count / relevant_count,
        "ranks": ranks,
    }
