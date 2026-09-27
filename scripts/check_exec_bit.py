#!/usr/bin/env python3
"""`check_exec_bit.py` —— 索引可执行位 ⇔ shebang 的一致性检查（Gate-0 `guards` 车道消费）。

## 它补的是哪条上游规则

`ruff` 的 `EXE001`（**Shebang is present but file is not executable**）只在 Unix 生效；本机
Windows 又叠上 `core.filemode=false` ⇒ 本地 `ruff check .` **看不见**它。机制叙述、两次事故
（`99a744fe` 补 7 个脚本的可执行位 / `2d3761c` 改 `scripts/live_gate.py` 的索引位）与全部实测
读数见 `docs/agents/verification.md` §2 ⑦ —— 此处**不复述**（`AGENTS.md` §16.1 的指针纪律）。

## 判据

被跟踪的 `*.py` / `*.pyi` 里，凡 **blob 内容以 `#!` 开头**者，其**索引模式必须是 `100755`**。

- **读索引，不读工作树权限**：本仓 `core.filemode=false`，工作树权限变更 git 根本不记录 ——
  两次事故的成因正是本地怎么改都看不见。
- **读 blob，不读工作树文件**：blob 是仓库存储字节（本仓 HEAD blob 恒 LF），
  读数与 `core.autocrlf` / 本地未提交编辑无关。
- **判据是位置口径：`#!` 必须在 blob 的字节 0**。头两字节不是 `#!` 的一律不算 —— 包括 UTF-8
  BOM 开头的文件（字节 `EF BB BF` + `#!`）。该形状**刻意不归本守卫管**：本地 `ruff` 的
  `EXE005`（`shebang-not-first-line`；默认规则集启用、**无平台门控**）在有 BOM 时就已必红，
  而 `chmod +x` **修不掉** EXE005 ⇒ 并进来只会给出误导性修法（2026-09-27 实测）。
- **范围只到 Python**（`ruff` 的 lint 面）：`.sh` / `.ps1` / `.mjs` 带 shebang 而 `100644` 是本仓
  现状且无害（一律经解释器调用），纳入会立刻产生 11 条误报 ⇒ 刻意的边界，不是漏项
  （清单见 `docs/agents/verification.md` §2 ⑦）。
- **反方向未纳入**（`100755` 而无 shebang）：本仓 0 例，且上游规则口径未核对 ⇒ 不猜。

**fail-closed 两处**：① blob 读不到（`--batch` 没回、或回的不是 blob）⇒ 算**违例**，不算跳过
（核对不了就不放行，与 `check_review_coverage.py` 同口径）；② 索引里有**未合并条目**
（`ls-files -s` 的 stage ≠ 0）⇒ 直接报错退出，不按 stage 0 的语义读 —— 冲突中的索引不代表将要
提交的内容，照着读会给出看起来正常、实则无意义的读数。

退出码：0 = 无违例；1 = 有违例或索引不可读（逐条打印原因 + 给修法）。
"""

from __future__ import annotations

import argparse
import subprocess
import sys

#: 判据只覆盖 ruff 会 lint 的扩展名（见模块 docstring 的"范围"一条）。
PY_SUFFIXES = (".py", ".pyi")

#: git 索引里"可执行"的模式位。本仓脚本一律这个值。
EXEC_MODE = "100755"

SHEBANG = b"#!"

REASON_UNREADABLE = "blob 读不到（核对不了 ⇒ fail-closed）"
REASON_NOT_EXECUTABLE = f"带 shebang 但索引模式不是 {EXEC_MODE}"

FIX_HINT = "修法：`git update-index --chmod=+x <path>`（改**索引**，不是关掉 CI 那条 ruff 规则）"


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace", newline="\n")


def read_index_entries(repo: str = ".") -> list[tuple[str, str, str]]:
    """`git ls-files -s` 里的 Python 部分，返回 `[(path, mode, blob_sha), ...]`。

    路径是仓库相对、正斜杠；`mode` 是索引里的模式（`100644` / `100755`）。

    未合并条目（stage ≠ 0）⇒ **直接退出**：见模块 docstring 的 fail-closed 第 ② 条。
    """
    proc = subprocess.run(
        ["git", "ls-files", "-s", "--", *(f"*{suffix}" for suffix in PY_SUFFIXES)],
        cwd=repo or None, capture_output=True, text=True, encoding="utf-8", check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"git ls-files 失败：{proc.stderr.strip()}")
    entries: list[tuple[str, str, str]] = []
    for line in proc.stdout.splitlines():
        meta, _, path = line.partition("\t")
        cells = meta.split()
        # `-s` 每行是 `<mode> <sha> <stage>\t<path>`。stage ≠ 0 = 未合并（冲突中）⇒ 这份索引
        # 不代表将要提交的内容，照着 stage 0 读只会给出看起来正常、实则无意义的读数 ⇒ 退出。
        if len(cells) != 3 or not path:
            continue
        if cells[2] != "0":
            raise SystemExit(f"索引里有未合并条目（{path} stage={cells[2]}）："
                             "先解决冲突，再跑本检查")
        entries.append((path, cells[0], cells[1]))
    return entries


def read_blob_heads(shas: list[str], repo: str = ".") -> dict[str, bytes | None]:
    """一次性读多个 blob 的**至多前 2 字节**；读不到的记 `None`（调用方 fail-closed）。

    用单进程 `git cat-file --batch` 而不是逐 blob 起子进程：Python 文件有数百个，
    逐条 `git cat-file blob` 会是数百次进程创建（`check_semantic_equiv.py` 只读 2 个 blob，
    所以它那种写法在那里没问题，在这里不行）。
    长度 < 2 的 blob（空文件 / 单字节）只会回更短的头 ⇒ 按 `size` 截断；判定不受影响
    （比 `#!` 更短的内容不可能等于 `#!`），但不截断就会把下一条响应头的首字节读进来。
    """
    if not shas:
        return {}
    payload = "".join(f"{sha}\n" for sha in shas).encode("ascii")
    proc = subprocess.run(
        ["git", "cat-file", "--batch"], cwd=repo or None,
        input=payload, capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"git cat-file --batch 失败：{proc.stderr.decode('utf-8', 'replace').strip()}")
    out = proc.stdout
    heads: dict[str, bytes | None] = {}
    pos = 0
    for sha in shas:
        newline = out.find(b"\n", pos)
        if newline < 0:  # 输出提前结束：剩下的都算读不到
            break
        cells = out[pos:newline].decode("ascii", "replace").split()
        pos = newline + 1
        # 缺失对象回的是 `<sha> missing`；其余是 `<sha> <type> <size>`。
        if len(cells) != 3 or cells[1] != "blob":
            heads[sha] = None
            continue
        try:
            size = int(cells[2])
        except ValueError:
            heads[sha] = None
            continue
        heads[sha] = out[pos : pos + min(2, size)]
        pos += size + 1  # blob 正文 + `--batch` 的那个换行
    for sha in shas:  # 上面 `break` 掉的那些也要显式登记成 None，不能靠 `dict.get` 的默认值
        heads.setdefault(sha, None)
    return heads


def find_violations(
    entries: list[tuple[str, str, str]],
    heads: dict[str, bytes | None],
) -> list[tuple[str, str, str]]:
    """判据本体（纯函数）：返回 `[(path, mode, 原因), ...]`，空列表 = 通过。

    `entries` 是 `(path, mode, sha)`；`heads` 是 `sha -> 至多前 2 字节或 None`。
    不读 git、不读文件系统 ⇒ 控制样本可以直接构造输入（见
    `tests/test_exec_bit_matches_shebang.py` 的正控 / 反控 / 越界控 / 缺读数控）。
    后缀范围由模块常量 `PY_SUFFIXES` 定；不设形参 —— 没有任何调用方需要别的范围。
    """
    problems: list[tuple[str, str, str]] = []
    for path, mode, sha in entries:
        if not path.endswith(PY_SUFFIXES):
            continue
        head = heads.get(sha)
        if head is None:
            problems.append((path, mode, REASON_UNREADABLE))
            continue
        if head.startswith(SHEBANG) and mode != EXEC_MODE:
            problems.append((path, mode, REASON_NOT_EXECUTABLE))
    return problems


def main(argv: list[str]) -> int:
    _utf8_stdio()
    parser = argparse.ArgumentParser(description="索引可执行位 ⇔ shebang 一致性检查")
    parser.add_argument("--repo", default=".", help="git 仓库根（默认当前目录）")
    args = parser.parse_args(argv)

    entries = read_index_entries(args.repo)
    heads = read_blob_heads([sha for _, _, sha in entries], args.repo)
    problems = find_violations(entries, heads)

    if not problems:
        print(f"✅ 索引可执行位检查通过：{len(entries)} 个 Python 文件，无 shebang/模式不符")
        return 0
    print(f"❌ 索引可执行位检查：{len(problems)} 条违例（共 {len(entries)} 个 Python 文件）")
    for path, mode, reason in problems:
        print(f"  · {path}  [{mode}]  {reason}")
    print(FIX_HINT)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
