"""#1662：`_RECEIPT_BOOTSTRAP_SHA` 必须钉在一个**它自己也满足**的锚点上。

为什么这条值得一条用例：锚点的失效形态是**静默 fail-open**。`receipt_present_check` 对
`merge-base --is-ancestor <bootstrap> <c>` 失败（退出码 ≠ 0）的提交直接 `continue`，注释写的是
「早于 bootstrap ⇒ 范围外」。于是**任何一棵用了这个常量、却拿不到该锚点的树**，其范围内全部
提交都被机械豁免——判据看起来还在、实际被挖空。

三条断言分别钉住三种形状：
① 锚点在仓里解析得出来（拼错 / 分支未 fetch ⇒ 这条红）；
② 锚点是本仓 HEAD 的祖先（锚成可解析但不相干的 sha ⇒ 这条红）；
③ **任何一棵带着这个常量的树，锚点都在它的历史里**——③ 才是 fail-open 的形状本身：常量与锚点
   必须一起传播。它同时是「锚须在**共享**历史里」的一般形式：锚点一旦随合并进了 `origin/main`，
   ③ 对 `origin/main`（以及所有从 main 切出的分支）就自动生效，不需要另写一条只认 origin/main
   的断言（那种断言在合并前必然红，而合并前锚点按裁决 A 本来就是分支私有的）。

判别力（2026-11-01 实测，一次性 `--shared` clone，未动主工作树）：
· 常量改成可解析但非祖先的 sha（`2b04fa767…`）⇒ 只有 ② 红（`assert 1 == 0`）；
· 常量拼成仓里不存在的 sha（`…dda8a`）⇒ 只有 ① 红（rc=128）；
· 在 `origin/main` 上另造一笔「带新常量、无锚点」的提交 ⇒ ③ 单独红（offenders=['HEAD']，
  另有别的 ref 也带常量时 offenders 指名该 ref，此时 ② 仍绿）；
不存在「三条断言一起红」的单一变异。
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GATE0_PATH = REPO / "scripts" / "gate0.py"


def _load_gate0():
    spec = importlib.util.spec_from_file_location("_gate0_i1662", GATE0_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate0 = _load_gate0()


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=False)


def test_bootstrap_anchor_exists_and_is_ancestor_of_head():
    anchor = gate0.receipt_bootstrap_sha()
    assert anchor and len(anchor) == 40, anchor

    resolved = _git("rev-parse", "--verify", f"{anchor}^{{commit}}")
    assert resolved.returncode == 0, f"锚点 {anchor} 在仓里解析不出：{resolved.stderr.strip()}"

    ancestor = _git("merge-base", "--is-ancestor", anchor, "HEAD")
    assert ancestor.returncode == 0, (
        f"锚点 {anchor} 不是 HEAD 的祖先 ⇒ receipt-present 会把范围内全部提交机械豁免"
        f"（判据被挖空，fail-open）：{ancestor.stderr.strip()}"
    )


def test_every_tree_carrying_the_anchor_also_has_it_in_its_history():
    """常量与锚点必须一起传播：带常量的树若拿不到锚点，该树的 receipt-present 就整体归零。

    这条覆盖「锚须在共享历史里」：合并进 `origin/main` 之后，origin/main 与从它切出的分支都带着
    常量 ⇒ 一并被本条要求「锚点是它们的祖先」。合并之前锚点是分支私有的（#1662 裁决 A 的取舍，
    见 `scripts/gate0.py` 常量注释的 AC5 段），本条对**没有**该常量的树不作要求——那些树跑的是
    自己那份旧常量，与本笔无关。
    """
    anchor = gate0.receipt_bootstrap_sha()
    # HEAD 也纳入：CI 的 `actions/checkout` 检出的是**游离 HEAD**，它不一定对应到某个 ref。
    refs = [r for r in _git("for-each-ref", "--format=%(refname)").stdout.split() if r]
    trees = ["HEAD", *refs]
    assert refs, "仓里一个 ref 都没有——无法核对常量传播"

    query = "".join(f"{ref}:scripts/gate0.py\n" for ref in trees)
    batch = subprocess.run(
        ["git", "cat-file", "--batch-check=%(objectname)"], cwd=REPO,
        input=query, capture_output=True, text=True, check=False,
    )
    assert batch.returncode == 0, f"批量取 blob 失败：{batch.stderr.strip()}"
    blob_of = {
        ref: (None if line.endswith("missing") else line.strip())
        for ref, line in zip(trees, batch.stdout.splitlines())
    }

    carriers = set()
    for blob in {b for b in blob_of.values() if b}:
        body = _git("cat-file", "blob", blob).stdout
        if anchor in body:
            carriers.add(blob)
    assert carriers, (
        f"没有任何树（含 HEAD）的 scripts/gate0.py 含锚点 {anchor}：常量与注释对不上（或常量刚被改掉）"
    )

    offenders = [
        ref for ref, blob in blob_of.items()
        if blob in carriers and _git("merge-base", "--is-ancestor", anchor, ref).returncode != 0
    ]
    assert not offenders, (
        f"这些树带着锚点 {anchor} 却拿不到该锚点 ⇒ 它们范围内全部提交被机械豁免"
        f"（fail-open，常量与锚点没有一起传播）：{offenders}"
    )
