"""#609：gate0 环境失败结构化字段（协议 §8.6 机械化口径；#294 码面另票）。

孵化风暴期（#338 轮 7：0xC0000142 ×133 / WinError 1114）车道失败在读数 JSON 里与
真实代码红**不可区分**，归因只能人工取证。本文件钉住：

1. `classify_env_failure` 只认两条窄签名（rc 形态锚定），其余一律 None；
2. 读数 doc 的 `env_failures`（顶层、恒在）+ 车道 `env_signature`（命中才有）是
   **additive**：`result` / `status` / `passed` / `failed` / `schema` / 退出码与旧
   形状逐项相同（不洗绿）；
3. §8.6 口径行三臂（全 env ⇒ 打；混合 ⇒ 不打；全真红 ⇒ 不打）+ replay 不消费
   新字段（源码零引用 + 差分读数判定逐项一致）。

签名表权威（协议 §1.3 三源）：pytest exit-codes（内部错误 ≠ 测试失败，分立信号）、
Buildbot EXCEPTION（infra 失败独立成态、绝不洗绿）、MS WinError.h 1114 +
NTSTATUS 0xC0000142（本地实测 3221225794，#338 留档）。新签名须另走新票（窄进）。
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GATE0_PATH = REPO / "scripts" / "gate0.py"


def _load_gate0():
    spec = importlib.util.spec_from_file_location("_gate0_i609", GATE0_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate0 = _load_gate0()

WINERROR_1114_OUTPUT = (
    "⚠ 无法执行：[WinError 1114] A dynamic link library (DLL) initialization "
    "routine failed."
)


def _lane(name: str):
    return gate0.Lane(name, f"{name} 车道", [sys.executable, "-c", "pass"], gate0.REPO_ROOT)


# ── AC1：窄签名命中 ──


def test_classify_hits_exit_0xc0000142():
    sig = gate0.classify_env_failure(3221225794, "（任意输出）")
    assert sig is not None and sig["id"] == "exit-0xc0000142"


def test_classify_hits_winerror_1114():
    sig = gate0.classify_env_failure(127, WINERROR_1114_OUTPUT)
    assert sig is not None and sig["id"] == "winerror-1114"


def test_signature_table_is_narrow_v1():
    # V1 只收两条留档证据（#338 轮 7 同一事件形态）；新增须另走新票。
    assert [s["id"] for s in gate0.ENV_SIGNATURES] == ["exit-0xc0000142", "winerror-1114"]


# ── AC1 反洗绿钉：分类只认 rc 形态锚定，不做纯文本匹配放行 ──


@pytest.mark.parametrize("rc,output", [
    (1, "assert actual == expected"),                          # 普通断言红
    (1, f"引用文本：{WINERROR_1114_OUTPUT}"),                  # 签名字样出现但 rc 不在表
    (1, "子进程退出码 3221225794 被记录"),                     # 同上（退出码文本引用）
    (124, "⚠ 超时：超过 600s 仍未返回"),                       # 超时另有语义，不入本表
    (127, "⚠ 无法执行：[WinError 2] 系统找不到指定的文件"),    # 127 但非 1114
    (0, WINERROR_1114_OUTPUT),                                 # 通过车道不分类
])
def test_anti_greenwash_non_hits(rc, output):
    assert gate0.classify_env_failure(rc, output) is None


# ── AC2：读数 doc additive 字段 + 旧形状逐项不变 ──


def _doc(results):
    return gate0.reading_doc(
        head="a" * 40, tree="b" * 40, argv=[], wall=1.0,
        results=results, affected=None, changed=[], worktree=None,
    )


def test_reading_doc_env_fields_are_additive_shapes_unchanged():
    results = [(_lane("ruff"), 0, 0.5, ""), (_lane("guards"), 3221225794, 0.1, "boom")]
    doc = _doc(results)
    sig = next(s for s in gate0.ENV_SIGNATURES if s["id"] == "exit-0xc0000142")
    # §8.6 点名的结构化字段：恒在、命中车道逐条（lane/rc/signature/evidence）
    assert doc["env_failures"] == [{
        "lane": "guards", "rc": 3221225794,
        "signature": "exit-0xc0000142", "evidence": sig["evidence"][:200],
    }]
    entries = {e["name"]: e for e in doc["lanes"]}
    assert entries["guards"]["env_signature"] == {
        "id": sig["id"], "evidence": sig["evidence"][:200],
    }
    assert "env_signature" not in entries["ruff"]          # 通过车道不带该字段
    # 反洗绿：判定面与旧形状逐项相同（归因 ≠ 通过）
    assert entries["guards"]["status"] == "FAIL"
    assert doc["result"] == "FAIL" and doc["failed"] == ["guards"]
    assert doc["passed"] == 1 and doc["total"] == 2
    assert doc["schema"] == 1                              # additive 不 bump（消费面已核）


def test_reading_doc_clean_run_has_empty_env_failures():
    doc = _doc([(_lane("ruff"), 0, 0.5, "")])
    assert doc["env_failures"] == []                       # 恒在：干净运行 = 空列表
    assert doc["result"] == "PASS"


# ── AC3：§8.6 口径行三臂 ──


def test_env_verdict_line_all_env_hits():
    results = [
        (_lane("guards"), 3221225794, 0.1, "boom"),
        (_lane("ruff"), 127, 0.2, WINERROR_1114_OUTPUT),
    ]
    line = gate0.env_verdict_line(results)
    assert line is not None
    assert "环境项之外 0 失败" in line and "2/2" in line
    assert "exit-0xc0000142" in line and "winerror-1114" in line


def test_env_verdict_line_mixed_is_none():
    results = [(_lane("guards"), 3221225794, 0.1, "boom"), (_lane("ruff"), 1, 0.1, "assert x")]
    assert gate0.env_verdict_line(results) is None         # 真红优先于归因


def test_env_verdict_line_all_real_red_is_none():
    assert gate0.env_verdict_line([(_lane("ruff"), 1, 0.1, "assert x")]) is None


def test_env_verdict_line_no_failures_is_none():
    assert gate0.env_verdict_line([(_lane("ruff"), 0, 0.1, "")]) is None


# ── AC4：replay 兼容（新字段不进判定）──


def test_replay_source_does_not_consume_env_fields():
    src = inspect.getsource(gate0.replay_reading)
    assert "env_signature" not in src and "env_failures" not in src


def test_replay_verdict_identical_with_and_without_env_fields(tmp_path, monkeypatch):
    """差分钉：除 env 字段外相同的两份读数，replay 判定必须逐项一致（都过）。"""
    sha, tree = "c" * 40, "d" * 40
    names = [ln.name for ln in gate0.build_lanes("")]
    assert len(names) == 6

    pass_argv = gate0._portable_argv([sys.executable, "-c", "pass"])
    fail_argv = gate0._portable_argv([sys.executable, "-c", "import sys; sys.exit(3)"])

    def make_doc(with_env: bool) -> dict:
        lanes = []
        for index, name in enumerate(names):
            failed = index == 0
            argv = fail_argv if failed else pass_argv
            entry = {
                "name": name, "desc": name,
                "status": "FAIL" if failed else "PASS",
                "rc": 127 if failed else 0, "seconds": 0.1, "cwd": ".",
                "argv": argv, "command": " ".join(argv),
            }
            if failed and with_env:
                entry["env_signature"] = {
                    "id": "winerror-1114",
                    "evidence": "WinError 1114 (ERROR_DLL_INIT_FAILED)",
                }
            lanes.append(entry)
        doc = {
            "schema": 1, "gate": "gate0", "sha": sha, "tree": tree, "argv": [],
            "result": "FAIL", "passed": 5, "total": 6, "failed": [names[0]],
            "wall_seconds": 1.0, "lanes": lanes,
        }
        if with_env:
            doc["env_failures"] = [{
                "lane": names[0], "rc": 127, "signature": "winerror-1114",
                "evidence": "WinError 1114 (ERROR_DLL_INIT_FAILED)",
            }]
        return doc

    class _Proc:
        def __init__(self, stdout: str) -> None:
            self.stdout, self.stderr, self.returncode = stdout, "", 0

    def fake_git(*args):
        if "--quiet" in args:
            return _Proc(tree + "\n")        # sha^{tree} 自洽
        if args[:2] == ("rev-parse", "HEAD"):
            return _Proc(sha + "\n")
        return _Proc("")

    monkeypatch.setattr(gate0, "git", fake_git)
    monkeypatch.setattr(gate0, "worktree_divergence", lambda: {
        "tracked": [], "hidden": [], "untracked": [], "risky": [],
    })
    # tmp_path 在 C: 盘、仓库在 D: 盘：replay 的**展示**辅助 `_rel`（os.path.relpath）
    # 跨盘抛 ValueError——本测试特有的前置，`_rel` 在 replay 里只进打印不进判定。
    monkeypatch.setattr(gate0, "_rel", lambda p: str(p))

    verdicts = []
    for with_env in (False, True):
        path = tmp_path / f"reading_{int(with_env)}.json"
        path.write_text(json.dumps(make_doc(with_env)), encoding="utf-8")
        verdicts.append(gate0.replay_reading(str(path)))
    assert verdicts == [0, 0], "env 字段不得影响 replay 判定（两份都必须一致通过）"


# ── P3（#609 批审查登记）：口径行的 main() 调用点钉 ──
#
# AC3 三臂只钉 env_verdict_line 纯函数本身；main() FAIL 分支的接线
# （`env_line = env_verdict_line(results); if env_line is not None: print(env_line)`）
# 无钉——接线断了（例如调用被误删）纯函数钉不红。此处 mock run_lane 走
# main(['--no-record']) 全链路，用 capsys 断言口径行的在场/缺席。


def _patch_main_io(monkeypatch, outcomes):
    """outcomes: lane_name -> (rc, output)；mock 掉全部 I/O 面，main 只剩纯调度。"""

    def fake_run_lane(lane):
        rc, out = outcomes[lane.name]
        return rc, 0.1, out

    class _Proc:
        stdout = "a" * 40 + "\n"

    monkeypatch.setattr(gate0, "run_lane", fake_run_lane)
    monkeypatch.setattr(gate0, "git", lambda *args: _Proc)
    monkeypatch.setattr(gate0, "surface_report", lambda since: "")
    monkeypatch.setattr(gate0, "_utf8_stdio", lambda: None)


def test_main_fail_branch_prints_env_verdict_line_when_all_env(monkeypatch, capsys):
    names = [ln.name for ln in gate0.build_lanes("")]
    outcomes = {n: (3221225794, "boom") for n in names}
    _patch_main_io(monkeypatch, outcomes)
    rc = gate0.main(["--no-record"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "Gate-0 FAIL" in out
    lines = [ln for ln in out.splitlines() if "环境项之外 0 失败" in ln]
    assert len(lines) == 1
    assert f"{len(names)}/{len(names)}" in lines[0]


def test_main_fail_branch_omits_env_verdict_line_on_mixed_red(monkeypatch, capsys):
    names = [ln.name for ln in gate0.build_lanes("")]
    outcomes = {n: (1, "assert x == y") for n in names}
    outcomes[names[0]] = (3221225794, "boom")
    _patch_main_io(monkeypatch, outcomes)
    rc = gate0.main(["--no-record"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "Gate-0 FAIL" in out
    assert "环境项之外 0 失败" not in out  # 混合真红 ⇒ 口径行缺席（真红优先）
