from evaluation.memory_v2_recall import dataset_sha256, load_dataset, recall_at_k


def test_frozen_project_recall_corpus_is_versioned_and_cross_session() -> None:
    dataset = load_dataset()

    assert dataset["version"] == 1
    assert dataset["scope"] == "project"
    assert dataset["source_session_id"] != dataset["query_session_prefix"]
    assert dataset["minimum_recall"] == 0.85
    assert dataset_sha256() == "8b33b9da3cf9cfeca894c3080d9358e4bfc3eca6cee3f8c817f7b772619813c8"
    assert len(dataset["memories"]) >= 20
    assert len(dataset["queries"]) >= 20
    assert all(memory["source"].startswith(("SPEC_ROOT/", "docs/")) for memory in dataset["memories"])


def test_recall_at_k_counts_relevant_items_and_reports_ranks() -> None:
    dataset = {
        "k": 2,
        "queries": [
            {"id": "q1", "relevant_ids": ["a"]},
            {"id": "q2", "relevant_ids": ["b", "c"]},
        ],
    }

    result = recall_at_k(dataset, {"q1": ["x", "a", "b"], "q2": ["c", "x"]})

    assert result == {
        "k": 2,
        "hits": 2,
        "relevant": 3,
        "recall": 2 / 3,
        "ranks": {"q1": 2, "q2": 1},
    }
