#!/usr/bin/env python3
"""语义等价判据：两个 Python blob 是否**只差 docstring / 注释**（issue #295 / 缺陷 1）。

## 它是什么

协议 `docs/SDD_WORKFLOW_PROTOCOL.md` §8.1 第 3 条（2026-09-23 / `#294` 写死）规定了一条**例外**：
纯注释 / 纯 docstring 的 `src/**` diff 判「语义等价」，读数可传递、不重开冻结树。
本文件把那句口径变成机器可执行 —— 口径的**判据**在文档里，**实现**在这里。

## 口径（写死，别顺手改）

1. 两个输入都 `ast.parse`（语法不合法 ⇒ **直接判不等价**，见"fail-closed"）；
2. 剥离**模块 / 类 / 函数（含 async）三级** docstring；
3. 剥离**全部注释**（`ast.parse` 本来就不保留注释 —— 它们在 token 流里，不在 AST 里；
   所以"剥离注释"是选 `ast` 路线的**天然收益**，不是额外做的一步）；
4. `ast.unparse` 成规范化源码；5. 两者**逐字节相同** ⇒ 等价。

## 为什么是 `ast.unparse` 而不是 token 流

| 实现 | 对**格式**的敏感度 | 对**行尾**的敏感度 | 本票选择 |
|---|---|---|---|
| `ast.parse` + `ast.unparse` | 不敏感（unparse 自己重新排版） | 不敏感 | **采用** |
| token 流序列化 | 不敏感（可只比 token 类型 + 字符串） | **敏感**（NL/NEWLINE token 带行尾信息） | 未采用 |

未采用 token 流的原因：本仓 `core.autocrlf=true`，同一文件在不同克隆里可能是 CRLF 或 LF
（HEAD blob 恒 LF、工作树恒 CRLF）。token 流比 `ast.unparse` 多一层"行尾口径"要维护，
而本判据的**唯一目的**是回答"代码语义有没有变"，格式/行尾差异恰恰**不该**让它判不等价。

## fail-closed

`ast.parse` 抛 `SyntaxError`（半截 diff、拿错 blob 类型、非 Python 内容）⇒ **判不等价**。
理由：判据在"看不懂"时的正确反应是拒绝等价（那会让调用者回落正常审查），
不是"看着差不多就算等价"。**绝不能**把解析失败当成"没有差异"。

## 用法

    # 两个 blob sha（`git rev-parse <rev>:<path>` 给出）
    python scripts/check_semantic_equiv.py --a <sha1> --b <sha2>
    # 文本模式：给两个文件路径
    python scripts/check_semantic_equiv.py --file-a x.py --file-b y.py
    # 机器可读（最后一行是 JSON）
    python scripts/check_semantic_equiv.py --a <sha1> --b <sha2> --json

退出码：等价 0 / 不等价 1 / **用法或环境错误 2**。⚠ 注意 1 与 2 分开：
"判了不等价"是**正常判定结果**，不是错误 —— 混在一起会让调用者分不清"判据说变了"
还是"判据自己坏了"。
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
import sys


def _utf8_stdio() -> None:
    """沙箱 / Windows 控制台默认可能是 GBK ⇒ 中文会 UnicodeEncodeError。输出统一 UTF-8/LF。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace", newline="\n")


# --------------------------------------------------------------------------- #
# 归一化
# --------------------------------------------------------------------------- #

def _strip_docstrings(tree: ast.AST) -> ast.AST:
    """就地剥掉**模块 / 类 / 函数（含 async）三级** docstring，返回原树。

    只处理这三级的**首条**语句：那是 Python 里唯一会被解释器当 docstring 的位置
    （`__doc__` 的来源）。一个语句块**中间**的裸字符串是**真语句**（求值后被丢弃），
    剥它就是在放松判据 —— 所以这里刻意只查 `body[0]`。

    **剥后为空时补 `ast.Pass()` 会放松判据（实测事故，见 issue #295 两轴审查 P1）**：
    一个「只有 docstring」的函数与一个「docstring + `pass`」的函数，旧实现剥完都只剩 `pass`，
    于是"删掉一行真语句"被判等价。正解：只在**原本就有非 docstring 语句**时才剥
    （判据是 `len(body) >= 2`）；剥完为空时**保留原 body 不动**。
    代价：只有 docstring 的函数与「docstring + `pass`」会被判**不等价**——这是刻意的
    fail-closed 方向（宁可判不等价，不可漏判等价）。

    注：本 docstring **刻意不写行内三引号字面量** —— 那会提前闭合本 docstring 本身
    （同一 session 里已经踩过两次，见协议 §8.9）。
    """
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body or len(body) < 2:
            # 剥了就没有别的语句了 ⇒ 保留原样，避免把"零语句"与"pass"混为一谈。
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                and isinstance(first.value.value, str):
            node.body = body[1:]
    return tree


def normalize(source: str) -> str | None:
    """归一化源码；**语法不合法返回 `None`**（调用者据此 fail-closed）。

    步骤 = `ast.parse` → 剥三级 docstring → `ast.unparse` → 每行 `rstrip` + 去首尾空行。
    `ast.parse` 不保留注释（注释不在 AST 里），所以"剥离全部注释"是这条路线**天然**成立的。
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return None
    tree = _strip_docstrings(tree)
    try:
        dumped = ast.unparse(tree)
    except (ValueError, RecursionError):
        return None
    return "\n".join(line.rstrip() for line in dumped.splitlines()).strip()


def normalized_digest(source: str) -> str:
    """归一化结果的 sha256（前 16 位足够区分，且便于在输出里并排看）。

    ⚠ 解析失败时返回一个**带标记**的摘要（`"<unparseable:…>"`），**不是**空串：
    空串会让"两个都读不出来"和"两个真的归一化成了一样"在输出上无法区分。
    """
    norm = normalize(source)
    if norm is None:
        return "<unparseable>"
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def is_semantically_equivalent(a: str, b: str) -> bool:
    """`a` 与 `b` 是否**只差 docstring / 注释**。

    返回 `True` 的**唯一**条件是两者的归一化源码逐字节相同。
    **任一**输入解析失败 ⇒ `False`（fail-closed）。
    """
    na, nb = normalize(a), normalize(b)
    if na is None or nb is None:
        return False
    return na == nb


# --------------------------------------------------------------------------- #
# 读 blob / 文件
# --------------------------------------------------------------------------- #

def read_blob(sha: str, repo: str = ".") -> str:
    """读一个 git blob（`git cat-file blob <sha>`），返回**仓库存储格式**的文本。

    ⚠ 用 `cat-file` 而不是 `show <rev>:<path>`：前者只吃 sha、语义单一；
    后者对 rev 形态（`a:b`）有额外解析，容易在本判据里引入无关失败面。
    ⚠ 返回的是**仓库存储字节**（`autocrlf` 未转换），对本判据正好 ——
    归一化会消掉行尾差异（见模块 docstring 的两实现差异表）。
    """
    proc = subprocess.run(["git", "cat-file", "blob", sha], cwd=repo or None,
                          capture_output=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(f"读不了 blob {sha}：{proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout.decode("utf-8", "replace")


def read_file(path: str) -> str:
    with open(path, "rb") as fh:
        return fh.read().decode("utf-8", "replace")


def main(argv: list[str]) -> int:
    _utf8_stdio()
    ap = argparse.ArgumentParser(description="语义等价判据（只差 docstring / 注释？）")
    ap.add_argument("--a", help="blob sha（左）")
    ap.add_argument("--b", help="blob sha（右）")
    ap.add_argument("--file-a", help="文件路径（左，与 --a 二选一）")
    ap.add_argument("--file-b", help="文件路径（右，与 --b 二选一）")
    ap.add_argument("--repo", default=".", help="git 仓库根（默认当前目录）")
    ap.add_argument("--json", action="store_true", help="最后一行输出 JSON")
    args = ap.parse_args(argv)

    if bool(args.a) == bool(args.file_a) or bool(args.b) == bool(args.file_b):
        ap.error("左右两侧各给一个：--a/--b（blob）或 --file-a/--file-b（路径），不要混用")

    left = read_blob(args.a, args.repo) if args.a else read_file(args.file_a)
    right = read_blob(args.b, args.repo) if args.b else read_file(args.file_b)

    eq = is_semantically_equivalent(left, right)
    da, db = normalized_digest(left), normalized_digest(right)
    payload = {"equivalent": eq, "digest_a": da, "digest_b": db}

    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        verdict = "等价（只差 docstring / 注释）" if eq else "不等价（存在语义改动）"
        print(f"{verdict}")
        print(f"  归一化摘要: a={da}  b={db}")
        if da == "<unparseable>" or db == "<unparseable>":
            print("  ⚠ 至少一侧语法不合法 ⇒ 按 fail-closed 判不等价（不是「等价」）")
    return 0 if eq else 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except BrokenPipeError:
        # 与覆盖闸门同纪律：管道被提前关闭 ≠ 判据跑通了，**不许** exit 0。
        sys.stderr.write("语义等价判据：输出管道被提前关闭，判据未完成 —— 这不算通过。\n")
        sys.exit(2)
