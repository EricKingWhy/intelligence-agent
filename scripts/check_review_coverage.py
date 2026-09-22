#!/usr/bin/env python3
"""check_review_coverage.py —— 覆盖闸门的 **可运行实现**（`scripts/check_review_coverage.sh` 的等价版）

## 为什么会有第二个实现

`.sh` 版是本仓**唯一的机械闸门**，但它依赖 `dirname` / `wc` / `comm` / `grep` / `sort` /
`mktemp`，而 WorkBuddy 沙箱 bash shim 的 **PATH 里没有 coreutils**（实测全部
`command not found`），`set -euo pipefail` 下第一行 `cd "$(dirname "$0")/.."` 就死 ⇒
**该闸门在施工环境里从未真的执行过**，长期以来只能靠人工用 python 复刻口径
（见 `docs/SDD_TICKET_TRACKER.md:2157` 的登记：「本沙箱跑不了（需 `wsl.exe`，被安全策略拦下）」）。

> **2026-09-22 更正（本批）**：coreutils **并非不存在**，它们在 Git 自带目录里
> （`<PortableGit>/usr/bin`，`dirname`/`wc`/`comm`/`sort`/`mktemp` 全套）⇒
> `PATH="<PortableGit>/usr/bin:$PATH" bash scripts/check_review_coverage.sh` **能跑**，
> 只是**很慢**（逐条 fork git，实测 >11 分钟未结束）。上面"跑不动"说的是**直接跑**
> （PATH 里没有），不是"不可能跑"。本文件的价值因此是：**在本机默认环境下真的能跑、且快**。

## 与 `.sh` 的关系（重要，别搞反）

- **`.sh` 是语义参考实现，冻结**：`docs/SDD_WORKFLOW_PROTOCOL.md` **按行号**引用它
  （`check_review_coverage.sh:51` 的 `DOC_PATTERN`、`:74-75` 的 fail-closed 形状），
  所以**不要**把 `.sh` 改成包装脚本，也不要重排它的行。
- **本文件是等价实现**，口径逐条对齐 `.sh`；凡是 `.sh` 里的实测坑（见其注释）本文件原样继承。
- 两者必须**同 tip 同结论**：三元组（总数 / 已审查 / 待判定）与 ❌ 集合逐项相同。
  日常跑哪一个都行；改动任一侧的语义时，另一侧必须同步。

## 与 `.sh` 的已知（且无害）差异

- 输出中 `%-44s` 的 desc 列对齐：bash printf 与 python 的宽度计算在**多字节中文**上可能差几个
  空格。验收比对的是**三元组 + ❌ 集合**，不是逐字节 stdout。
- 本文件用 `git cat-file --batch-check` 一次解掉全部 rev、用一次 `git show` 批量取文件表，
  因此比 `.sh`（逐条起子进程）快一到两个数量级；**判定逻辑不变**。

## 用法 / 退出码

    python scripts/check_review_coverage.py              # 0 = 全绿；1 = 有未声明 commit 或台账有问题
    LEDGER=path/to.tsv python scripts/check_review_coverage.py
    LEDGER_DIR=path/to/dir python scripts/check_review_coverage.py     # 默认 docs/review_ledger.d
    python scripts/check_review_coverage.py --list       # 只打印范围与覆盖数；仍有缺口时退 2

判据：`<最早台账 base>..HEAD` 的**每条 commit 都必须在台账里有归属**——审查行 / `[whitelist]` 段
里逐条自校验过 docs-only 的 commit / **恰好只改台账文件本身**的记账提交。
**代码提交永远不能走白名单**，只有"补一次审查"一条路。

## 双读过渡（issue #293）

台账有**两处来源**，本闸门**两处都认**（并集）：

- `docs/review_ledger.tsv` —— 旧单文件（多线并行时两侧都 append 会反复走并集解析）；
- `docs/review_ledger.d/<name>.tsv` —— **一文件一条**。目录不存在 = 空，不影响任何判定。

两条硬约束：

- **文件名只是标签，判定只看内容**：闸门**不**从文件名推断 sha / 范围 / 归属，所以文件名叫错
  不会改变任何判定。这也意味着改名 / 重排不会造成口径漂移。
- **append-only（与 vendored `show-me-your-work` 同源）**：目录形式下"追加一条" = **新建一个文件**；
  写错要改 ⇒ **再加一条新文件**，**不去改既有文件**。本闸门**无法**机械证明这一点（与旧单文件
  同样属于声明式输入）—— 它是纪律，不是判据，别把它读成已被强制。

`.sh` 参考实现**只读旧单文件**，所以两者的"同 tip 同结论"只在**目录为空**时逐项可比；
迁入目录后比的是**判定集**（三元组 + `❌` 集合），不是逐字 stdout——与既有的对照纪律一致。

信任边界（与 `.sh` 相同，来自协议 §7 第 8 条）：台账是**声明式输入**——本闸门只能证明
"每条 commit 都有归属"，**不能**证明审查真实发生过；审计窗口左端由台账自己决定；台账从
**工作树**读（不读 HEAD 版）⇒ 必须在**干净检出**上跑，否则脏改台账能骗过闸门。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ⚠ 与 `.sh:51` **逐字符相同**。协议按行号引用该模式，别在这里"顺手优化"。
# 语义：白名单 commit 的改动必须**全部**命中这里（根级名带 `$` 锚，docs/ 下只认文档扩展名）。
DOC_PATTERN = r'^(docs/.*\.(md|txt|rst|tsv|json|ya?ml)$|AGENTS\.md|CLAUDE\.md|CONTEXT\.md|[^/]*\.md)$'
DOC_RE = re.compile(DOC_PATTERN)

LEDGER_PATH = "docs/review_ledger.tsv"

#: 台账目录（issue #293）：**一文件一条**。不存在 = 空（过渡期两处都认，见模块 docstring "双读过渡"）。
LEDGER_DIR = "docs/review_ledger.d"
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40,64} (commit|tag) \d+$")

FAIL_HELP = """
闸门失败。处置（二选一，不要改台账蒙过去）：
  · 对这些 commit **补一次审查**（两轴 /code-review，范围写进台账的新行）；
  · 或者：若它们确实只是文档改动 ⇒ 在台账 [whitelist] 段声明（脚本会校验 docs-only）。
  协议原文：docs/SDD_WORKFLOW_PROTOCOL.md §7。
"""


def _utf8_stdio() -> None:
    """沙箱/Windows 控制台默认可能是 GBK ⇒ 中文与 ✅ 会 UnicodeEncodeError。输出统一 UTF-8/LF。"""
    for stream in (sys.stdout, sys.stderr):
        # 不用 try/except-pass（ruff S110 + BLE001 会报）：reconfigure 是 3.7+ 的公开 API，
        # 用 hasattr 守卫"被替换成非 TextIOWrapper 的流"这一种情况即可。
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace", newline="\n")


def git(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def die(msg: str) -> None:
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()
    sys.exit(1)


# --------------------------------------------------------------------------- #
# 台账解析（与 `.sh` 的 `while IFS= read -r line` 逐条对齐）
# --------------------------------------------------------------------------- #

def split_fields(row: str, count: int) -> list[str]:
    """等价于 bash 的 `IFS=$'\t' read -r a b c`。

    ⚠ 必须**折叠连续 tab**（并剥掉首尾 tab）：tab 属 IFS whitespace，bash 的 `read`
    会把连续分隔符当一个、并去掉首尾空白。用 `str.split("\t")` 会把这些行解析成不同
    结果 —— 那是真实的**口径漂移**（2026-09-22 由 Spec 轴独立审查指出），不是风格问题。
    第 `count` 个字段吃掉**剩余全部内容**（含其中的 tab），与 bash 把余下词并进最后一个
    变量的行为一致。

    ⚠ **它不是"为未来的输入"造的防御**（两轴 Standards 轴质疑过这一点，成立）：当前
    `docs/review_ledger.tsv` 的 103 行审查行里，**0 行**含连续 / 首尾 tab（审查者实测），
    即今天的新旧切法逐行全等。保留它的理由是**语义等价**：这个函数的定义就是 bash 的
    `IFS=$'\t' read`，而"与冻结的 `.sh` 口径等价"是双实现对照能被接受的前提。
    若判定这仍属投机抽象 ⇒ 可回退为 `row.split("\t", count - 1)`，代价是口径不再严格等价。
    """
    fields = re.split(r"\t+", row.strip("\t"), maxsplit=count - 1)
    return fields + [""] * (count - len(fields))


def read_ledger(path: str) -> tuple[list[str], list[str]]:
    """返回 (审查行, 白名单行)。空行与 `#` 注释跳过；`[whitelist]` 切换段。"""
    with open(path, "rb") as fh:
        raw = fh.read()
    text = raw.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if lines and lines[-1] == "":        # `read -r` 不会为结尾换行产出一行
        lines.pop()

    rows: list[str] = []
    wl: list[str] = []
    section = "review"
    for line in lines:
        # `${line%$'\r'}` 再 `${line#$BOM}`：只剥一个 \r、只剥行首 BOM——
        # 顺序与 `.sh` 相同（早期版本把 BOM 剥离写错过，两轴 P3 实测）。
        line = line.removesuffix("\r").removeprefix("\ufeff")
        if line == "" or line.startswith("#"):
            continue
        if line == "[whitelist]":
            section = "wl"
            continue
        (rows if section == "review" else wl).append(line)
    return rows, wl


def _rel(path: str) -> str:
    """仓库相对路径（正斜杠）—— 打印统一用这个形态（台账路径会在 `[whitelist]` 行里出现）。"""
    return os.path.relpath(path, REPO_ROOT).replace(os.sep, "/")


def ledger_dir() -> str:
    """台账目录的绝对路径（`LEDGER_DIR` 环境变量可覆盖，测试用）。"""
    raw = os.environ.get("LEDGER_DIR") or LEDGER_DIR
    return raw if os.path.isabs(raw) else os.path.join(REPO_ROOT, raw)


def is_ledger_path(path: str) -> bool:
    """这个路径是不是台账文件本身（旧单文件，或新目录下的一条 `.tsv`）。"""
    return path == LEDGER_PATH or (path.startswith(LEDGER_DIR + "/") and path.endswith(".tsv"))


def is_ledger_only(files: list[str]) -> bool:
    """恰好只改**一个**台账文件 ⇒ 记账动作自动放行。

    ⚠ 这是对 `.sh` 原判据（`== [docs/review_ledger.tsv]`）的**等价扩展**，不是放松：仍然要求
    **恰好一个**文件，且那个文件必须是台账（旧单文件 / 新目录下的 `.tsv`）。夹带任何其他文件
    （含 `scripts/`、`.md`、`.zcodeignore`）一律回落到正常判定。
    不扩展的话，目录形式会重现死循环：登记一条 → 需要一个白名单行 → 白名单行又是新文件 → …
    """
    return len(files) == 1 and is_ledger_path(files[0])


def read_all(legacy: str) -> tuple[list[str], list[str], int, int, list[str]]:
    """读**两处**台账并求并集：返回 `(审查行, 白名单行, 旧文件行数, 目录行数, 告警)`。

    旧单文件在前、目录按**文件名排序**在后——顺序只影响打印与 `base` 的比较顺序，**不影响判定**：
    `base` 取的是"能被 HEAD 到达的最早那个"（按可达性比较，不是按行序），因此两处合并的先后
    不改变三元组。
    """
    warns: list[str] = []
    paths = [legacy]
    d = ledger_dir()
    if os.path.isdir(d):
        for entry in sorted(os.listdir(d)):
            full = os.path.join(d, entry)
            if not os.path.isfile(full):
                continue
            if not entry.endswith(".tsv"):
                warns.append(f"⚠️  {_rel(full)} 不是 .tsv，已忽略（目录只收 .tsv）")
                continue
            paths.append(full)
    rows: list[str] = []
    wl: list[str] = []
    n_old = 0
    for path in paths:
        is_legacy = path == legacy
        file_rows, file_wl = read_ledger(path)
        rows += file_rows
        wl += file_wl
        if is_legacy:
            n_old = len(file_rows)
        elif not file_rows and not file_wl:
            # 不失败：一个"什么都没声明"的文件不豁免任何 commit（不构成放松）；但它十有八九是写错了。
            # ⚠ 只对**目录**下的文件告警：旧单文件在完全迁走之后本来就只剩注释，
            # 对"正常终态"刷警告会把告警训练成噪音。
            warns.append(f"⚠️  {_rel(path)} 里没有审查行/白名单行（空文件或只有注释）⇒ 贡献 0 条归属")
    return rows, wl, n_old, len(rows) - n_old, warns


# --------------------------------------------------------------------------- #
# 提交图：一次 rev-list 拿全图，之后全靠内存可达性（不再逐条起子进程）
# --------------------------------------------------------------------------- #

def load_graph() -> dict[str, list[str]]:
    proc = git("rev-list", "--parents", "HEAD")
    if proc.returncode != 0:
        die(f"❌ `git rev-list --parents HEAD` 失败：{proc.stderr.strip()}")
    graph: dict[str, list[str]] = {}
    for line in proc.stdout.split("\n"):
        parts = line.split()
        if parts:
            graph[parts[0]] = parts[1:]
    return graph


def make_ancestors(graph: dict[str, list[str]]):
    cache: dict[str, set[str]] = {}

    def ancestors(sha: str) -> set[str]:
        """`sha` 自身 + 其全部祖先（与 `merge-base --is-ancestor A B` 的 A==B 语义一致）。"""
        hit = cache.get(sha)
        if hit is not None:
            return hit
        seen: set[str] = set()
        stack = [sha]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(graph.get(cur, ()))
        cache[sha] = seen
        return seen

    def is_ancestor(maybe_anc: str, descendant: str) -> bool:
        return maybe_anc in ancestors(descendant)

    return ancestors, is_ancestor


def resolve_many(revs: list[str]) -> dict[str, str | None]:
    """一次 `git cat-file --batch-check` 解掉全部 `<rev>^{commit}`；失败映射为 None。"""
    uniq = sorted(set(revs))
    if not uniq:
        return {}
    proc = git("cat-file", "--batch-check", input_text="".join(f"{r}^{{commit}}\n" for r in uniq))
    out: dict[str, str | None] = {}
    lines = proc.stdout.split("\n")
    for rev, line in zip(uniq, lines):
        line = line.strip()
        out[rev] = line.split(" ")[0] if FULL_SHA_RE.match(line) else None
    return out


def short_and_subject(shas: list[str]) -> dict[str, tuple[str, str]]:
    """一次调用批量取 (短 sha, subject)。

    `%h` 与 `git rev-parse --short` 是**同一套** abbreviation 算法（实测 8/8 逐条相等），
    但 `rev-parse --short` **只接受单个 rev**（134 个参数直接 `fatal: Needed a single revision`），
    所以必须走 `log --no-walk`。按 sha 建字典 ⇒ 不依赖输出顺序。
    """
    if not shas:
        return {}
    proc = git("log", "--no-walk", "--format=%H%x09%h%x09%s", *shas)
    out: dict[str, tuple[str, str]] = {}
    for line in proc.stdout.split("\n"):
        if "\t" not in line:
            continue
        full, _, rest = line.partition("\t")
        short, _, subj = rest.partition("\t")     # 只切两次：subject 里再有 tab 也留着
        out[full] = (short, subj)
    return out


def files_of(shas: list[str]) -> dict[str, list[str]]:
    """`git show --no-renames --pretty=format: --name-only` 的批量版（逐条对齐 `.sh:129,150`）。

    用 `%x01%H` 作提交间哨兵。merge 走 `--cc`（与单条 `git show <merge>` 相同）⇒ 两侧都不同的
    文件才列出；**空表 = "核对不了"**，`.sh` 对此 fail-closed。
    """
    if not shas:
        return {}
    proc = git("show", "--no-renames", "--pretty=format:%x01%H", "--name-only", *shas)
    out: dict[str, list[str]] = {}
    for block in proc.stdout.split("\x01")[1:]:
        lines = block.split("\n")
        sha = lines[0].strip()
        if not FULL_SHA_RE.match(f"{sha} commit 0"):  # 哨兵后第一行必须是完整 sha
            continue
        # `awk 'NF'` 等价：丢掉全空白行，保留原始文件名（不 strip，避免改掉 `quotePath` 的引号形式）
        out[sha] = [ln for ln in lines[1:] if ln.strip()]
    return out


def main(argv: list[str]) -> int:
    _utf8_stdio()

    ledger = os.environ.get("LEDGER") or LEDGER_PATH
    if not os.path.isabs(ledger):
        ledger = os.path.join(REPO_ROOT, ledger)
    list_only = "--list" in argv

    if not os.path.isfile(ledger):
        die(f"找不到台账 {os.environ.get('LEDGER') or LEDGER_PATH}")

    rows, wl, n_old, n_new, ledger_warns = read_all(ledger)
    if not rows:
        die("台账里没有审查行")

    graph = load_graph()
    ancestors, is_ancestor = make_ancestors(graph)
    head_sha = resolve_many(["HEAD"])["HEAD"]
    if head_sha is None:
        die("❌ 解不出 HEAD")

    # 先把全部 rev 一次解掉（快路径），再**按台账行序**做同样的 fail-closed 校验（错误文案同 `.sh`）。
    revs: list[str] = []
    parsed: list[tuple[str, str, str, str]] = []
    for row in rows:
        date, desc, rng = split_fields(row, 3)
        rbase = rng.split("..")[0]
        rtip = rng.rsplit("..", 1)[-1]
        parsed.append((date, desc, rbase, rtip))
        revs += [rbase, rtip]
    resolved = resolve_many(revs)

    rows_desc = f"审查范围（台账，{len(rows)} 行" + (f" = 旧单文件 {n_old} + 目录 {n_new}" if n_new else "") + "）:"
    print(rows_desc)
    for w in ledger_warns:
        print(w)

    covered: set[str] = set()
    base_literal = ""
    base_sha = ""
    for date, desc, rbase, rtip in parsed:
        b_sha = resolved.get(rbase)
        t_sha = resolved.get(rtip)
        if b_sha is None:
            die(f"❌ 台账 base 不存在: {rbase}（{desc}）")
        if t_sha is None:
            die(f"❌ 台账 tip 不存在: {rtip}（{desc}）")
        if not is_ancestor(t_sha, head_sha):
            die(f"❌ 台账 tip 不是 HEAD 的祖先: {rtip}（{desc}）")
        # base 必须是 tip 的祖先：否则 `rev-list tip --not base` 在非线性历史（base 取自旁支/merge）
        # 下会**静默扩大**覆盖范围。当前区间无 merge，属预防性 fail-closed。
        if not is_ancestor(b_sha, t_sha):
            die(f"❌ 台账 base 不是 tip 的祖先: {rbase}..{rtip}（{desc}）")
        # `git rev-list <tip> --not <base>` = anc(tip) \ anc(base)
        covered |= ancestors(t_sha) - ancestors(b_sha)
        print(f"  {date}  {desc:<44} {rbase}..{rtip}")
        if not base_literal:
            base_literal, base_sha = rbase, b_sha
        # 最靠前的 base：取能到达 HEAD 的边界里最早的那个（按提交序比较）
        if is_ancestor(b_sha, base_sha):
            base_literal, base_sha = rbase, b_sha

    if resolved.get(base_literal) is None:
        die(f"❌ 计算出的 base 不存在: {base_literal}")
    # ⚠ base 也必须是 HEAD 的祖先：台账手抄错一格（base 抄到 HEAD 或更后）⇒ `rev-list HEAD --not base`
    # 为空 ⇒ "提交总数 0 / 待判定 0" ⇒ **exit 0 假绿**，白名单校验整个被跳过——那正是 #213 的失效
    # 形态（fixed point 手抄错误静默豁免一票）。闸门必须在这里显式失败而不是通过。
    if not is_ancestor(base_sha, head_sha):
        die(f"❌ 台账的最早 base 不是 HEAD 的祖先: {base_literal}（台账手抄错了？）")

    all_set = ancestors(head_sha) - ancestors(base_sha)
    total = len(all_set)
    if total == 0:
        die("❌ 覆盖区间为空（base..HEAD 没有提交）——台账 base 抄错或没有新提交可审")

    missing = sorted(all_set - covered)
    miss_n = len(missing)
    cov_n = total - miss_n

    print()
    # 短 sha 的**显示宽度**是环境属性（git 的 abbreviation 依入参形态与 git 版本而变），
    # 不是实现差异：本行与 `.sh:104`（`git rev-parse --short "$base"`）都走 git 缩写，
    # 同一提交在本会话见过 8 位、在别的调用上下文见过 7 位。
    # ⇒ 双实现对照**比判定集，不比读数文本**；机制与全量对照读数见
    #   `docs/agents/SDD_ACCELERATION_AUDIT.md` §9.4（本注释不重复叙述，§16.1）。
    print(f"覆盖区间: {short_and_subject([base_sha])[base_sha][0]}..HEAD")
    print(f"提交总数 {total} / 已审查 {cov_n} / 待判定 {miss_n}")

    if list_only:
        # 只打印：但把"还有缺口"如实反映到退出码（否则任何只查 $? 的自动化用法都会失去闸门作用）
        return 2 if miss_n else 0

    fail = 0
    used_wl: list[str] = []
    meta = short_and_subject(missing)
    filemap = files_of(missing)

    for sha in missing:
        short, subject = meta.get(sha, (sha[:7], ""))
        # 台账自身的记账动作**自动放行**：恰好只改 docs/review_ledger.tsv 的提交机械可验、藏不了代码；
        # 而"把这件事记进台账"本身又要被记账是**死循环**（实测 2026-09-17 绕了三轮）。
        # 收窄条件：夹带任何其他文件（含 scripts/）即回落到正常判定。
        if is_ledger_only(filemap.get(sha, [])):
            print(f"✅ 台账自身更新（自动放行）: {short}  {subject}")
            continue

        reason = ""
        for w in wl:
            if w.startswith((f"{short}\t", f"{sha}\t")):
                reason = w.split("\t", 1)[1]
                used_wl.append(w)
        if not reason:
            print(f"❌ 未审查且未声明: {short}  {subject}")
            fail = 1
            continue

        files = filemap.get(sha, [])
        if not files:
            # 合并提交的 name-only 默认无输出：**核对不了就不放行**，但别把"核对不了"说成"改了非文档文件"
            print(f"❌ 白名单只收 docs-only，但 {short} 的改动文件**核对不了**（合并提交？）（{reason}）")
            fail = 1
            continue
        bad = [f for f in files if not DOC_RE.match(f)]
        if bad:
            print(f"❌ 白名单只收 docs-only，但 {short} 改了非文档文件（{reason}）:")
            for f in bad:
                print(f"     {f}")
            fail = 1
        else:
            print(f"✅ 白名单(docs-only): {short}  {subject} — {reason}")

    if fail:
        print(FAIL_HELP)
        return 1

    # 死条目告警（不失败）：白名单写了但**本次一条都没用上**的 sha——要么已被审查行覆盖（冗余，
    # 可删），要么 sha 抄错。两轴 P3 实测发现首例（`e125e27`：早已被审查行覆盖，永不进入待判定集）。
    for w in wl:
        if w not in used_wl:
            print(f"⚠️  白名单条目本次未被用到（冗余或 sha 抄错）: {w.split(chr(9))[0]}")

    # 口径门：断言的是"每条 commit 都有台账归属"，**不是**"审查确实发生过"——台账是声明式输入，
    # 审查行的真实性由人对账（详见 docs/SDD_WORKFLOW_PROTOCOL.md §7 第 8 条的信任边界）。
    print(f"✅ 台账覆盖闸门通过：{base_literal}..HEAD 每条 commit 均有归属（审查行 / 白名单 / 台账记账）。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except BrokenPipeError:
        # ⚠ **不许 exit 0**：那正是 `.sh:126-128` 明写要堵的"防 SIGPIPE 假绿"形状
        # （管道被提前关闭 ≠ 闸门跑通了）。自写解析器不得把"没验"变成"通过"。
        sys.stderr.write("覆盖闸门：输出管道被提前关闭，闸门未完成 —— 这不算通过。\n")
        sys.exit(1)
