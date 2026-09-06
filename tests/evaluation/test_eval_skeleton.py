"""T6 #122：evaluation/ 骨架——数据集加载、真实 Runtime 跑 case、报告、seed 幂等。"""

from __future__ import annotations

import json
from pathlib import Path

from evaluation.runner import load_dataset, run_case, run_dataset
from evaluation.seed_langfuse import push_dataset

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DATASET = _REPO_ROOT / "evaluation" / "datasets" / "p0_core.jsonl"


def test_load_dataset_returns_typed_cases():
    cases = load_dataset(_DATASET)
    assert cases and cases[0].name == "tool_selection_add"
    assert cases[0].case_type == "tool_selection"
    assert cases[0].script, "deterministic case 必须带模型剧本"


def test_run_case_with_real_runtime_deterministic(tmp_path: Path):
    """默认路径 = ScriptedModel + 真实 AgentRuntime + 真实 tool registry。"""
    case = load_dataset(_DATASET)[0]
    result, events = run_case(case, session_root=tmp_path / "sess")

    assert result.ok, f"metrics={result.metrics} error={result.error}"
    assert result.metrics["tool_selected"] is True
    assert result.metrics["dangling_tool_calls"] == 0
    assert result.metrics["status"] == "completed"
    assert result.usage_total.get("total_tokens", 0) >= 0
    assert events, "事件流必须返回供 Langfuse Scores 复用"


def test_run_dataset_writes_local_report(tmp_path: Path):
    results = run_dataset(_DATASET, session_root=tmp_path / "sess")

    assert len(results) == 1 and results[0].ok
    reports = sorted((_REPO_ROOT / "evaluation" / "reports").glob("p0_core-*.json"))
    assert reports, "本地报告恒产生（云关也能看）"
    report = json.loads(reports[-1].read_text(encoding="utf-8"))
    assert report["passed"] == 1 and report["total"] == 1
    reports[-1].unlink()  # 测试产物不留在 repo


class _FakeLangfuse:
    """记录式假客户端：get_dataset 第二次返回已有 items（幂等验证）。"""

    def __init__(self):
        self.datasets: dict[str, list] = {}
        self.created_items: list = []

    def get_dataset(self, name: str):
        if name not in self.datasets:
            raise RuntimeError(f"dataset {name} not found (404)")
        from types import SimpleNamespace

        return SimpleNamespace(items=[
            SimpleNamespace(metadata={"name": n}) for n in self.datasets[name]
        ])

    def create_dataset(self, name: str):
        self.datasets.setdefault(name, [])

    def create_dataset_item(self, *, dataset_name: str, input, expected_output, metadata):
        self.datasets.setdefault(dataset_name, [])
        self.datasets[dataset_name].append(metadata["name"])
        self.created_items.append(metadata["name"])


def test_seed_pushes_and_is_idempotent():
    client = _FakeLangfuse()
    factory = lambda **kwargs: client

    first = push_dataset(
        _DATASET, public_key="pk", secret_key="sk",
        base_url="https://example.invalid", client_factory=factory,
    )
    assert first == {"status": "ok", "created": 1, "skipped": 0,
                     "dataset": "p0_core"}

    second = push_dataset(
        _DATASET, public_key="pk", secret_key="sk",
        base_url="https://example.invalid", client_factory=factory,
    )
    assert second["created"] == 0 and second["skipped"] == 1, "重复推送不得建重复 item"
    assert len(client.created_items) == 1


def test_seed_skips_gracefully_without_keys():
    calls: list = []

    def _boom(**kwargs):
        calls.append(kwargs)
        raise AssertionError("未配置时不得触碰 SDK")

    result = push_dataset(
        _DATASET, public_key="", secret_key="",
        base_url="https://example.invalid", client_factory=_boom,
    )
    assert result["status"] == "skipped"
    assert "未配置" in result["reason"]
    assert not calls
