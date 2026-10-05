# #635 手动触发上下文压缩（/compact）——工作记录

## 五项启动检查表（AGENTS.md §3）

| 前置项 | 实际读取依据 | 状态 |
|---|---|---|
| Vision 相关原则 | SPEC_ROOT/00_PROJECT_VISION.md 未逐条细读——依赖 06/11 规格条款；Vision 核心原则（Lightweight、可观测、Session 事件源）已由 AGENTS.md §7 不变量与 06 §5 条款确认；本票为 Context 模块能力扩展，不改项目愿景 | READY（按本任务相关范围） |
| 当前任务规格 | `goal/.../docs/spec/06_CONTEXT_ARTIFACT_MEMORY.md` §5 Compaction（结构化摘要/只替换 runtime 投影/历史不删除/阈值可配/失败 fallback/不拆 tool pair）与 `11_STREAMING_API_WEB_UI.md` §3 CLI（compact 为 CLI 最少支持能力）、§5/§6（Web 右侧 context/compaction 展示、UI 只消费 SessionEvent） | READY |
| Reuse 相关判定 | `goal/.../docs/spec/13_OPEN_SOURCE_REUSE_MATRIX.md` §2：Compaction = PORT DESIGN（Pi）；§4 直接 REUSE 库。落盘判定：**本仓 `ContextCompactor` → REUSE**（唯一压缩语义实现，#22）；**Cline `/smol` 机制 → PORT DESIGN**（只借"手动走真实管线 + 并发三件套"设计，不抄代码；Cline Apache-2.0 即使抄也要 attribution，本票不抄代码）；Claude Code `/compact` → PORT DESIGN（交互：一等公民手动触发） | READY |
| Phase 依据 | `goal/.../docs/spec/14_IMPLEMENTATION_ROADMAP.md`——本任务为 issue #635 单票工程任务，非 Roadmap Phase 规划项；按 AGENTS.md §5 第 7 条（已批准 Issue）与 §16.2 执行入口推进 | READY |
| 本任务触发细则 | `docs/SDD_WORKFLOW_PROTOCOL.md` 全文（895 行，首行→EOF，2026-10-05 读完，V3.1-lite）；`docs/agents/implementation-discipline.md` 全文（§9.5–§9.6 展开说明）；操作 GitHub Issue 前将读 `docs/agents/issue-tracker.md`；ADR/CONTEXT 按需读 | READY |

## Grill（用户已裁决，记录结论）

- 用户 2026-10-05 拍板：设计按调研 §6 修正表改完即施工；`/compact [instructions]` 自定义指令**第一版不做**（简单优先）。
- 设计 §8 开放项逐项裁决：
  1. CLI 确认面 → 直接执行 + `--dry-run`（采纳推荐）
  2. 低水位 floor → `compacted_turn_count==0` 时零写入返回"水位过低无需压缩"，不写空 bracket（R6 落点；前端按钮置灰为 UX 提示）
  3. 按钮落点 → ContextUsagePanel（采纳推荐）
  4. `--model` / `?model=` → 第一版做（优先级：显式参数 > `settings.summary_model` > 主模型；非法 → Web 422 / CLI exit 1，零副作用前校验）
  5. 端点路径 → `POST /api/sessions/{session_id}/context/compact`（采纳推荐）
- R2 `is_busy` vs `get_active` → 采用 `is_busy` + 实质论证（写会话事件日志 + finalizer 窗口 seq 竞态 #560-B），**不许写"对标 #616"**（#616 无 409 检查，调研 §3.2 实证）

## Spec（验收标准，依据设计 §11 + 调研 §6 修正表）

AC1. 40% 水位 CLI 成功产出合法 bracket（三事件字段齐：schema=eight_section、source_seq_start/end、bracket_id），重投影确认通过（与自动路径同一确认逻辑）。
AC2. 校验闸门任一失败 → `context/compaction_failed`，会话可继续（与自动路径一致）。
AC3. 在途 run：CLI `SystemExit(1)` + 明确错误；Web 409；**零事件写入**。
AC4. 压缩后 `derive_messages` 跳 shadow 旧事件、新摘要在场；resume/fork/replay 回归通过。
AC5. `--model` 缺省 = 主模型逐字节等价；指定模型透传到 compactor（`summary_model`）；非法模型名 → Web 422 / CLI exit 1（校验在零副作用前）。
AC6. Web：按钮在 ContextUsagePanel 可见可用；压缩前后 token 对比展示；失败就地显示；重复调用追加新 bracket（非严格幂等，文档化）。
AC7. 低水位（`compacted_turn_count==0`）零写入返回"水位过低无需压缩"，不写空 bracket。
AC8. 并发防重：per-session in-flight guard，Web 连点/CLI 并发第二次被拒绝（响亮拒绝），guard 在 finally 释放。
AC9. gate0 6/6（含 `check_review_coverage.py` 台账行，tip 为 HEAD 祖先）；Evidence 落盘 `docs/gate/<sha>.json`。
AC10. 不变量：#3 append-only（历史事件一字不改）/#22 唯一实现 ContextCompactor/#7 不新增 Tool 路径/#16 预算账本不动。

## Tickets

| 票 | 内容 | 验收 |
|---|---|---|
| T1 | R3 实测：Web 同步 POST 经网关的超时行为（60s sleep 桩端点打一次） | 得出网关超时结论，决定前端是否加超时提示 |
| T2 | `ContextBuilder.compact_now(session)`：抽取 builder.py:489-560 整段（compact+失败记录+bracket+重投影+硬护栏复核）；`build()` 阈值命中时调它 | 自动路径逐字节等价（现有压缩测试全绿） |
| T3 | `SessionService.compact_session_context()`：校验→is_busy 拒绝→短持锁判忙+快照→释放锁→LLM→重拿锁复验→落盘；in-flight guard；model 优先级；零写入低水位返回 | AC1/AC2/AC3/AC5/AC7/AC8 + 单元测试 |
| T4 | CLI `agent-harness compact --session <id> [--model M] [--dry-run]` 瘦触发器（对标 #616 形态） | AC1/AC5，exit 码矩阵 |
| T5 | Web `POST /api/sessions/{session_id}/context/compact`（来源闸、422→404→409、?model=）+ 前端按钮（ContextUsagePanel：loading/结果对比/失败就地显示/低水位置灰） | AC3/AC5/AC6 + vitest |
| T6 | 文档：设计 §6 修正表落盘（删未核实引用）；PR | AC9 |

## 实现纪律

- 绝不碰主树 `~/workspace/intelligence-agent` 与 `~/workspace/intelligence-agent-wt-impC`；只在 `~/workspace/intelligence-agent-wt-635` 工作。
- 只 add 本任务文件；小步 commit，message 写工程事实。
- 不 merge、不关 issue、不 push 别的分支；PR 开完即停。

## T1（R3 实测）结论 2026-10-05

- 实测：`/tmp/t1_sleep_probe.py` 起 uvicorn + `POST /api/debug/sleep-60` 桩端点，
  `RESULT status=200 body={'ok': True, 'slept': 60} elapsed=60.0s`——uvicorn 层无 <60s 超时。
- 网关层一手证据（仓库已有）：`web/app.py:1281` 注释实测 `POST /api/sessions` 响应经
  CloudStudio Gateway + EdgeOne 在 44.158s 后完整送达（只是攒到流结束才下发）。
- 结论：同步 POST 足够，**不引入 SSE 新机制**；前端保留 spinner，并在 `compactSession`
  上加显式超时（AbortController，约 180s）+ 超时/失败的明确就地错误提示。

## Task A（T2+T3）完成 2026-10-05（codebuddy 施工）

- commit `288279f9` T2：`ContextBuilder.compact_now(session, *, runtime_context=None, write_guard=None)`；
  `build()` 委派，自动路径逐字节等价（tests/context 全绿）。
- commit `607c9c32` T3：`SessionService.compact_session_context()` + `SessionContextCompaction` DTO +
  `COMPACT_ENTRY_API`；16 新测试红→绿（首轮 9 红：`log_event` 事件名未注册，修正后全绿）；
  focused 323 passed；`tests/session/` 674 passed + 1 既有失败（root 下 chmod 只读无效），
  `tests/agent/` 837 passed + 1 既有失败（代理环境 httpx InvalidURL），两处均经 `git stash`
  在原始树复现，属既有环境问题。
- 偏差（codebuddy 按 AGENTS.md §9.1.1 取最小可行解，已在 docstring/测试注明）：
  1. SessionService 构造契约被 ADR-0040 + 18-collaborator 契约测试冻结，加 collaborator 会牵动 ADR，
     故用私有可覆盖 `_compact_context_builder()` 直接装配轻量 builder（不走 build_runtime）。
     代价：手动路径 `reserved_tokens` 不含 system_prompt/runtime 快照（target 闸门保留量略小），压缩语义不变。
  2. `compact_now` 新增 `write_guard` 只包 bracket 写入窗口（T3 传"重拿 session_lock+复验"守卫；build 传 None）。
  3. `compact_now` 低水位返回 None；`build()` 视作"未压缩"走共用收尾。
  4. 失败上报测试用"剧本耗尽的 ScriptedModel"驱动两次真实摘要失败（落 2 条 compaction_failed，无 bracket）。
  5. dry_run 水位预览用 `_has_compactable_early_turn`（compactor early 窗口同形近似），不调 LLM。
- 残余：偏差 1 的 reserved_tokens 差异待用户/审查裁决（语义不变，仅闸门保留量略小）。

## Matt 双轴审查（2026-10-05，codebuddy，两轴并行，只读）

- Standards 轴：**PASS-WITH-FINDINGS**（2×P2、5×P3，无 P0/P1）。
  P2-1：post-bracket fail-closed 被吞成"未改动"（与 Spec P2-2 同根）；
  P2-2：runtime provider 空白时被调两次（破"只调一次"契约）；
  P3：dry-run 预览缺 `_is_compaction_summary` 守卫；CLI 字符串匹配区分拒绝理由；
  硬护栏公式两处重复；`Any` 类型弱；`_format_grouped` 下划线分隔符。
  另诚实记录：审查窗口内编排侧落了一笔 docstring 提交（后已 commit 9cd27b79，纯文档，语义等价）。
- Correctness/Spec 轴：**NEEDS-FIX**（1×P1、2×P2、P3×6）。
  P1：write_guard 事件数复验把"自己写的失败记录"误判为并发改动→成功重试被 409（且非零写入）；
  P2：post-bracket fail-closed 被吞（同 Standards P2-1）；缺省摘要模型未跟随会话级模型覆盖
  （AC5"缺省=主模型"不成立）；
  P3：dry-run 判据漂移；reserved_tokens 后果已登记（可接受但不完全等价）；strip 不一致；
  `_get_wiring` docstring 与"轻量"矛盾；T6 文档清理待落盘。
- 确认无误点：AC3 拒绝顺序零写入正确；Web 409/404/422 映射正确；ClassVar 防重正确；
  锁协议结构正确；#3/#7/#16/#22 不变量守住；T2 抽取等价；无"对标 #616"/"cache stays warm"/"93%"。
- 处置：F1–F11 修复任务已派 codebuddy（TDD 红证→修复→转绿）；F12（reserved_tokens）保持已登记偏差。

## 审查修复（F1–F11）完成 2026-10-05（codebuddy，TDD 红证→修复→转绿）

- commit `aac5130c`（compactor/builder 侧）/ `8d605cf4`（service 侧）/
  `1ecc425a`（CLI/Web 侧）/ `cceb0f72`（测试）。
- F1：write_guard 工厂化 `own_writes`；F2：`CompactionPostWriteError` 响亮失败
  （Web 500/CLI exit 1）；F3：缺省模型跟随 `current_model_selection`；
  F4：`_RUNTIME_CONTEXT_UNSET` 哨兵；F5：dry-run 补 `_is_compaction_summary`；
  F6：`CompactionInProgress`/`CompactionConcurrentWrite` 子类；F7：公式注释；
  F8：类型收紧；F9：千位逗号；F10：strip 统一；F11：docstring 如实化。
- 新增 9 条红证测试；focused（service/cli/web/context）276 passed；ruff 全绿。
- 附加回归：`tests/session/ + tests/context/ + test_cli_compact` 927 passed
  （1 既有环境失败 test_progress_file root-chmod）；`tests/web/` 698 passed
  （1 既有失败 test_web_batch51_spec_contract，已在基线树 git stash 复现确认无关）。
- F12（reserved_tokens 差异）保持已登记偏差，未扩大。
- 修后重审已派（两轴各 1 轮，范围 aac5130c..cceb0f72）。
