#!/usr/bin/env bash
# t508_exec_hardening_pressure.sh — #508 验收的压力复现/回归脚本：CPU 满载下
# 循环跑 tests/sandbox/test_exec_hardening.py（票面验收要求"压力复现脚本落盘"）。
#
# 用法:
#   bash scripts/t508_exec_hardening_pressure.sh [遍数]     # 默认 3
# 环境变量:
#   T508_LOG_DIR   读数落盘目录（默认 <仓库父目录>/t508_pressure_logs，仓库外）
#
# 负载形状 = 票面条件「全量 pytest 并行 + 高 CPU 负载」：python 燃烧器占满全部
# 逻辑核（scripts/_t508_burn_cpu.py）。⚠ 不要换成 bash 死循环燃烧器：实测 16 核
# bash 死循环把进程孵化拖到 10s+，真进程杀树用例的 1.5s kill 刺激在孵化完成前
# 就触发（started marker 永不出现）——那是任意进程孵化都可能被饿死的物理极限，
# 超出本票"负载敏感假红"的条件，且修复它必须拉长 kill 预算到 15s+，会击穿
# "墙钟不超 2 倍基线"的验收线。
#
# 输出: 每遍一行 `<遍号> EXIT=<rc> <pytest 末行>`（stdout + summary.txt）。
# 判定: 修复后 N 遍全绿（EXIT=0 / "31 passed"）；单文件墙钟不得劣化超过 2 倍基线。
# 负载时长 3600s 是上界：燃烧器子进程带同值 TTL（父被强杀时孤儿最多存活到该
# 上界），正常路径由 EXIT trap 杀树收口（taskkill //T / pkill -P）。
# 两条收口路径均已实证（2026-10-02）：脚本正常退出 → 零孤儿；父进程 kill -9
# （trap 不执行的最坏情况）→ 子进程 TTL 到期自行退出。
set -u
cd "$(dirname "$0")/.." || { echo "cannot cd to repo root" >&2; exit 1; }

N="${1:-3}"
LOG_DIR="${T508_LOG_DIR:-$(cd .. && pwd)/t508_pressure_logs}"
mkdir -p "$LOG_DIR"

CORES=$(nproc 2>/dev/null || echo 8)
PY=".venv/Scripts/python.exe"
[ -x "$PY" ] || PY=".venv/bin/python"
[ -x "$PY" ] || { echo "venv python not found at .venv/Scripts/python.exe or .venv/bin/python" >&2; exit 1; }

"$PY" scripts/_t508_burn_cpu.py "$CORES" 3600 >"$LOG_DIR/burn.log" 2>&1 &
BURN_PID=$!
# 杀树，不是只杀父进程：SIGTERM/TerminateProcess 绕过 atexit，multiprocessing
# 的 daemon 清理不会执行——只 kill $BURN_PID 会留下 N 个满核孤儿（子进程的
# TTL 只是兜底上界，正常收口靠这里）。
trap 'kill $BURN_PID 2>/dev/null
  taskkill //F //T //PID $BURN_PID >/dev/null 2>&1 || pkill -TERM -P $BURN_PID 2>/dev/null' EXIT
sleep 1

for i in $(seq 1 "$N"); do
  PYTHONUTF8=1 PYTHONPATH= "$PY" -m pytest tests/sandbox/test_exec_hardening.py \
    -q --no-header -p no:cacheprovider > "$LOG_DIR/run${i}.log" 2>&1
  rc=$?
  echo "run${i} EXIT=$rc $(tail -1 "$LOG_DIR/run${i}.log")" | tee -a "$LOG_DIR/summary.txt"
done
