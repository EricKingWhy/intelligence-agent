#!/usr/bin/env bash
# check_review_coverage.sh —— 交付前闸门：**`<最早台账 base>..HEAD` 的每条 commit 都必须在台账里有归属**
#
# 完整机制叙述（为什么存在、两类例外、**信任边界**）见 `docs/SDD_WORKFLOW_PROTOCOL.md` §7 第 8 条。
# 本文件只写**这段代码自己看不出来的操作约束**：
#   · 台账 = `docs/review_ledger.tsv`：审查行取并集；白名单段**逐条**自校验 docs-only；
#   · 例外两类：① [whitelist] 的 docs-only commit；② **恰好只改台账文件本身**的记账提交
#     （机械可验、藏不了代码，直接放行——"记账动作也要被记账"是死循环，实测绕了三轮）；
#   · 台账是从**工作树**读的，不读 HEAD 版 ⇒ 闸门必须在**干净检出**上跑：本地脏改台账
#     能骗过闸门，但只会**延迟**到下一次干净跑的失败，不会静默入 main。
#
# 用法 / 退出码：
#   scripts/check_review_coverage.sh              # 0 = 全绿；1 = 有未声明 commit 或台账有问题
#   LEDGER=path/to.tsv scripts/check_review_coverage.sh
#   scripts/check_review_coverage.sh --list       # 只打印范围与覆盖数；仍有缺口时退 2（便于接自动化）
set -euo pipefail
cd "$(dirname "$0")/.."

LEDGER="${LEDGER:-docs/review_ledger.tsv}"
LIST_ONLY=0
if [ "${1:-}" = "--list" ]; then LIST_ONLY=1; fi

[ -f "$LEDGER" ] || { echo "找不到台账 $LEDGER"; exit 1; }

# 临时文件：早退分支（台账 base 不存在等）也要收干净——否则"闸门报错"顺手在 /tmp 留垃圾。
# ⚠ 登记必须在**父进程**做：`covered="$(mktmp)"` 是命令替换子 shell，子 shell 里的
# `TMP_FILES+=` 对父进程不可见（97aa5ac 第一版就这么写的，实测每次跑漏 3 个文件）。
# 所以 mktmp 只返回路径，登记由调用方在父进程里显式做。
TMP_FILES=()
cleanup() {
  if [ "${#TMP_FILES[@]}" -gt 0 ]; then
    rm -f -- "${TMP_FILES[@]}"
  fi
}
trap cleanup EXIT
mktmp() {
  mktemp
}
reg() { TMP_FILES+=("$1"); }   # 父进程登记（调用方紧随 `$(mktmp)` 之后调用）

# 文档模式：白名单里的 commit 必须**全部**改动都命中这些（代码永远进不了白名单）。
# ⚠ 三个实测踩到的坑（两轴审查 P1/P2，均有复现命令）：
#   ① 根级名分支**必须带 `$` 锚**：不带的话 `AGENTS.md.bak` / `AGENTS.md.sh` / `CLAUDE.md.orig`
#      全被判成 docs-only（`grep -Evc` 得 0 = 放行）。备份文件可能是**旧版规则**，更不该放行。
#   ② **不能只看 `docs/` 前缀**：仓库里现成的 `docs/integration/verify-before-merge.sh`（可执行
#      脚本）与 `docs/tickets/tickets.json` 都在 docs/ 下，前缀判法会把真代码当散文放行
#      （实测：`85c427d` 加白名单一行即 exit 0）。所以 docs/ 下**只认文档扩展名**。
#   ③ 配合 `--no-renames`（见下文两处 git show）：否则 `git mv src/x.py docs/x.py` 这类提交的
#      文件表只有目标名 `docs/x.py`，原代码文件不出现，同样被误判为 docs-only。
# 注：`web/PRODUCT.md` 故意不收——它不是根级、也不在 docs/ 下，要改就走正常审查。
DOC_PATTERN='^(docs/.*\.(md|txt|rst|tsv|json|ya?ml)$|AGENTS\.md|CLAUDE\.md|CONTEXT\.md|[^/]*\.md)$'

rows=()
wl=()
section="review"
while IFS= read -r line; do
  line="${line%$'\r'}"
  line="${line#$'\xef\xbb\xbf'}"   # BOM：早期写成 `#\$'…'`（多一个反斜杠）⇒ 模式成了字面串，剥不掉（两轴 P3）
  case "$line" in
    ''|'#'*) continue ;;
    '[whitelist]') section="wl"; continue ;;
  esac
  if [ "$section" = "review" ]; then rows+=("$line"); else wl+=("$line"); fi
done < "$LEDGER"

[ "${#rows[@]}" -gt 0 ] || { echo "台账里没有审查行"; exit 1; }

base=""
covered="$(mktmp)"; reg "$covered"; : > "$covered"
printf '审查范围（台账，%d 行）:\n' "${#rows[@]}"
for r in "${rows[@]}"; do
  IFS=$'\t' read -r date desc range <<< "$r"
  rbase="${range%%..*}"; rtip="${range##*..}"
  git rev-parse --verify --quiet "$rbase^{commit}" >/dev/null || { echo "❌ 台账 base 不存在: $rbase（$desc）"; exit 1; }
  git rev-parse --verify --quiet "$rtip^{commit}"  >/dev/null || { echo "❌ 台账 tip 不存在: $rtip（$desc）"; exit 1; }
  git merge-base --is-ancestor "$rtip" HEAD || { echo "❌ 台账 tip 不是 HEAD 的祖先: $rtip（$desc）"; exit 1; }
  # base 必须是 tip 的祖先，否则 `rev-list tip --not base` 在非线性历史（base 取自旁支/merge）
  # 下会**静默扩大**覆盖范围（两轴 P3）。当前区间无 merge，属预防性 fail-closed。
  git merge-base --is-ancestor "$rbase" "$rtip" || { echo "❌ 台账 base 不是 tip 的祖先: $rbase..$rtip（$desc）"; exit 1; }
  git rev-list "$rtip" --not "$rbase" >> "$covered"
  printf '  %s  %-44s %s..%s\n' "$date" "$desc" "$rbase" "$rtip"
  if [ -z "$base" ]; then base="$rbase"; fi
  # 最靠前的 base：取能到达 HEAD 的边界里最早的那个（按提交序比较）
  if git merge-base --is-ancestor "$rbase" "$base"; then base="$rbase"; fi
done
sort -u -o "$covered" "$covered"

git rev-parse --verify --quiet "$base^{commit}" >/dev/null || { echo "❌ 计算出的 base 不存在: $base"; exit 1; }
# ⚠ base 也必须是 HEAD 的祖先：台账手抄错一格（base 抄到 HEAD 或更后）⇒ `rev-list HEAD --not base`
# 为空 ⇒ "提交总数 0 / 待判定 0" ⇒ **exit 0 假绿**，白名单校验也整个被跳过——那正是 #213 的
# 失效形态（fixed point 手抄错误静默豁免一票），闸门必须在这里显式失败而不是通过。
if ! git merge-base --is-ancestor "$base" HEAD; then
  echo "❌ 台账的最早 base 不是 HEAD 的祖先: $base（台账手抄错了？）"; exit 1
fi
all="$(mktmp)"; reg "$all"; git rev-list HEAD --not "$base" | sort -u > "$all"
total=$(wc -l < "$all" | tr -d ' ')
if [ "$total" = "0" ]; then
  echo "❌ 覆盖区间为空（base..HEAD 没有提交）——台账 base 抄错或没有新提交可审"; exit 1
fi
cov=$(comm -12 "$all" "$covered" | wc -l | tr -d ' ')
missing="$(mktmp)"; reg "$missing"; comm -23 "$all" "$covered" > "$missing"
miss_n=$(wc -l < "$missing" | tr -d ' ')

printf '\n覆盖区间: %s..HEAD\n' "$(git rev-parse --short "$base")"
printf '提交总数 %s / 已审查 %s / 待判定 %s\n' "$total" "$cov" "$miss_n"

if [ "$LIST_ONLY" = "1" ]; then
  # 只打印：但把"还有缺口"如实反映到退出码（否则任何只查 $? 的自动化用法都会失去闸门作用）
  if [ "$miss_n" != "0" ]; then exit 2; fi
  exit 0
fi

fail=0
# 记录本次**真正用到**的白名单条目（末尾据此告警死条目）——必须在循环前初始化：
# 早期版本把初始化写在循环之后，实测直接 `unbound variable` 崩在循环里（`set -u`）。
used_wl="$(mktmp)"; reg "$used_wl"; : > "$used_wl"
while read -r sha; do
  [ -z "$sha" ] && continue
  short=$(git rev-parse --short "$sha")
  subject=$(git log -1 --pretty=%s "$sha")
  reason=""
  # 台账自身的记账动作**自动放行**：恰好只改 docs/review_ledger.tsv 的提交机械可验、藏不了代码；
  # 而"把这件事记进台账"本身又要被记账是**死循环**（实测 2026-09-17 绕了三轮：白名单声明 → 台账
  # 补记 → 惯例块，每轮都产出新的待记账提交）。收窄条件：夹带任何其他文件（含 scripts/）即回落
  # 到正常判定——85310b4 含 scripts/ 改动，实测仍被拦下要求审查。
  # ⚠ 判等用**精确相等**而不是 `printf | grep -Evq`：后者在 `set -o pipefail` 下会因 `grep -q`
  # 提前退出触发 SIGPIPE（141），使整条管道非 0 ⇒ `!` 反相成功 ⇒ **跳过全部校验直接放行**
  # （两轴 P2 实测：文件表 ≥ 约 64 KB 管道缓冲即可触发；本仓库历史最多 75 个文件故尚未可达）。
  lfiles=$(git show --no-renames --pretty=format: --name-only "$sha" | awk 'NF')
  if [ "$lfiles" = "docs/review_ledger.tsv" ]; then
    printf '✅ 台账自身更新（自动放行）: %s  %s\n' "$short" "$subject"
    continue
  fi
  full=$(git rev-parse "$sha")
  for w in ${wl[@]+"${wl[@]}"}; do
    # 整行匹配（不经 word-splitting 的字段拆分）：原因文本含空格时 `read -r wsha wreason`
    # 仍把剩余部分当**一个** reason；逐行比对而非拆词，避免含空格的原因被拆散后误匹配。
    # 同时接受 7 位缩写与 40 位全长（否则人按 `git rev-parse HEAD` 粘全长 SHA 会被**静默丢弃**，
    # 文件里明明写着声明却报"未审查且未声明"）。
    case "$w" in
      "$short"$'\t'*) reason="${w#*$'\t'}"; printf '%s\n' "$w" >> "$used_wl" ;;
      "$full"$'\t'*)  reason="${w#*$'\t'}"; printf '%s\n' "$w" >> "$used_wl" ;;
    esac
  done
  if [ -z "$reason" ]; then
    echo "❌ 未审查且未声明: $short  $subject"
    fail=1
    continue
  fi
  files=$(git show --no-renames --pretty=format: --name-only "$sha" | awk 'NF')
  if [ -z "$files" ]; then
    # 合并提交的 --name-only 默认无输出：**核对不了就不放行**，但别把"核对不了"说成"改了非文档文件"
    echo "❌ 白名单只收 docs-only，但 $short 的改动文件**核对不了**（合并提交？）（$reason）"
    fail=1
    continue
  fi
  bad=$(printf '%s\n' "$files" | grep -Evc "$DOC_PATTERN" || true)
  if [ "$bad" != "0" ]; then
    echo "❌ 白名单只收 docs-only，但 $short 改了非文档文件（$reason）:"
    printf '%s\n' "$files" | grep -Ev "$DOC_PATTERN" | sed 's/^/     /'
    fail=1
  else
    printf '✅ 白名单(docs-only): %s  %s — %s\n' "$short" "$subject" "$reason"
  fi
done < "$missing"

if [ "$fail" != "0" ]; then
  cat <<'EOT'

闸门失败。处置（二选一，不要改台账蒙过去）：
  · 对这些 commit **补一次审查**（两轴 /code-review，范围写进台账的新行）；
  · 或者：若它们确实只是文档改动 ⇒ 在台账 [whitelist] 段声明（脚本会校验 docs-only）。
  协议原文：docs/SDD_WORKFLOW_PROTOCOL.md §7。
EOT
  exit 1
fi

# 死条目告警（不失败）：白名单写了但**本次一条都没用上**的 sha——要么已被审查行覆盖（冗余，
# 可删），要么 sha 抄错。两轴 P3 实测发现首例（`e125e27`：早已被审查行覆盖，永不进入待判定集）。
for w in ${wl[@]+"${wl[@]}"}; do
  grep -qxF -- "$w" "$used_wl" || printf '⚠️  白名单条目本次未被用到（冗余或 sha 抄错）: %s\n' "${w%%$'\t'*}"
done

# 口径门：断言的是"每条 commit 都有台账归属"，**不是**"审查确实发生过"——台账是声明式输入，
# 审查行的真实性由人对账（详见 docs/SDD_WORKFLOW_PROTOCOL.md §7 第 8 条的信任边界）。
echo "✅ 台账覆盖闸门通过：$base..HEAD 每条 commit 均有归属（审查行 / 白名单 / 台账记账）。"
