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

#: 描述字段 lint 的规则名集合（issue #295 / 缺陷 7）。**测试按集合断言**：
#: 加规则不该让守卫红，删 / 改名现有规则必须红。
LINT_RULES = ("unbalanced_backtick", "empty_parens", "control_chars", "overlong", "unbalanced_bold")

#: 描述字段长度硬上限 = 协议 §8.5 的值。它同时是 lint 的 `overlong` 阈值。
LINT_LINE_LIMIT = 800

#: Unicode 私用区（U+E000..U+F8FF + 两个补充私用区）。这两个区段本身合法但**不承载语义**，
#: 出现在台账描述里几乎只能来自"文本被二进制 / 编码转换污染"。
_PUA_RE = re.compile("[\ue000-\uf8ff\U000f0000-\U000ffffd\U00100000-\U0010fffd]")

#: 控制字符（除 \t 与 \r）：NUL 是其中最典型的一个，但 BS / ESC 等同样不该出现在台账里。
_CONTROL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: 行内代码（一对反引号夹住的内容）。**私有**：lint 与 glob 豁免都用它把"代码里的符号"摘出去。
#: 只认**单反引号**定界（`` `code` ``）。**双反引号定界**（`` `` `x` `` ``，CommonMark 里用来包住
#: 含反引号的代码）由 `_MULTI_BACKTICK_RE` 先摘 —— 否则被包住的单反引号会被数成裸反引号，
#: 造成 `unbalanced_backtick` 假阳性（issue #295 两轴审查实测：协议 §8.9 自己就踩了这条）。
_MULTI_BACKTICK_RE = re.compile(r"(`{2,}).+?\1")
_INLINE_CODE_RE = re.compile(r"`[^`]*`")


def _strip_code_spans(desc: str) -> str:
    """摘掉**双反引号定界**的行内代码（可能内嵌单反引号），再摘单反引号行内代码。

    顺序不可换：先摘双的 —— 否则 `` `` a`b `` `` 会被单反引号规则切成两半、留下半个定界。
    """
    return _INLINE_CODE_RE.sub("", _MULTI_BACKTICK_RE.sub("", desc))

#: **glob 里**的 `**`：`src/**`、`tests/**`、`docs/**` 这类。它们不是 markdown 粗体定界符，
#: 粗体奇偶检查必须先豁免它们（2026-09-23 实测：历史台账 4 条奇数 `**` 行里，
#: `099-37e8c4d-d165740.tsv` 的奇数**全部**来自 glob ⇒ 不豁免就是纯假阳性）。
#:
#: 合法的**粗体对**（`**x**`，内容非空且不以 `*` 起）。**先摘它、再判剩余奇偶** ——
#: 顺序是关键：靠单条正则同时处理「粗体对」与「glob」时，glob 模式一定会误吃粗体的闭合 `**`
#: （2026-09-23 两轴审查 P2 实测：`**A** 与 **B 未闭合` 被判"偶数" ⇒ 半截粗体静默漏报）。
_BOLD_PAIR_RE = re.compile(r"\*\*(?=[^\s*])(?:[^*]|\*(?!\*))*?\*\*")

#: **glob 里**的 `**`：`src/**`、`tests/**`、`docs/**` 这类。它们不是 markdown 粗体定界符，
#: 粗体奇偶检查必须豁免（2026-09-23 实测：历史台账 4 条奇数 `**` 行里，
#: `099-37e8c4d-d165740.tsv` 的奇数**全部**来自 glob ⇒ 不豁免就是纯假阳性）。
#: 形状 = 紧跟在路径字符后、且后面是 `/`、空白、标点或行尾。
#: **已知局限（刻意不修）**：同一行里「半截粗体 + 裸 glob」共存、且两者相加恰好凑成偶数时
#: （如 `**B 未闭合，另见 src/** 目录` ⇒ 1 颗半截 + 1 颗 glob = 2 颗）仍会漏报。
#: 无法用正则消歧——`src/**` 与 `**B` 的字符形状完全同构，要真判需要 markdown 解析器。
#: 处置：这是**假阴性**（漏报），不是假阳性；且该形状在真实台账里未出现。
#: 真正的兜底是「有半截粗体必然伴随行被截断 ⇒ `overlong` / 语义审查会发现」。登记于此，
#: 由 issue #295 两轴审查轮确认接受。
_GLOB_STARS_RE = re.compile(r"(?<=[A-Za-z0-9_./-])\*\*(?=[/\s,，。）)]|$)")

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
        # 用 git 视角枚举台账目录，而不是 `os.listdir`（R2 P1-c）：`os.listdir` 会读到被
        # `.git/info/exclude` / `.gitignore` 藏起的伪造 `.tsv`，而守卫（`git status -uall` /
        # `git ls-files --others --exclude-standard`）看不见它 ⇒ 伪造审查行能溜进判定集、
        # 把 exit 1 伪造成 exit 0。统一到 git 视角后两者口径一致：藏起的文件**两边都不认**。
        # ⚠ 但 `LEDGER_DIR` 可被环境变量覆盖为**仓库外**路径（测试用）：仓库外目录不是 git
        # 管理的，git 视角列不出任何文件（`ls-files` 只列仓库内路径），且跨盘时 `os.path.relpath`
        # 会抛 `ValueError`。这种情况**回退 `os.listdir`**——那是测试临时目录，不涉及本仓的
        # exclude 绕过面；盲目用 git 视角会静默跳过或崩溃（R3 修后重审 P3/P4，两轴独立指出）。
        try:
            rel = os.path.relpath(d, REPO_ROOT)
            in_repo = rel != os.pardir and not rel.startswith(os.pardir + os.sep)
        except ValueError:
            in_repo = False
        if in_repo:
            listed: set[str] = set(git("ls-files", "--cached", "--", rel.replace(os.sep, "/").rstrip("/") + "/").stdout.splitlines())
            listed |= set(git("ls-files", "--others", "--exclude-standard", "--", rel.replace(os.sep, "/").rstrip("/") + "/").stdout.splitlines())
            for entry in sorted(listed):
                if not entry:
                    continue
                if not entry.endswith(".tsv"):
                    warns.append(f"⚠️  {entry} 不是 .tsv，已忽略（目录只收 .tsv）")
                    continue
                paths.append(os.path.join(REPO_ROOT, entry.replace("/", os.sep)))
        else:
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
# 描述字段 lint（issue #295 / 缺陷 7）
# --------------------------------------------------------------------------- #

def lint_description(desc: str, row: str | None = None) -> list[dict]:
    """体检一条台账描述字段，返回命中列表（每条 `{"rule", "why"}`）。**纯函数、永不抛**。

    为什么需要它（B-43 真实事故）：用 `python -c "…"` 把含**反引号**的中文写进台账白名单行 ⇒
    Git Bash 在双引号内对反引号做**命令替换**，两处文件名被**静默吞掉**；更糟的是第二个反引号
    区间的文本被当成脚本执行，**在仓库根创建了 7 个 0 字节垃圾文件**。而覆盖闸门**完全没报警** ——
    归属只看 sha 前缀（描述字段是自由文本、不参与判定），截断后的行照样让闸门 exit 0。

    它**不**证明描述语义正确（那是散文，脚本判不了真伪）；它挡的是"文本被机械损坏"这一族：
    被吞掉的反引号区间、被截断的行、二进制污染。**默认为 warn**，`--strict` 才升 fail ——
    历史台账里有已知的截断行（本模块的守卫测试硬编码了其中一条作正控），
    立刻改成 fail 会让闸门在存量上红；「新行从严、存量登记」是惯用做法。
    """
    hits: list[dict] = []
    # `overlong` 按协议 §8.5 量**整行**；未传 `row` 时退化为只量 `desc`（单字段调用者）。
    whole = desc if row is None else row

    def add(rule: str, why: str) -> None:
        hits.append({"rule": rule, "why": why})

    if not isinstance(desc, str):      # 全定义：任何输入都不许抛
        add("control_chars", f"描述不是字符串（{type(desc).__name__}）——台账解析出错了？")
        return hits

    # 反引号奇偶：**先摘掉行内代码**（含双反引号定界的），再数剩下的裸反引号。
    # issue #295 两轴审查实测：不摘的话 `` `` a`b `` `` 这类合法写法会被数成奇数 ⇒ 假阳性。
    stripped = _strip_code_spans(desc)
    if stripped.count("`") % 2:
        add("unbalanced_backtick",
            f"裸反引号 {stripped.count('`')} 个（奇数）⇒ 必有区间没闭合。**这条最常见于"
            "命令替换把整段代码吞掉**，被吞掉的往往正是文件名 / sha / 路径")
    # 裸空括号：行内代码已在上面摘掉 —— `` `f()` `` 里的括号是代码，不是瑕疵。
    if "（）" in stripped or "()" in stripped:
        add("empty_parens",
            "存在空的圆括号对（全角或半角）⇒ 括号里的内容没了。真实事故里它是**反引号区间"
            "被整段吞掉**留下的坑（`把「加载更早」点到全量（）`）")
    if _CONTROL_RE.search(desc) or "\x00" in desc:
        add("control_chars", "含控制字符（NUL / BS / ESC 之属）⇒ 文本被二进制污染")
    if _PUA_RE.search(desc):
        add("control_chars", "含 Unicode 私用区字符（U+E000..U+F8FF 等）⇒ 编码转换污染")
    # 量法必须与协议 §8.5 第 1 条一致：**整行**（含 `date\tdesc\trange` 三列），不是只量 desc 列。
    # issue #295 两轴审查 P1：早期只量 desc 列 ⇒ 与协议口径系统性不等（一条 700 字符 desc +
    # 300 字符 range 的行走协议该报、走实现不报）。`row` 缺省为 `desc` 以兼容单字段调用。
    if len(whole) > LINT_LINE_LIMIT:
        add("overlong", f"整行长 {len(whole)} 字符 > 上限 {LINT_LINE_LIMIT}（协议 §8.5 第 1 条的硬上限，量法 = 三列合计）")
    # 粗体奇偶：**先豁免 glob 里的 `**`**（`src/**` / `tests/**`），再数剩下的。
    # 粗体奇偶：**两段式** —— 先摘合法粗体对，再摘 glob，最后数剩余 `**` 的奇偶。
    # 顺序不可换（issue #295 两轴审查 P2 实测）：靠单条 glob 正则去豁免时，它一定会
    # 把粗体的闭合 `**` 当 glob 吃掉 ⇒ `**A** 与 **B 未闭合` 被判"偶数" ⇒ 半截粗体漏报。
    bold_probe = _GLOB_STARS_RE.sub("", _BOLD_PAIR_RE.sub("", stripped))
    if bold_probe.count("**") % 2:
        add("unbalanced_bold",
            f"粗体定界符 `**` 剩 {bold_probe.count('**')} 颗（奇数，已豁免成对粗体与 glob）⇒ "
            "有半截粗体没闭合，通常伴随**行被截断**")
    return hits


def lint_rows(rows: list[str], wl: list[str]) -> list[dict]:
    """对台账全部行跑 lint，返回 `[{"where", "line", "text", "hits"}, ...]`。

    `where` = `"review"` / `"whitelist"`（**打印时要能指回是哪一段**，否则用户不知道该去
    哪个区块改）。描述字段的取值口径跟判定循环一致：
      · 审查行 = `split_fields(row, 3)` 的第 2 个字段（范围描述）；
      · 白名单行 = `row.split("\t", 1)[1]`（原因），**没有 tab 的行整行当描述**
        ——与 `main()` 里 `w.split("\t", 1)[1]` 的既有形状对齐，缺 tab 的行在那里本来就拿不到原因。
    """
    out: list[dict] = []
    for line, row in enumerate(rows, 1):
        desc = split_fields(row, 3)[1]
        if desc:
            found = lint_description(desc, row)
            if found:
                out.append({"where": "review", "line": line, "text": row, "hits": found})
    for line, w in enumerate(wl, 1):
        desc = w.split("\t", 1)[1] if "\t" in w else ""
        if desc:
            found = lint_description(desc, w)
            if found:
                out.append({"where": "whitelist", "line": line, "text": w, "hits": found})
    return out


def format_lint_report(findings: list[dict], total_rows: int) -> list[str]:
    """把 lint 结果渲染成要打印的行（**逐条给规则名 + 原因 + 截断样本**）。

    打印策略：每条命中一行摘要（`⚠️ lint <where>:<line> …`），**不打印整行原文**
    ——历史行的描述动辄上千字符，全打出来会把闸门的正常输出淹掉。
    截断到 100 字符足以定位（配 `where` + `line` 就能在文件里找到）。
    """
    lines = [f"⚠️  台账描述字段 lint：{len(findings)} 行命中（共体检 {total_rows} 行；默认 warn，`--strict` 升 fail）"]
    for f in findings:
        rules = ",".join(h["rule"] for h in f["hits"])
        snippet = f["text"][:100].replace("\r", "")
        lines.append(f"     {f['where']}:{f['line']}  [{rules}]  {snippet}")
        for h in f["hits"][:2]:          # 每行最多展开两条原因，避免刷屏
            lines.append(f"         · {h['rule']}: {h['why']}")
    return lines


# --------------------------------------------------------------------------- #
# docs-only 按路径机械自动归属（issue #295 / 缺陷 5，方案 C）
# --------------------------------------------------------------------------- #

def is_docs_only(files: list[str]) -> bool:
    """一个提交的**全部**改动路径都命中 `DOC_PATTERN` ⇒ 可机械自动归属。

    为什么这是**更强**的证据（方案 C 的立论）：旧路径要求作者手写一行白名单声明，
    闸门只做 sha 前缀匹配 —— 归属证据是「作者说它是 docs-only」。本判据的输入是
    `git show --name-only` 给出的**路径客观事实**，作者无法伪造（他可以写错描述，
    但不能让 `scripts/x.py` 变成 `docs/x.md`）。

    ⚠ **与 `is_ledger_only` 并列、不替代**：那条管「恰好一个台账文件」，
    这条管「全是文档」。两者都自动放行，但都必须自己 fail-closed：
      · `files` 为空 ⇒ 返回 False。空表的成因是 merge 提交的 `--name-only` 默认无输出，
        或枚举失败 —— **「核对不了」不是「没问题」**（`.sh` 对此 fail-closed，本判据沿用）。
      · 模式**复用 `DOC_RE`**，不另造。另造一个更松的模式就是「在看不见的地方放松闸门」；
        `tests/tooling/test_review_coverage_lint.py` 里有一条逐例对账锁住这一点。
    """
    return bool(files) and all(DOC_RE.match(f) for f in files)


def format_auto_attribution(short: str, subject: str, files: list[str]) -> str:
    """渲染自动归属的一行（**逐条打印路径**，票面要求「保持可审计」）。

    归属从「作者声明」升级为「路径客观事实」之后，这个事实必须留在输出里 ——
    否则审计者只能看到一句「自动放行」，那就比旧的白名单行**更**不可审计了。
    多路径用 ` + ` 连；单路径直出。**不截断路径列表**（路径本身就短）。
    """
    return f"✅ docs-only（按路径自动归属）: {short}  {subject} — {' + '.join(files)}"


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
    # `--strict`：把描述字段 lint 的命中从 warn 升为 fail（issue #295 / 缺陷 7）。
    # **默认 warn** 的理由：历史台账有已知截断行，立刻 fail 会让闸门在存量上红；
    # 存量登记（本模块守卫测试里的硬编码正控 + 台账就地修回）之后才可把它设为默认。
    strict = "--strict" in argv

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

    # 描述字段 lint（缺陷 7）。⚠ **必须在任何判定前跑**：它是「方案 C 自动归属」的前置依赖
    # ——自动归属若先跑，一条被截断的 docs-only 行会被照放（票面明写的依赖顺序）。
    lint_findings = lint_rows(rows, wl)
    if lint_findings:
        for line in format_lint_report(lint_findings, len(rows) + len(wl)):
            print(line)

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

        # 方案 C（issue #295 / 缺陷 5）：**全部**改动路径都是文档 ⇒ 按路径机械自动归属。
        # 与白名单相比这是**更强**的证据（路径客观事实 > 作者声明），所以放在白名单之前，
        # 且**不需要**作者再写一行声明。⚠ 顺序有讲究：先 `is_docs_only` 再回落白名单，
        # 因为已有的大量白名单行现在只是冗余（不删也能跑 —— 白名单分支仍在）。
        files_docs = filemap.get(sha, [])
        if is_docs_only(files_docs):
            print(format_auto_attribution(short, subject, files_docs))
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

    if strict and lint_findings:
        print(f"❌ `--strict`：{len(lint_findings)} 行描述字段 lint 命中被升为失败")
        fail = 1

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
