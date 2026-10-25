#!/usr/bin/env python3
"""`check_line_coverage_ratchet.py` —— 覆盖率**基线留痕**与**变更可执行行棘轮**（Issue #1499）。

## 它解决什么（以及它不解决什么）

本仓 CI 的覆盖率（`.github/workflows/gate1.yml` / `gate-nightly.yml` 的
`--cov=src/agent_harness --cov-report=term --cov-report=xml:artifacts/coverage.xml`，命令逐字未改）
**只打印、不判定**。Issue #1499 的已裁结论 11（A 案）**不设全局硬门限**，只做两件事：

1. **基线留痕**：把「这次测量跑在哪棵树上、多大范围、排除了什么、原始报告在哪」变成一份
   可复查的记录（`baseline` 子命令），并让**缺少测量 / 报告不是这棵树的**在判定时**明确失败**
   —— 旧报告不得假绿。
2. **变更行棘轮**：PR 的**变更可执行行**当分母，阈值 60%（票面定）；未改行、白名单行、
   显式声明的纯移动文件**不进分母**；**全仓总率只记录、永不判定**（`ratchet` 子命令）。

## 为什么不用 coverage.py 的 `fail_under`（B 案对照）

coverage.py 官方配置文档原文：*"If the total coverage measurement is under this value, then exit
with a status code of 2."*，并明写 *"If you specify a non-integral value, you must also set
`[report] precision` properly to make use of the decimal places."* 与 *"A setting of 100 will fail
any value under 100, regardless of the number of decimal places of precision."*
⇒ 它是**全仓单一硬阈值**：① 对精度/取整敏感；② 平台盲区（容器 / 非本平台实现）直接变成假红；
③ 与历史欠账耦合，单人项目里会把「没人能悄悄删测试」的收益换成「改一行也过不了」的等待代价。
本票的 A 案把判定面收窄到**本次变更**，全仓总率退回观察位。

分级机制的一手旁证（只作机制参考，不是本票的实测结论）：Lighthouse CI 的 `assertions` 三级
—— `warn` 级原文 *"the result will be printed to stderr, but failure will not result in a
non-zero exit code"*，只有 `error` 级才非零退出（未列出的审计默认 `off`）；pa11y-ci 的
`--threshold` 原文 *"permit this number of errors, warnings, or notices, otherwise fail with exit
code 2"*，且支持**按页面**单独设阈值 —— 即"可观测但不阻断 / 分范围容忍"是成熟产品的既有形态。

## 输入、输出与退出码

    # ① 基线留痕（CI 在跑完覆盖率之后调用；产物随 artifacts/ 一起上传）
    python scripts/check_line_coverage_ratchet.py baseline \
        --report artifacts/coverage.xml --out artifacts/coverage-baseline.json \
        --scope "pytest tests + --cov=src/agent_harness"

    # ② 变更行棘轮（PR 侧；本地或 CI 均可，读的是同一份 artifacts/coverage.xml）
    python scripts/check_line_coverage_ratchet.py ratchet \
        --report artifacts/coverage.xml --baseline artifacts/coverage-baseline.json \
        --base origin/main

退出码：`0` = 通过（含**空集合**语义，见下）/ `1` = 棘轮未达标 / `2` = **不可判**
（报告或基线缺失、报告与基线不是同一份文件、基线测量的树不是当前树、`git diff` 读不出来
——含 `--base` 与 HEAD 没有共同祖先、`--base` 解不出、白名单不合法）。

## 判定口径（机械，全部可复核）

- **分母** = `git diff -U0 <base>...HEAD` 里 `src/` 下 **`.py`** 的**新增行号** ∩ 报告里该文件的
  **可执行行** —— 即「本次改动引入的、本可以被执行到的行」。未改动的行永远不进分母；
  非 `.py` 文件没有可执行行，既不进分母、也不构成"不可判"。
- **分子** = 分母里报告命中数 > 0 的行。
- **通过条件**用整数比较：`分子 * 100 >= 分母 * 60`（**恰好 60% 通过**）。不走浮点，
  所以没有 coverage.py `fail_under` 那条「对 precision / 取整敏感」的随机红。
- **白名单**（`docs/agents/coverage-ratchet.whitelist.json`）按**精确文件路径**逐条给
  `reason` + `platform`；**不许通配、不许目录前缀、不许整域排除**（解析期 fail-closed）。
  命中白名单的文件，其变更行**不进分母**，并在输出里逐条打印理由。
  ⚠ 白名单判定**先于**「报告里没有这个文件」的不可判判定：盲区的主用途正是"报告里根本没有它"
  （容器内代码 / 平台专有实现），顺序颠倒会让白名单永远救不了它该救的文件。
- **纯移动重构**：只有显式传 `--move-only-note "<说明>"` **且** git `-M` 判为 **`R100`（逐字相同）**
  的文件才跳过；`R<100`（移动 + 修改）与没有说明都照常计入分母 —— `-M` 默认阈值是 50% 相似度，
  那是"改过"不是"纯移动"（不存在"看起来像移动就自动放过"的通道）。
- **全仓总率**（报告 `lines-valid` / `lines-covered`）只打印、只写进基线，**不参与判定**。
- **零可执行变更**（分母为空）⇒ 输出 `RATCHET EMPTY` 并 **exit 0**。理由：把空集合判成失败会让
  docs-only / 纯移动 PR 无故变红（正是 A 案要消灭的等待代价），判成"不可判"同样阻断正常 PR；
  所以选**通过 + 显式打印**，让"这次没有任何可执行行被改"成为一条**看得见**的结论而不是静默绿。
"""

from __future__ import annotations

import argparse
import codecs
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime


class Undecidable(Exception):
    """判定前提不成立（读不出 diff / 报告不是这棵树的 / 缺测量）⇒ 调用方退 2，绝不退化成绿。"""


#: 仓库根。默认取脚本所在仓库；`COVERAGE_RATCHET_REPO` 只服务于测试里对临时仓库跑真 CLI
#: （不设时行为完全不变）。所有 `git` 调用都在它下面跑。
REPO_ROOT = (os.environ.get("COVERAGE_RATCHET_REPO")
             or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: 棘轮门槛（票面 60%；整数比较，见模块 docstring）。
RATCHET_THRESHOLD_PERCENT = 60

#: 白名单默认落点。`docs/agents/` 与 `verification.map.tsv` 同族：它是**流程输入**，不是产品代码。
DEFAULT_WHITELIST = os.path.join("docs", "agents", "coverage-ratchet.whitelist.json")

#: 变更面的扫描面：`src/` 下 **`.py`** 的新增行。非 `.py` 文件（例如
#: `src/agent_harness/attachments/image_media_types.snapshot.txt` 这类快照）在覆盖率报告里
#: 没有行号、本来就不是可执行行 ⇒ 不进分母，也不能把棘轮打成"不可判"。
SCOPE_PREFIX = "src/"
SCOPE_SUFFIX = ".py"


# --------------------------------------------------------------------------- #
# 覆盖率报告（coverage.py 的 Cobertura XML —— CI 已产出的那一份，不新增测量）
# --------------------------------------------------------------------------- #
def parse_coverage_xml(path: str, repo_root: str = REPO_ROOT) -> tuple[dict, dict]:
    """读 coverage.py 的 `--cov-report=xml`，返回 (`{仓库相对路径: {行号: 命中数}}`, `{行数统计}`)。

    路径换算：XML 的 `<class filename>` 是**相对 `<sources>`** 的，所以
    `仓库相对路径 = relpath(<source>, repo_root) / filename`。`<source>` 不在本仓库内时
    （例如跑的是别的 checkout 的报告）该文件被**跳过并计数**，由调用方打印 —— 静默丢弃会让
    "报告不是这棵树的"这件事看不见。
    """
    tree = ET.parse(path)
    root = tree.getroot()
    sources = [s.text.strip() for s in root.iter("source") if s.text and s.text.strip()]
    files: dict[str, dict[int, int]] = {}
    outside = 0
    for cls in root.iter("class"):
        name = cls.get("filename")
        if not name:
            continue
        rel = _repo_relative(name, sources, repo_root)
        if rel is None:
            outside += 1
            continue
        hits = files.setdefault(rel, {})
        for line in cls.iter("line"):
            hits[int(line.get("number"))] = int(line.get("hits") or 0)
    totals = {
        "lines_valid": int(root.get("lines-valid") or 0),
        "lines_covered": int(root.get("lines-covered") or 0),
        "files_outside_repo": outside,
    }
    return files, totals


def _repo_relative(filename: str, sources: list[str], repo_root: str) -> str | None:
    """`<class filename>`（相对 `<source>`）→ 仓库相对路径。

    coverage.py 的 XML 里 `filename` 是**相对 `<source>`** 的；但也存在"直接把仓库相对路径
    写进 filename"的报告形状（`<source>` 指向仓库根时两者同形）。⇒ 先取**盘上真的存在**的那个，
    再退回结构上可接受的第一个 —— 返回值只被当作"报告覆盖了这个文件"的键，落错会把
    "改了却没测"错判成"测了"，所以宁可返回 None（由调用方判不可判）。
    """
    filename = filename.replace("\\", "/")  # Windows 分隔符的报告也认
    # `<source>` 一个都不在仓库内（报告来自别的 checkout）⇒ candidates 为空 ⇒ 返回 None，
    # 由调用方计数并打印（"报告不是这棵树的"必须看得见，不能静默丢）。
    inside = [item for item in sources if _inside(item, repo_root)]
    candidates: list[str] = [] if sources else [filename]  # 无 <source> 时 filename 即仓库相对
    for source in inside:
        for candidate in (os.path.join(source, filename), filename):
            rel = os.path.relpath(os.path.abspath(candidate), repo_root)
            if not rel.startswith(".."):
                candidates.append(rel.replace(os.sep, "/"))
    for rel in candidates:
        if os.path.isfile(os.path.join(repo_root, rel)):
            return rel
    return candidates[0] if candidates else None


def _inside(path: str, repo_root: str) -> bool:
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(repo_root))
    return not rel.startswith("..")


def _unquote(raw: str) -> str:
    """git 对含特殊字符的路径输出 C 风格的带引号形式（`"b/\344\270\255.py"`）。

    普通 UTF-8 路径由 `-c core.quotePath=false` 直接给出字面量；这里只兜住仍然被引号包住的
    极端路径（含引号 / 控制字符），解不出来就原样返回（宁可让该文件判"未测量"，不可错绑）。
    """
    if len(raw) < 2 or not (raw.startswith('"') and raw.endswith('"')):
        return raw
    try:
        return codecs.escape_decode(raw[1:-1].encode())[0].decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return raw


# --------------------------------------------------------------------------- #
# git 面
# --------------------------------------------------------------------------- #
def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )


def rev_parse(rev: str) -> str | None:
    """解一个成立的对象；解不出返回 None（fail-closed，调用方据此判"不可判"）。"""
    proc = git("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def changed_lines(base: str, head: str = "HEAD") -> dict[str, set[int]]:
    """`base...head`（merge-base 口径）里被**新增**的行号：`{仓库相对路径: {行号}}`。

    用 `--no-renames`：重命名按「删旧 + 加新」计，于是"移动"是否豁免由 `renamed_paths()` 单独决定，
    两条判据不互相污染。`-U0` 保证每个 hunk 只有纯增块，`+c,0`（纯删）不产生行。
    """
    proc = git("-c", "core.quotePath=false", "diff", "--no-color", "--no-ext-diff",
               "--no-renames", "--unified=0", f"{base}...{head}")
    if proc.returncode != 0:
        # rc≠0 = diff 读不出来（最常见：两侧没有共同祖先，`fatal: no merge base`）。
        # **绝不能**当成"没有变更" —— 那会把一次读失败洗成 RATCHET EMPTY 的绿。
        raise Undecidable(
            f"git diff {base}...{head} 读不出来（rc={proc.returncode}）：{proc.stderr.strip()}")
    out: dict[str, set[int]] = {}
    current: str | None = None
    pending_header = False  # 只有紧跟 `--- ` 的 `+++ ` 才是文件头
    for raw in proc.stdout.splitlines():
        if raw.startswith("diff --git "):
            current, pending_header = None, False
            continue
        if pending_header:
            pending_header = False
            if raw.startswith("+++ "):
                path = _unquote(raw[4:].strip()).removeprefix("b/")
                # 只看扫描面内的 .py；面外的文件连 hunk 都不解析（否则 `@@` 会往不存在的键上写）。
                current = path if (path.startswith(SCOPE_PREFIX)
                                   and path.endswith(SCOPE_SUFFIX)) else None
                if current:
                    out.setdefault(path, set())
            continue
        if raw.startswith("--- "):
            # 内容里以 `++ ` 开头的新增行在 patch 里长成 `+++ …`；不设这道状态机就会把它
            # 误当文件头 ⇒ 该 hunk 剩余行**少算**分母（反向口子）。
            pending_header = True
            continue
        if raw.startswith("@@") and current:
            match = re.search(r"\+(\d+)(?:,(\d+))?", raw)
            if not match:
                continue
            start = int(match.group(1))
            count = int(match.group(2) or 1)
            out[current].update(range(start, start + count))
    return {k: v for k, v in out.items() if v}


def renamed_paths(base: str, head: str = "HEAD") -> set[str]:
    """`-M` 判出来的**逐字相同**（`R100`）的 rename 目标路径。

    只认 `R100`：`git -M` 的默认阈值是 50% 相似度，`R077` 是"移动 + 改动"，那部分新代码必须
    照常进分母（票面 AC3 说的是**纯移动重构**）。`-z` 让路径免于引号/制表符转义。
    """
    proc = git("-c", "core.quotePath=false", "diff", "--no-color", "--name-status", "-M", "-z",
               f"{base}...{head}")
    if proc.returncode != 0:
        raise Undecidable(
            f"git diff --name-status {base}...{head} 读不出来（rc={proc.returncode}）："
            f"{proc.stderr.strip()}")
    fields = [field for field in proc.stdout.split("\0") if field]
    out: set[str] = set()
    index = 0
    while index < len(fields):
        status = fields[index]
        if status.startswith(("R", "C")):
            if index + 2 >= len(fields):
                break
            if status == "R100":
                out.add(fields[index + 2])
            index += 3
        else:
            index += 2  # 状态 + 一个路径
    return out


# --------------------------------------------------------------------------- #
# 白名单
# --------------------------------------------------------------------------- #
def load_whitelist(path: str) -> dict[str, dict]:
    """读盲区白名单；**类型**不对 ⇒ TypeError、**取值**不对 ⇒ ValueError（两者都被调用方判不可判）。

    形状护栏是为了 AC3「不把整个安全域排除来过闸」：每条必须是**精确文件路径** + 非空
    `reason` / `platform`。通配符、目录前缀、空字段一律拒绝。
    """
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        doc = json.load(handle)
    entries = doc.get("entries")
    if not isinstance(entries, list):
        raise TypeError(f"{path}: 顶层必须是 {{\"entries\": [...]}}")
    table: dict[str, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise TypeError(f"{path}: entries 里每一项都必须是对象")
        target = entry.get("path")
        reason = entry.get("reason")
        plat = entry.get("platform")
        if not isinstance(target, str) or not target:
            raise ValueError(f"{path}: 缺 path")
        if target.endswith("/") or any(ch in target for ch in "*?[") or ".." in target:
            raise ValueError(f"{path}: {target!r} 必须是精确文件路径（禁通配 / 目录前缀 / 上跳）")
        if target != target.strip() or target.startswith(("./", "/")):
            raise ValueError(
                f"{path}: {target!r} 必须是规范化的仓库相对路径（无 `./` / `/` 前缀与首尾空白）")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"{path}: {target!r} 缺 reason")
        if not isinstance(plat, str) or not plat.strip():
            raise ValueError(f"{path}: {target!r} 缺 platform")
        if target in table:
            raise ValueError(f"{path}: {target!r} 重复")
        table[target] = {"reason": reason.strip(), "platform": plat.strip()}
    return table


# --------------------------------------------------------------------------- #
# 子命令：baseline
# --------------------------------------------------------------------------- #
def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cmd_baseline(args: argparse.Namespace) -> int:
    if not os.path.isfile(args.report):
        print(f"❌ 基线失败：找不到覆盖率报告 {args.report}（缺测量 ⇒ 不许回落读旧报告）")
        return 2
    head = rev_parse(args.head)
    if not head:
        print(f"❌ 基线失败：{args.head} 解不出 commit")
        return 2
    _, totals = parse_coverage_xml(args.report)
    record = {
        "schema": "coverage-baseline/1",
        "sha": head,
        "tree": (git("rev-parse", f"{head}^{{tree}}").stdout or "").strip(),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "platform": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "measurement_scope": args.scope,
        "exclusions": {
            "whitelist": args.whitelist,
            "note": "白名单只排除**列出的精确文件**的变更行；整域排除会被解析期拒绝",
        },
        "report": {"path": args.report, "sha256": _sha256(args.report)},
        # 全仓总率**只记录**：任何判定都不得读这两个字段（见 ratchet 子命令）。
        "totals_recorded_only": totals,
    }
    out = args.out or os.path.join("artifacts", "coverage-baseline.json")
    parent = os.path.dirname(out)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{out}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, out)
    print(f"✅ 基线留痕已写入 {out}")
    print(f"   sha={head} tree={record['tree']}")
    print(f"   平台={record['platform']} python={record['python']}")
    print(f"   测量范围={record['measurement_scope']}")
    print(f"   原始报告={args.report} sha256={record['report']['sha256'][:16]}…")
    print(f"   全仓总率（只记录、不判定）：{totals['lines_covered']}/{totals['lines_valid']}")
    return 0


# --------------------------------------------------------------------------- #
# 子命令：ratchet
# --------------------------------------------------------------------------- #
def cmd_ratchet(args: argparse.Namespace) -> int:
    fail = _verify_inputs(args)
    if fail is not None:
        print(f"❌ 棘轮不可判：{fail}")
        return 2

    # 基线已在 `_verify_inputs` 里读过并校验过（schema / sha / tree / 报告 sha256），不重复读。
    report, totals = parse_coverage_xml(args.report)
    if totals["files_outside_repo"]:
        print(f"⚠ 报告里有 {totals['files_outside_repo']} 个文件的 <source> 不在本仓库内，已跳过")

    try:
        whitelist = load_whitelist(args.whitelist)
    except (TypeError, ValueError, OSError) as exc:
        print(f"❌ 棘轮不可判：白名单不合法 —— {exc}")
        return 2

    base = rev_parse(args.base)
    if not base:
        print(f"❌ 棘轮不可判：--base {args.base} 解不出 commit")
        return 2
    try:
        changed = changed_lines(args.base, args.head)
        moved = renamed_paths(args.base, args.head) if args.move_only_note else set()
    except Undecidable as exc:
        print(f"❌ 棘轮不可判：{exc}")
        return 2

    denominator_lines: dict[str, set[int]] = {}
    skipped_whitelist: list[tuple[str, set[int], dict, bool]] = []
    skipped_moved: list[tuple[str, set[int]]] = []
    unmeasured: list[str] = []
    for path, lines in sorted(changed.items()):
        executable = report.get(path)
        if path in whitelist:
            # 白名单**必须先于**"未测量"判定：盲区的主用途正是"报告里根本没有这个文件"
            # （容器内代码 / 平台专有实现）。放在后面会让这份白名单永远救不了它该救的文件。
            hit = lines & set(executable) if executable is not None else lines
            skipped_whitelist.append((path, hit, whitelist[path], executable is None))
            continue
        if executable is None:
            # 改了 src/ 下的文件却不在报告里：测量面没覆盖到它 ⇒ 不可判（不是"没有可执行行"）。
            unmeasured.append(path)
            continue
        hit = lines & set(executable)
        if not hit:
            continue
        if path in moved:
            skipped_moved.append((path, hit))
        else:
            denominator_lines[path] = hit

    if unmeasured:
        print("❌ 棘轮不可判：以下文件被改动但不在覆盖率报告里（测量范围没覆盖 / 报告不是这棵树的）：")
        for path in unmeasured:
            print(f"   - {path}")
        return 2

    if args.move_only_note and not skipped_moved:
        print(f"⚠ --move-only-note 给了说明，但 git -M 没判出任何 R100（逐字相同的纯移动）"
              f"（说明未生效，仅记录）：{args.move_only_note.strip()[:80]}")

    total = sum(len(v) for v in denominator_lines.values())
    covered = sum(
        1 for path, lines in denominator_lines.items() for line in lines if report[path][line] > 0
    )

    head_sha = rev_parse(args.head) or ""
    print(f"覆盖率棘轮：base={base[:12]} head={head_sha[:12]}")
    print(f"  报告={args.report}（基线 sha256 对账通过）")
    print(f"  全仓总率（只记录、不判定）：{totals['lines_covered']}/{totals['lines_valid']}")
    if skipped_whitelist:
        print(f"  白名单排除 {len(skipped_whitelist)} 个文件的变更行：")
        for path, lines, entry, blind in skipped_whitelist:
            where = "报告里没有该文件（盲区）" if blind else f"{len(lines)} 行"
            print(f"   - {path}（{where}；platform={entry['platform']}；理由={entry['reason']}）")
    if skipped_moved:
        print(f"  纯移动跳过 {len(skipped_moved)} 个文件（说明：{args.move_only_note.strip()}）：")
        for path, lines in skipped_moved:
            print(f"   - {path}（{len(lines)} 行）")

    if total == 0:
        print("RATCHET EMPTY：本次没有变更的可执行行 ⇒ 空集合语义 = 通过（不是静默通过，"
              "也不是失败；理由见脚本 docstring）")
        return 0

    passed = covered * 100 >= total * RATCHET_THRESHOLD_PERCENT
    verdict = "PASS" if passed else "FAIL"
    ratio = covered * 100 / total
    print(f"  变更可执行行：{covered}/{total} = {ratio:.2f}%（门槛 {RATCHET_THRESHOLD_PERCENT}%，"
          f"恰好等于门槛为通过）")
    for path, lines in sorted(denominator_lines.items()):
        missed = sorted(line for line in lines if report[path][line] == 0)
        if missed:
            print(f"   - 未覆盖 {path}: {_ranges(missed)}")
    print(f"RATCHET {verdict}")
    return 0 if passed else 1


def _verify_inputs(args: argparse.Namespace) -> str | None:
    """判定前的三道机械前置；返回非 None = 不可判（把理由原样打出去）。"""
    if not os.path.isfile(args.report):
        return f"找不到覆盖率报告 {args.report}（缺测量 ⇒ 不许回落读旧报告）"
    if not os.path.isfile(args.baseline):
        return f"找不到基线记录 {args.baseline}（先跑 baseline 子命令留痕）"
    try:
        with open(args.baseline, encoding="utf-8") as handle:
            baseline = json.load(handle)
    except (OSError, ValueError) as exc:
        return f"基线记录读不出来：{exc}"
    if baseline.get("schema") != "coverage-baseline/1":
        return f"基线记录 schema 不认识：{baseline.get('schema')!r}"
    for key in ("sha", "tree"):
        value = baseline.get(key)
        if not isinstance(value, str) or not value:
            return f"基线记录缺 {key}（或类型不对）：{value!r}"
    recorded = (baseline.get("report") or {}).get("sha256")
    actual = _sha256(args.report)
    if recorded != actual:
        return (f"报告与基线不是同一份文件（基线记 {str(recorded)[:16]}… / 实测 {actual[:16]}…）"
                " ⇒ 旧报告不得假绿")
    head = rev_parse(args.head)
    if not head:
        return f"{args.head} 解不出 commit"
    tree = (git("rev-parse", f"{head}^{{tree}}").stdout or "").strip()
    # 判据是**树**，不是 SHA：内容相同的树就是被测量的那棵树（协议 §8.1 的读数传递口径）。
    # 一个空提交会改 SHA 而不改树 —— 那份测量对当前代码依然成立，不该因此判不可判。
    if baseline.get("tree") != tree:
        return (f"基线测量的树 {str(baseline.get('tree'))[:12]} ≠ 当前 {tree[:12]}"
                f"（基线 sha={str(baseline.get('sha'))[:12]} / 当前 HEAD={head[:12]}）"
                " ⇒ 这份测量不是这棵树的，判定无依据")
    return None


def _ranges(lines: list[int]) -> str:
    """`[1,2,3,7,9,10]` → `1-3, 7, 9-10`（只为人读，不参与判定）。"""
    out: list[str] = []
    start = prev = lines[0]
    for line in lines[1:]:
        if line == prev + 1:
            prev = line
            continue
        out.append(f"{start}-{prev}" if prev > start else str(start))
        start = prev = line
    out.append(f"{start}-{prev}" if prev > start else str(start))
    return ", ".join(out)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    base = sub.add_parser("baseline", help="把一次覆盖率测量记成可复查的基线")
    base.add_argument("--report", required=True, help="coverage.py 的 Cobertura XML")
    base.add_argument("--out", default=os.path.join("artifacts", "coverage-baseline.json"))
    base.add_argument("--scope", required=True, help="测量范围的文字记录（测试选择 + --cov 目标）")
    base.add_argument("--whitelist", default=DEFAULT_WHITELIST)
    base.add_argument("--head", default="HEAD")
    base.set_defaults(func=cmd_baseline)

    ratchet = sub.add_parser("ratchet", help="对变更的可执行行做棘轮判定")
    ratchet.add_argument("--report", required=True)
    ratchet.add_argument("--baseline", required=True)
    ratchet.add_argument("--base", default="origin/main")
    ratchet.add_argument("--head", default="HEAD")
    ratchet.add_argument("--whitelist", default=DEFAULT_WHITELIST)
    ratchet.add_argument("--move-only-note", default="",
                        help="显式声明「本 PR 是纯移动/重命名」的说明；不给则移动按新增计")
    ratchet.set_defaults(func=cmd_ratchet)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
