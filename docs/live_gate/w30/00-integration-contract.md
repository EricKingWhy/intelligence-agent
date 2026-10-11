# W-30（#384）集成契约：增补判据 × W-21 harness

> 本文件是 #384 在本仓的「集成证据」部分：定义 4 条增补判据如何接入既有
> Live Gate / 挑战夹具框架（W-19 / W-20 / W-21 同套）、消费哪些 W-21 harness
> 产物、产出什么证据。判据实现（真实模型跑出来的 PASS / FAIL）在
> `intelligence-agent-backend` 仓库，不在本票。

## 1. 判定器合入顺序（R1）

```
最终发布 Gate 执行
  ├── 先合入：W-30 判定器（4 条增补判据，criteria.json）
  │     消费：W-21 harness 产物（§2，不要求 W-21 先 passed）
  ├── 再执行：发布 Gate（含 W-21 既有判据，原样保留）
```

- 判定器在**最终发布 Gate 执行前**合入本仓（本目录 + spec §9.2），不是"先发版再补判据"。
- 判定器**消费 W-21 harness**（§2 的输入清单），**不要求 W-21 先 passed**：
  #365 已于 2026-10-09 按用户口径（自动门禁 + 廉价冒烟）收口关闭，**未声明**
  「发布 Gate passed」（两次独立真实模型通过未取得，如实见 #365 关单 comment），
  等一个不存在的 passed 会把 W-30 永久阻塞。

## 2. 消费的 W-21 harness 输入（全部接地到本仓真实路径）

| 输入 | 本仓路径 | 被哪条判据消费 |
|---|---|---|
| `compaction/start`、`compaction/end` 事件定义 | `src/agent_harness/session/event.py` | C1 / C2 / C3 / C4 |
| `tool/call`、`tool/result` 事件对 | `src/agent_harness/session/event.py` | C2 / C3 |
| `progress.md` 快照（`progress_md_sha256` 独立单列） | `src/agent_harness/session/evidence.py` | C1 |
| UI 进度清单投影 | `web/src/components/PlanList.tsx` | C1 / C4 |
| W-20 挑战夹具判定器（R-042 / R-043 验收形态） | `tools/challenge-fixture/judge.py` | C2 / C4 |
| LiveEvidence 证据契约（`attempts` / `assertions` 形态） | `evaluation/live_gate/schema.py` | C3 |
| W-21 harness 证据组织形态 | `docs/live_gate/w21/30-gates.md` | C1 |

判定（`tests/live_gate/test_w30_criteria.py::test_harness_inputs_exist_in_repo`）：
上面每一行路径在本仓真实存在；任一缺席即集成契约破裂，测试红。

## 3. 输出证据格式

每条判据一次判定产出一份证据，字段见 `criteria.json` 的 `evidence_fields`，
**必含 `sha` + `tree`**（绑定 commit sha 与 tree hash，票面验收）与
`verdict ∈ {PASS, FAIL, BLOCKED, SKIPPED}`（与 `evaluation/live_gate/schema.py`
的 `Verdict` 四态一致）。

证据规则（`evidence_rule`，四条判据相同）：
- MUST 来自真实模型运行的 SessionEvent + 真实操作记录；
- mock / 替身 / Fake Provider / UI 绿勾的输出**不得**计入 PASS（上限锁 FAIL），
  与 `evaluation/live_gate/schema.py::decide_verdict` 的注入判据同形；
- 失败轮次的原始证据保留（attempt JSONL + evidence.json），修后须重新得到
  两次独立完整通过（#344 发布 Gate 纪律，票面工作指令第 1 条）。

## 4. #365 依赖边的两种含义（R2）

- **真阻塞边**：W-21 harness 产物存在（§2 输入清单）。本仓已存在 ⇒ 边已满足，
  不阻塞。
- **非阻塞边**：W-21 的 passed 状态。#384 不等待它（见 §1：#365 未声明 passed）。

## 5. 方案依据（协议 §1.3）

### 来源（≥2 独立来源，2026-10-11 已读）

1. OpenHands `StuckDetector`（`openhands/controller/stuck.py` + SDK PR #4332）：
   4x 相同 action/observation 或 3x 相同 action→error ⇒ 注入一次 corrective nudge，
   之后仍重复则终止/暂停。
2. LangGraph `RetryPolicy`：相同输入重试只给分类为 transient 的错误（5xx / 429），
   有界（max_attempts=3）；逻辑错误零系统重试，须带修正参数返回。
3. GitHub merge queue（docs.github.com，verified live）：
   PR 自己的 required checks 先过才能进队列；队列组装临时 merge-group commit，
   required checks 在该产物上跑（`merge_group` 事件）——gate 消费 harness 组装的
   产物，而非要求被测对象先 passed。
4. LaunchDarkly：flag + 发布策略（targeting rules / release policy）在代码上线前配好。
5. SWE-bench：FAIL_TO_PASS 测试在 agent 动手前冻结（test_patch 在 base_commit 时不存在）。

### 机制摘要 → 本票落点

| 口径 | 来源机制 | 落点 |
|---|---|---|
| R3 失败重试限定 | OpenHands：相同条件重复→禁，一次纠正机会；LangGraph：transient 才可相同输入重试，逻辑错误须带新证据 | C3 的 pass/fail 规则：相同条件/无新证据的重走判 FAIL；合理修正输入后的重试不违规 |
| R1 判定器顺序 | merge queue：gate 消费 harness 产物；LaunchDarkly：criteria 先于 feature 配好；SWE-bench：判据冻结在先 | §1 合入顺序：判定器在最终发布 Gate 执行前合入，消费 W-21 harness 而非要求先 passed |
| R4 清单一致限定 | 无成熟产品有「后继验证接班清单」机制（Q1 调研结论：Claude Code / Codex / Cursor / Cline / OpenHands 均无） | C1 为本项目自创判据，按用户限定口径执行（压缩本身无 plan 更新时才要求逐字一致），不擅自扩大 |
| R2 依赖边 | 本仓 roadmap §3 的依赖边钉法（只钉谁依赖谁） | §4 两种边含义 |

### 契合点（AGENTS.md §7 不变量）

- #8 Recovery before Retry、#11 Tool Retry 只有一个责任域、#14 UNKNOWN 不盲重跑：
  R3/C3 只定义「什么算重走」，不另开重试路径，不碰 ToolExecutor。
- #3 Session append-only、#5 Event ≠ Log：C1/C3 的判定只读事件流与快照，不改写历史。

### 判定

- R3/C3：`PORT DESIGN`（OpenHands 阈值思想 + LangGraph 分类思想，判据形状按本票重写）。
- R1：`PORT DESIGN`（merge queue / LaunchDarkly / SWE-bench 的"判据先行 + 消费 harness"思想）。
- R4/C1：`BUILD`（无成熟产品机制，本项目自创；用户已拍板限定口径）。
- R2：`BUILD`（本仓特有，无上游可复用）。

### License

未复制任何第三方代码，只借鉴设计思想 ⇒ 无 License 义务；来源 URL 见上，
一手来源报告在 `~/workspace/research_notes/384-mature-product-gate-practices-20261011-1037/report.md`。

## 6. Backend 判定器的消费契约

`intelligence-agent-backend` 的判定器实现消费本目录的 `criteria.json`
（`$id` 见 `criteria.schema.json`）：
- 按 `check_id` 逐条实现判定逻辑；
- 输入 = §2 的 harness 产物（由 backend 从运行环境采集）；
- 输出 = §3 的证据格式（`sha` + `tree` 必含）；
- `user_rulings` 的 R1–R4 是冻结口径，判定逻辑不得放宽。

## 7. 不做（票面）

- 不改 W-21 已有判据（spec §9.1 原样保留，只增补 §9.2）；
- 不改压缩算法；
- 不扩大 Gate 范围到 Memory V2（#304 独立）。
