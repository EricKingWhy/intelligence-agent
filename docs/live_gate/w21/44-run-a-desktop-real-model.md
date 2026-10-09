# W-21（#365）Run A：桌面客户端 + 真实模型的完整任务读数

父票 #365 [W-21] 要求两次互相独立的完整通过（Run A：安装后的**桌面**；Run B：安装后的 **TUI 冷启动**）。
本文件是 **Run A** 的读数；**不宣布 #365 Gate 通过**（还需 Run B 与双轴独立审查）。

## 环境（据实标注）

- 安装件：`Intelligence-Agent-Setup-0.1.0.exe`，sha256 `ce5fd54acf925bb6e17731e35990f18c52a0a90da57f834ad39960d0c6ad9880`（D8–D11 修复后构建，HEAD `80557a6b`）。
- 模型：`mimo-v2.6-flash`（provider `mimo`，`https://api.xiaomimimo.com/v1`），密钥值不记录。桌面进程由操作者带 `.env` 模型环境启动；服务由桌面派生。
- 样例：`tools/challenge-fixture/generate.py` 新生成的 `run-a-sample-20261008T030004`，样例应用由操作者托管在 `http://127.0.0.1:8911`（`host-fixture.py`：app.py 变更后自动重启）。
- 会话：`cbebff65-30fc-48be-86b1-5000ea906bbf`；事件 5725 条；4 个 run、1 次暂停、1 次恢复、4 次完成。
- 操作者窗口配置：`MAX_CONTEXT_TOKENS=64000`（首轮，用于触发 W-04 暂停）→ 产品默认 200000 → `120000`（用于让自动压缩在 turn 4 起始触发）；`MODEL_STREAM_IDLE_TIMEOUT=240`。
- 证据目录：`D:/w21-work/evidence/run-a/`；驱动脚本 `D:/w21-work/run-a.py`、`approve.py`、`browser-check.mjs`。

## 票面要求逐条

| 票面要求 | 读数 |
| --- | --- |
| 安装后的桌面启动真实模型 | 端点由桌面派生服务提供（pid 随重启变化，会话内 4 个 run 都在同一会话上） |
| 定位 → 修改 | 模型给出 B1/B2/B3 的文件与行号定位，最终改动集中在 `app.py` 两处；操作者 diff **85 行**（`run-a-fix.diff`） |
| 测试 | 5 条验收入口逐条真实 HTTP 复现，读数落在 `run-a-features.json` 与 judge |
| 真实浏览器页面验证 | 操作者用真实 Chrome（Playwright channel，headless）读页面，模型两次判读写数（`browser-reading.txt` / `-final`） |
| diff 审阅 | 模型自审 diff：无新依赖、未改判定器/测试、无 scope 外改动 |
| ≥1 次 compaction | `context/compacted` seq 4151：`compacted_turn_count=3`、`bracket_id=7ad352f2…`（真实模型摘要，8 节结构） |
| ≥1 次摘要调用失败 | 8 条 `context/compaction_failed`：2 条 `transport_error`（摘要模型不可达）+ 4 条真实模型 `plan_section_mismatch` + 2 条 `no_compactable_early_turn`；每次失败都保留原投影、零写入 |
| 新 context window 后复读 progress.md / 原 Event | turn 4（seq 4148）用「你现在处于新的上下文窗口」消息要求以磁盘为准复述；`progress.md` 增补「压缩后复核」节，7 条旧禁令与未完成项未漂移 |
| 判定器 | `run-a-judge.json`：6 项全 `ok: true`，`overall: pass`（R-042 failed/0 条、R-043 恰好 40、同 ID 同内容返回既有、不同内容 409、旧 7 条 hash 不变、页面与库一致） |
| 用户接受状态 | features.json 7 项 `passes: true`（`kill-reconcile` 保持 false —— 那条属 Run B/W-16 的 kill 腿）；`progress.md` 落盘 17754 B |

## W-04 暂停与恢复（票面「至少一次 compaction/摘要失败」之外的设计路径）

- `run/paused` seq 2187：`reason=budget_exhausted`、`trigger_dimension=max_context_tokens`、`budget_version=1`，
  `consumed.total_tokens=581535`（`run-a-pause.json`）。触发原因：操作者窗口 64000（自动 44800 / 硬护栏 54400）
  加上单用户轮会话没有可压缩的早期段（`no_compactable_early_turn`，seq 2185/2186）。
- 恢复：按 W-04 设计换更大窗口（产品默认）后用
  `POST /api/sessions/{id}/resume {run_id: ce8489dd-8134-4d73-9218-7baaf56aea2d, resume_basis: budget_increase, budget: {expected_version: 1}}`，
  同一 run 继续 1208 条事件后 `run/completed` seq 3395。恢复不是"隐藏失败重跑"：暂停、恢复请求与继续的事件都在同一会话日志里。

## 压缩腿（细节见操作者证据 `D:\w21-work\evidenceun-aun-a-compaction-legs.md`）

1. `?model=glm-5.3-flash`：seq 4048/4049 `transport_error`（`OpenAIAuthenticationError`），端点 200、`bracket_id=null`、`compacted_turn_count=0` —— 一次真实摘要调用失败。
2. 真实模型手动压缩：seq 4050/4051 与 4145/4146 `plan_section_mismatch`（两次尝试各被拒一遍），端点 200、零写入。
3. 自动路径：seq 4151 成功（`compacted_turn_count=3`）。

腿 2 是**缺陷回票候选**（本票不修产品代码）：`_validate_plan_section`（`compactor.py:1251`）拿摘要第 5 节与
**当前**计划（`derive_plan(session.events)`）逐字比对，而摘要只覆盖最后一条 `HumanMessage` 之前的消息
（`compactable_early_window`）。于是两种子情形都不可满足：计划里零 `in_progress` 而仍有未完成工作时（4050/4051），
闸门要求 `(none)` 但 prompt 要求罗列未完成工作；最后一条已完成轮次刚更新过计划时（4145/4146），闸门要求逐字出现 9 条进行中项，
而摘要段内看不到它们。自动路径不受影响（新轮起始时，计划的最近一次更新落在早期段内）。

## 关联观察（据实记录，不在本批修）

1. Windows 上多行 `python -c "…"` 命令返回 exit 0 但 stdout 为空（模型自己改成写脚本文件绕过）；这是命令执行环境的观察，未定性为产品缺陷。
2. 操作者审批回环曾短暂不可用，期间一次 bash 调用 fail-closed 被拒（随后重试成功）；属于操作者工具链，不是产品行为。
3. 浏览器读数里的 404 控制台错误经模型用 `check_favicon.py` 确认为 favicon 请求，与页面真值无关。
4. 安装件内的 `resources/node` 运行时（D5，约 +24 MB）为 TUI 冷启动提供 Node；Run A 未用到该路径（属 Run B）。

## 未覆盖（本文件不含）

- kill/reconcile 与 TUI 冷启动：票面分配给 **Run B**（同一安装件、新样例、逐票独立）。
- W-16 干净 VM、运行中更新安全暂停、暂停失败、迁移中断、磁盘满回退（后者只做逻辑验证，操作者明令不得真写满磁盘）。
- 双轴独立审查与 review coverage：属修复工作的收口，与本文件无关。
- 本文件不构成 #365 的通过结论；两次独立完整通过 + 审查完成后才谈 B-5 与关单。
