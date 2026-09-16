#!/usr/bin/env bash
# check_review_coverage.sh —— 交付前闸门：**没有任何 commit 未经 review 就进 main**
#
# 为什么需要（两个实测案例，都在 2026-09-17 复验时才发现）：
#   ① #213 的 e2e 落在批次边界上：批次的 fixed point（`6f81c6e`）恰好是它自己的末条
#      commit ⇒ 那条 commit 不在任何审查范围内，**从未被任何 review 读过**（v2 协议里
#      "fixed point = 上一批审查结束时的 commit" 是靠人抄进 tracker 的一个数字，抄错
#      一格就静默豁免一票，没有任何东西会报错）。
#   ② 修复提交结构性免疫：协议表格自己写着"修复 commit = 下一批的 fixed point"，于是
#      **交付周期里最后一次审查的修复提交没有下一批**。实测 `9f2a8f8` 就是这样，而复验
#      证明它带着一个真 bug（凭据删除 fail-open）。
#
# 闸门规则（机械、无自由裁量）：
#   1. `docs/review_ledger.tsv` 的审查行构成"已审查范围"的并集；
#   2. `<最早 base>..HEAD` 的**每条** commit 必须落在某个已审查范围内；
#   3. 例外只有 [whitelist] 段，且**脚本自己校验**该 commit 是 docs-only（改动文件全部
#      命中文档模式）——代码 commit 永远不能靠白名单放行，只能去补一次审查。
#
# 用法：
#   scripts/check_review_coverage.sh              # 检查（默认台账 docs/review_ledger.tsv）
#   LEDGER=path/to.tsv scripts/check_review_coverage.sh
#   scripts/check_review_coverage.sh --list       # 只打印范围与覆盖情况
#
# 退出码：0 = 全绿；1 = 有未审查的 commit（或台账本身有问题）。
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
# ⚠ 根级名分支必须带 `$` 锚：不带的话 `AGENTS.md.bak` / `CLAUDE.md.orig` 这类备份文件也算
# docs-only（实测 grep 0 命中=放行）——备份文件可能是**旧版规则**，用它的提交不该进白名单。
# `web/PRODUCT.md` 分支其实是冗余的（`[^/]*\.md$` 已覆盖），保留是为了把"这个例外文件"写显眼。
DOC_PATTERN='^(docs/|AGENTS\.md|CLAUDE\.md|CONTEXT\.md|[^/]*\.md$)'

rows=()
wl=()
section="review"
while IFS= read -r line; do
  line="${line%$'\r'}"
  line="${line#\$'\xef\xbb\xbf'}"   # BOM（fail-closed，但剥掉才能让白名单 SHA 匹配上）
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

if [ "$LIST_ONLY" = "1" ]; then exit 0; fi   # 临时文件由 EXIT trap 收

fail=0
while read -r sha; do
  [ -z "$sha" ] && continue
  short=$(git rev-parse --short "$sha")
  subject=$(git log -1 --pretty=%s "$sha")
  reason=""
  # 台账自身的记账动作**自动放行**：恰好只改 docs/review_ledger.tsv 的提交机械可验、藏不了代码；
  # 而"把这件事记进台账"本身又要被记账是**死循环**（实测 2026-09-17 绕了三轮：白名单声明 → 台账
  # 补记 → 惯例块，每轮都产出新的待记账提交）。收窄条件：夹带任何其他文件（含 scripts/）即回落
  # 到正常判定——85310b4 含 scripts/ 改动，实测仍被拦下要求审查。
  lfiles=$(git show --pretty=format: --name-only "$sha" | awk 'NF')
  if [ -n "$lfiles" ] && ! printf '%s\n' "$lfiles" | grep -Evq '^docs/review_ledger\.tsv$'; then
    printf '✅ 台账自身更新（自动放行）: %s  %s\n' "$short" "$subject"
    continue
  fi
  for w in ${wl[@]+"${wl[@]}"}; do
    # 整行匹配（不经 word-splitting 的字段拆分）：原因文本含空格时 `read -r wsha wreason`
    # 仍把剩余部分当**一个** reason；逐行比对而非拆词，避免含空格的原因被拆散后误匹配
    case "$w" in
      "$short"$'\t'*) reason="${w#*$'\t'}" ;;
    esac
  done
  if [ -z "$reason" ]; then
    echo "❌ 未审查且未声明: $short  $subject"
    fail=1
    continue
  fi
  files=$(git show --pretty=format: --name-only "$sha" | awk 'NF')
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
  协议原文：docs/SDD_WORKFLOW_PROTOCOL.md §5。
EOT
  exit 1
fi
echo "✅ 审查覆盖闸门通过：$base..HEAD 无未审查的代码提交。"
