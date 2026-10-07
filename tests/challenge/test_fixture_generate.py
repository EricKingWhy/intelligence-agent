"""W-20: 夹具生成可复跑性测试.

两次独立生成 batch-import-demo，应得到相同初态（7 条旧记录 hash 一致）。
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../tools/challenge-fixture"))

from generate import generate, seed_hashes


def _db_hashes(db_path: str) -> dict[str, str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT id, payload_hash FROM records").fetchall()
        return {r[0]: r[1] for r in rows}
    finally:
        conn.close()


def test_two_independent_generations_same_initial_state(tmp_path):
    d1 = generate(str(tmp_path / "demo1"))
    d2 = generate(str(tmp_path / "demo2"))
    h1 = _db_hashes(os.path.join(d1, "demo.db"))
    h2 = _db_hashes(os.path.join(d2, "demo.db"))
    assert h1 == h2, "两次生成初态不一致"
    assert h1 == seed_hashes(), "seed hash 与固定值不符"
    assert len(h1) == 7


def test_features_json_all_false_initially(tmp_path):
    import json

    d = generate(str(tmp_path / "demo"))
    with open(os.path.join(d, "features.json"), encoding="utf-8") as f:
        features = json.load(f)
    assert all(f["passes"] is False for f in features["features"])
    assert len(features["features"]) == 8


def test_progress_md_contains_constraints(tmp_path):
    d = generate(str(tmp_path / "demo"))
    with open(os.path.join(d, "progress.md"), encoding="utf-8") as f:
        text = f.read()
    # 三条用户约束必须在进度文件里（跨窗口可读）
    assert "不得删除" in text or "旧 7 条" in text
    assert "FAKE_ADVICE" in text
