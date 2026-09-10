# 集成提示词：feat/backend → main（`reasoning_effort` P0 修复）

> 面向集成 AI。本分支为 `feat/backend` @ `D:\intelligence-agent-backend`。
> 本 Agent 已本地 commit，**未 push**（AGENTS.md §13.2 / §14.4）。

---

## 1. 一句话

修复一个 P0：**用户在会话里把「思考深度」选成「标准」或「深度」后，每一次 run 都在第一次模型调用就 400 失败、零输出**。根因是后端把产品词汇（`standard` / `deep`）当 provider 线格式字面量直接发出去。已翻译 + 加防呆，1 个 commit。

---

## 2. commit 与改动面

| commit | 内容 | 文件 |
| --- | --- | --- |
| `4219cbc` | `fix(model): reasoning_effort 语义档位翻译为 provider 线格式枚举` | 3 files +128/−22 |

```
src/agent_harness/model/provider.py   | 54 +++++++++++++++++++---
src/agent_harness/web/app.py          |  6 ++-
tests/model/test_reasoning_effort.py  | 90 ++++++++++++++++++++++++++++++------
docs/PHASE_STATUS.md                  | 新增 2026-09-11 条目
```

base：`5c7d85b`（merge：origin/main 架构深化 code-review 集成 + 前端 T9 集成记录）。

---

## 3. 根因（有生产日志实证）

**症状**（用户报告）：在会话里发消息「啥也没有」。

**证据链**：

1. 真实会话 `D:\intelligence-agent\.agent\workspace\sessions\f181c5ce-7c84-43f9-b249-efa606293268\events.jsonl`：
   - `seq=28 model/changed` → `{"to_provider":"qwen","to_model_id":"qwen3.8-27b"}`
   - `seq=30 user/message` → `"我是王浩宇"`
   - `seq=32 model/failed` → `{"message":"model call failed: BadRequestError"}`
   - `seq=33 run/failed` → 零输出，run 立刻收口
2. `D:\intelligence-agent\.agent\logs\agent.jsonl`，2026-09-08 ~ 09-11 反复出现：
   ```
   event_type=task_failed  outcome=error  error_type=BadRequestError
   'reasoning_effort' must be one of: 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'
   ... input: 'deep'
   ```
3. 代码定位：`web/app.py` 的 `REASONING_EFFORT_DESCRIPTIONS` 定义三档 `minimal | standard | deep`（**产品词汇**，前端显示 轻量 / 标准 / 深度），`model/provider.py:create_chat_model` 把它**原样**塞进 `ChatOpenAI(reasoning_effort=...)` → 进请求体。只有 `minimal` 恰好同名合法；`standard` / `deep` 是非法字面量。
4. 原设计假设被证伪：`docs/goal/GOAL_RUNTIME_REASONING_EFFORT.md` §5 风险表写「不支持的字段会被 Provider 静默忽略（OpenAI SDK `extra_body` 语义）」。实测端点**严格校验该字段取值枚举并硬 400**。
5. 影响面：`reasoning_effort` 经 amend 在每次续聊时透传（`web/src/lib/amend.ts`），所以一旦选了非法档位，**该会话后续每一条消息都失败**。

---

## 4. 修复内容

- `model/provider.py`
  - 新增 `WIRE_REASONING_EFFORTS`：provider 线格式合法枚举 `{none, minimal, low, medium, high, xhigh, max}`（单一事实源）。
  - 新增 `REASONING_EFFORT_WIRE` 翻译表：`minimal→minimal` / `standard→medium` / `deep→high`。`deep` 取 `high` 而非 `xhigh`/`max`：后两档只有部分端点支持，`high` 在「深度推理」里兼容面最广（想更激进只改一行，见 §7 未决项）。
  - `create_chat_model` 只注入翻译后的合法字面量；查不到、或翻译结果不在合法枚举内 → **不注入 + 一条 warn**（fail-open，不抛错）。
  - 纠正 docstring 里「不支持的 Provider 静默忽略」的错误断言。
- `web/app.py`：目录常量的注释标明 key 是 harness 语义档位，不要改成 `medium`/`high` 这类线格式词汇。
- `tests/model/test_reasoning_effort.py`：重写为 6 条契约（G1/G1b/G2/G3/G4/G4b/G5）。其中 G1 原断言 `reasoning_effort == "deep"` **正是 bug 的固化**——这就是先红后绿的那条红。

**为什么是后端责任（而不是前端）**：前端只照 `GET /api/reasoning-efforts` 渲染选项、把选中 id 原样回传，行为正确。违约方是后端把产品词汇当线格式词汇发出（不变量 #22：不维护第二套真相）。前端**零改动**。

---

## 5. 验证门（在 feat/backend 实测）

```
ruff check .                     → All checks passed!
pytest -q                        → 1506 passed, 9 skipped, 39 deselected, 0 failed  (122s)
pytest tests/model/test_reasoning_effort.py tests/test_assembly_reasoning_effort.py
                                 → 9 passed
```

基线 1503 passed（含 main 最新 `0cdbcc9` step_id 修复）→ 1506（+3）。

运行时实证（三个档位真实发出的字面量）：

```
minimal   -> 'minimal'   合法
standard  -> 'medium'    合法
deep      -> 'high'      合法
None      -> 不注入
'bogus'   -> 不注入 + warn
```

`create_chat_model` 的全部 4 处调用点均已核（`assembly.py` primary/fallback、`cli.py` health check、`capability/factories.py` memory extractor）——翻译只在 provider 单点（不变量 #7）。

---

## 6. 集成步骤（§14.6 先回后正）

```bash
# 在 main worktree
git fetch D:/intelligence-agent-backend feat/backend   # 两侧 repo 独立，需 fetch 才看得到对象
git merge-base --is-ancestor <本分支 tip> main && echo "main 已是祖先"
git merge-tree --write-tree main feat/backend          # 预期零冲突（改动仅 3 文件，与 main 无交集）
git merge --no-ff feat/backend                          # 需用户明确批准（§14.4）
ruff check src/ tests/
pytest -q                                               # 预期 1506 passed / 9 skipped / 0 failed
```

预期冲突：**无**。改动面只落在 `model/provider.py`（本分支独占）、`web/app.py` 一处注释、一个测试文件、`docs/PHASE_STATUS.md`（同位追加，注意最新在上）。

集成后建议顺手做的事（**不属于本 commit，需用户决定**）：
- `docs/INTEGRATION_PROMPT_STEP_ID_FIX.md` 目前仍是**未跟踪文件**（step_id 修复那次遗留的集成提示词，未入库）。AGENTS.md §13.1 要求 docs 入库——请确认是否随本次一并 `git add`。

---

## 7. 未决项 / 需要产品决策

| 项 | 说明 | 影响 |
| --- | --- | --- |
| `deep` 的映射落点 | 现落 `high`。`deep` 的 UI 描述是「最多推理开销，较慢但最深入」——若产品语义要求「最深入」，应改 `xhigh`（一行 + 1 条断言） | 只影响「深度」档的推理强度 |
| `standard` 的映射落点 | 现落 `medium`。语义是「平衡」，中位合理 | 低 |
| 是否按 provider 分档 | 未做。GOAL §7 明确禁止 per-provider 参数映射（如 Qwen 的 `enable_thinking`），本次只做 provider 无关的线格式适配 | 低 |

---

## 8. 与本次一并诊断、但属于**前端**的缺陷（不在本 commit）

用户同一次提问里的另外两个症状是前端缺陷，已写成独立交接单：`docs/HANDOFF_FRONTEND_RECOVER_FORK_SCROLL.md`。

- 「恢复会话」点了没反应 —— `web/src/lib/runState.ts:116` 的 `isRecoverableRun` 不认识 T8 新增的终态 `run/interrupted`，按钮永不消失。
- 「分叉」点了没反应 —— `web/src/components/Conversation.tsx:292` 把 `turn.step_id`（turn 键）当成用户消息 `seq` 传给 `from_seq`；错误又被 `web/src/App.tsx:318-323` 的 `catch {}` 静默吞掉。
- 流式期间滑轮被顶回去 —— `web/src/components/Conversation.tsx:121-132` 每个 delta 重发一次 `scrollIntoView({behavior:'smooth'})`，并重复实现了 `web/src/lib/followLatest.ts` 已有的跟随原语。

**前端不在本 worktree 修改**（AGENTS.md §13.1：前端任务在 `D:\intelligence-agent-frontend` / `feat/frontend`）。

---

## 9. 授权链与状态

- 授权链：用户 `[$diagnosing-bugs]` 报告「发了消息啥也没有」等三个症状；本 Agent 定位根因后按 §16.2（diagnose → 最小修复 → 回归 → code-review）执行。
- 状态：feat/backend 本地提交 `4219cbc`，**待集成 AI 合入 main**；本 Agent 未 push、未 merge（§14.4）。
