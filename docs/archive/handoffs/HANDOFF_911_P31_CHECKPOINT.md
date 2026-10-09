<!-- 断点交接（一次性）：#911 P3-1 施工中断用的续跑断点，随本票落进版本控制。 -->
# #911 P3-1 收尾状态（2026-10-10）

- 分支 `claude/911-p31-gate-exclusions`，worktree `/home/hatch/workspace/intelligence-agent-wt/911-p31`
- **已 push**，**PR #918 已开**（base=main）：https://github.com/EricKingWhy/intelligence-agent/pull/918
- 基线 origin/main `8edade99`；已做 **2 次先回后正同步**（merge `6f39f595` 合 8f55233c；merge `27078c50` 合 cab16c30）
- 推送 tip：`git rev-parse HEAD`（本文件写入时为 `6e9aa9ad`）
- **代码面自被审 SHA `e2c8b8ff` 起逐字节未变**（`git diff e2c8b8ff..HEAD -- src/ tests/` 为空；
  `derive.py` blob `5d3a4fa3724d85c73394b696e6bea48ba1c3d9b8`）；其后全部为 docs-only（台账/归档/tracker/收据）

## 门禁
- 本机 Gate-0：`f9c1ed02` / tree `b2f191ab` **6/6 PASS**（22.6s，收据 `docs/gate/f9c1ed02….json`）；
  更早数枚 6/6（含合并树 `350b6d81`）均在库
- 覆盖闸门 `check_review_coverage.py`：**exit 0**，0 条 ❌
- **集成树全量（终态读数）**：tip `76de42ce` / tree `b14dd2cd`（含 main #890 侧代码），
  `uv sync --locked --all-extras` + `pnpm install --frozen-lockfile`，日志
  `/home/hatch/pytest-911/verify-911v5.log` —— **7185 passed / 0 failed / 27 skipped / 51 deselected
  （425.82s）**；沙箱 Gate-0 同树 **6/6 PASS**（收据 `docs/gate/76de42ce….json`，随该树生成）
- 前一轮同法读数（预合并树 `6f39f595`）：7142 passed / **1 failed** / 27 skipped（406.55s），
  唯一红 = `test_real_ledger_passes_after_the_431_fix`（根因 = 台账行当时未入库，与产品代码无因果
  关系）；该红在集成树上已消失（同文件隔离复跑 **4 passed**）
- CI：GitHub 必需 `gate0` **pass**（44s）、PR 级 Chromium E2E 冒烟 **pass**（5m35s），`mergeStateStatus=CLEAN`

## 环境陷阱（本票实测踩到，续跑必读）
- worktree 的 `.venv` 是**指向主 clone `intelligence-agent/src` 的 editable 安装**（`site-packages/*.pth`
  里写死了主 clone 路径）⇒ 在 worktree 里直接 `python -m pytest` 会**静默测到主 clone 的旧代码**，
  本票新增用例会报 4 red（假红）。跑本票测试须显式 `PYTHONPATH="$PWD/src"`；本机验证命令：
  `PATH="$PWD/.venv/bin:$PATH" PYTHONPATH="$PWD/src" python -m pytest tests/session/test_derive_direct_user_input.py`
  （实测 17 passed）。沙箱侧无此问题（clone 后 `uv sync` 自建）。
- 用 `sbx --cloud exec` 前台 `nohup ... &` 启动的长任务会在该 exec 会话结束时被一并杀掉
  （表现为进程消失、日志停在半途、loadavg 归零）⇒ 须 `setsid nohup ... & disown`。

## 审查（四轮，零未闭合 finding）
发现阶段两轴（正确性 PASS 2×P4 / 标准 PASS-WITH-FINDINGS 2×P2+1×P3）→ 修后重审两轴
（A 轴 P4-a 采纳 / P4-b 经独立复核实为**假阳性**故不改代码 / P3 闭合；B 轴上一轮 3 条全闭合 + 4 条记账面 findings 全处置）
→ 窄复验 C 轴（三问全闭合，自写探针 0 分叉 / 20 例）→ 窄复验 D 轴（三问全闭合，无新 finding）。
报告：`/home/hatch/pytest-911/review3/{A-correctness,B-standards,C-closure,D-closure2}.md`

## 未做（按 §14.4 交用户）
- **未 merge、未关 Issue #911**（用户未授权；PR 已就绪等 CI 必需检查绿）
- 一条**协议条文冲突**登记在 `docs/SDD_TICKET_TRACKER.md` 的 #911 状态行，供用户裁断

## 断点续跑怎么做
1. `cd` 到 worktree，`git fetch origin && git log --oneline -1` 核对 tip
2. CI 必需检查已全绿、`mergeStateStatus=CLEAN`（本文件写入时）⇒ 已报告 Muse，等 merge 授权
3. 若 main 又前进 ⇒ 按 §14.6 再做一次同步合并 + 补机械归属行 + Gate-0（本轮已做两遍，流程同上）
