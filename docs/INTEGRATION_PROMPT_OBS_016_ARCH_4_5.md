# OBS-016 + ARCH-4/4b/5 集成提示词（集成 AI 执行手册）

> **收件人**：集成 AI（Git Integrator 角色，AGENTS.md §14）
> **任务**：把 `feat/backend` 与 `feat/frontend` 的本批成果合入 `main` 并完成验证
> **红线**：永不 force-push / rebase / reset --hard；凭据零泄漏（`.env` 值不进任何输出/提交）；
> 每次合并动作前确认 worktree 与分支（§14.2）；冲突后**立即停止**自动解决并按 §14.7 逐文件分析；
> 一次只合一个分支（§14.9：先 backend → main 验证完，再重新分析 frontend）。
> **本文件状态**：逐票追加——已完成票在对应 § 里给出 commit 与门禁；在途票明确标注「在途」。

---

## 0. 机器现状（以 `git -C <path> log -1` 为准，合并前复核）

| 用途 | 目录 | 分支 | 本批 tip |
| --- | --- | --- | --- |
| 集成主战场 | `D:\intelligence-agent` | `main` | 以实际为准 |
| 后端施工区 | `D:\intelligence-agent-backend` | `feat/backend` | `aa29562`（+ 本文件所在 docs commit） |
| 前端施工区 | `D:\intelligence-agent-frontend` | `feat/frontend` | `43b9ffd` |

- **拓扑**：`D:\intelligence-agent-frontend` 是**独立 clone**（自带 `.git`），不是 worktree；`D:\intelligence-agent-backend` 才是 worktree。
- **origin/main 基线**：两 clone 都看到 `63db650`。
- **后端领先** `origin/main` **8 commits**（本批之前 OBS-008/010/011/012/013/015 + 架构深化三连）。
- **前端相对 `origin/main`**：基线 `274afcf` 是 `63db650` 的**严格祖先**；本批在其上加 2 commits（`1fac807`、`43b9ffd`）。两边**改动文件集不相交**（main 那 18 个 commit 只动 backend/`docs/PHASE_STATUS.md`，本批只动 `web/src/lib/` 与两处 docs）→ 合并预期零冲突，但**仍需按 §14.9 顺序做，且后端合入后重新 `fetch`/`diff`/`merge-base` 再判前端**。

---

## 1. OBS-016（#141）：read 超长单行提示去掉 POSIX 专有命令 —— ✅ 已完成（跨端）

### 1.1 问题

`ReadTool` 在「单行本身超 50KiB 字节帽」时给出的续读指引是：

```text
[Line {n} truncated at 51200 bytes. Use bash with 'sed -n '{n}p' <file> | head -c 51200' plus 'tail -c +N' to read further segments.]
```

本机真实解释器是 cmd.exe（Windows；见 OBS-012），`sed` / `head -c` / `tail -c` **都不存在** ——
模型照提示做会拿到 `'sed' is not recognized`，整次工具调用作废。与 OBS-012 同一缺陷类
（模型可见文案描述了一个不存在的解释器环境）。规范面：`05_SANDBOX_CODING_TOOLS.md` 对提示
文案无要求 → 代码诚实性问题，**非规格冲突**。

### 1.2 跨端契约（本票的关键约束）

标记前缀 `[Line {n} truncated at {bytes} bytes` 与结尾 `]` 是前端解析契约：

```ts
// web/src/lib/toolShapes.ts:30
const LINE_TRUNCATED_RE = /\[Line (\d+) truncated at (\d+) bytes[^\]]*\]\s*$/;
```

`[^\]]*` 吞掉尾部 → **改措辞不破坏解析**，需要同步的只是 fixture 与文档，不是解析逻辑。
因此本票**未改** `toolShapes.ts` 的任何逻辑。

### 1.3 后端改动（`D:\intelligence-agent-backend` @ `feat/backend`）

**commit `aa29562`**

| 文件 | 改动 |
| --- | --- |
| `src/agent_harness/tools/read.py` | `giant_line` 分支正文改为**点名真实存在的工具标识符**（`bash` / `grep`），不点名任何命令：解释器与其陷阱由各工具自己的描述声明（OBS-012）。同分支的陈旧注释（写「标记改用 bash 建议…（sed/head 取片段）」）一并订正。 |
| `tests/tools/test_coding_tools.py` | 新增三条断言：① 前端解析契约（正则取 `(line, bytes) == (1, 51200)` + 结尾 `]`）；② 词边界 `\b(sed|head|tail)\b` 不得出现（避免误伤 `used`/`closed`/`based`）；③ 必须点名至少一个真实工具标识符（`bash|grep`）。 |

新正文（`{start}` 为行号，`_READ_MAX_BYTES` = 51200）：

```text
[Line {start} truncated at 51200 bytes. This single line alone exceeds the read limit, so it cannot be returned in full. Use the bash tool to read a further byte range, or the grep tool to locate the part you need.]
```

**⚠ code-review 抓到并已修的坑（初版缺陷）**：初版写成 `"Use the shell tool ..."` —— 仓库里
**没有名为 `shell` 的工具**（注册名是 `bash`，见 `tools/bash.py:40`；`grep` 见 `tools/grep.py:45`）。
这等于把模型指向不存在的东西，**正是本票要修的缺陷类**。两轴 review 各自独立发现，已改为
`"the bash tool"` / `"the grep tool"`，并加断言锁死（第 ③ 条）。

- 有意**不做**：不给 read 加 byte-offset 参数（另一张票）；不改
  `[Showing lines X-Y of N. Use offset=Z to continue.]`（本就解释器无关）。
- 门禁：`ruff check src/ tests/` ✓；全量 `pytest` **1563 passed / 10 skipped / 39 deselected / 0 failed**。
- 变异验证（均转红后还原）：① 文案回退成 sed 示例 → `\bsed\b` 断言红；② 前缀改成
  `[Line {n} truncated {bytes}` → 解析契约断言红；③ 正文不含 `bash`/`grep` → 真实工具断言红。
  （第 ②/③ 项最初在「未还原前一次变异」的状态下跑，红色归因不成立，已重跑隔离验证。）

### 1.4 前端改动（`D:\intelligence-agent-frontend` @ `feat/frontend`）

**commit `1fac807`（功能+文档）/ `43b9ffd`（tracker）**

| 文件 | 改动 |
| --- | --- |
| `web/src/lib/toolShapes.test.ts` | 新增「新文案（OBS-016）」用例；原用例改标「旧文案（历史会话已落盘）」并**保留**——历史 JSONL 事件里仍是旧文案，两种都必须能解，注释明写「别删旧用例」。 |
| `docs/HANDOFF_FRONTEND_SYNC.md` §1.3 | 订正为「形状契约 + 明示措辞可变 + 历史文案兼容要求」。 |
| `docs/SDD_TICKET_TRACKER.md` | 登记本批门禁、变异验证、跨端配对与「未做 merge」的理由。 |

- **解析逻辑零改动**（`LINE_TRUNCATED_RE` 本就吞尾部）。
- 门禁：`tsc -b` ✓ / `vitest` **502 passed（28 文件）** / `oxlint` **35 warnings 0 errors**（与基线持平）/
  `playwright --workers=2` **118 passed** / `vite build` ✓。
- 变异验证：把 `LINE_TRUNCATED_RE` 改成仅匹配旧文案（追加 `\. Use bash`）→「新文案」用例红、
  「旧文案」用例仍绿（已还原）→ 证明新用例非空转、且旧用例仍锁住向后兼容。

### 1.5 验收标准核对

| # | 标准 | 状态 |
| --- | --- | --- |
| 1 | 后端提示不再出现 `sed` / `head -c` / `tail -c` | ✅ 词边界断言锁定 |
| 2 | 前缀/结尾形状不变；`parseReadShape` 对旧、新两种文案都能解出 `{line, bytes}` | ✅ 新增新文案用例 + 保留旧文案用例 |
| 3 | `docs/HANDOFF_FRONTEND_SYNC.md:46` 同步订正 | ✅ |
| 4 | 后端 ruff + 全量 pytest 绿；前端五项门禁绿 | ✅ |

### 1.6 合并与验证要点

- 后端：`feat/backend` → `main`，预期无冲突（本票只碰 `tools/read.py` + 一个测试文件）。
- 前端：`feat/frontend` → `main`，预期无冲突（只碰 `web/src/lib/toolShapes.test.ts` + 两处 docs）；
  **main 侧 `toolShapes.ts` 与 `toolShapes.test.ts` 自 `274afcf` 起零改动**（已核）。
- 合并后建议的验证（`main` 上实跑）：
  - 后端：`uv run ruff check src/ tests/ && uv run python -X utf8 -m pytest -q`
  - 前端：`cd web && npx tsc -b && npx vitest run && npx oxlint && npx playwright test --workers=2 && npx vite build`
  - 契约抽查（真机）：让模型 `read` 一个单行 >50KiB 的文件，断言返回的标记能解析出
    `{line, bytes}` 且正文不含 POSIX 专有命令；再回放一条**历史**会话（旧文案）确认前端仍解析。

### 1.7 关单

GitHub issue **#141 已关闭**（§14.12，comment 内含两个 clone 的分支与 commit、门禁与变异证据，
并注明「尚未合入 main，集成由集成 AI 执行」）。

---

## 2. ARCH-4b（#142）：`/api/sessions` 补 `trace_url` —— ⏳ 在途

后端列表契约缺 `trace_url`（前端 `types.ts::SessionSummary` 已按契约 `2d7f87a` 声明为必填
`string | null`）。计划：让 `read_session_summary` 的终结事件取值同时产出 `trace_id` 与
`trace_url`，`list_sessions` + `/api/sessions` 透传；核实 Langfuse 开/关两态。

**完成时本节补齐**：commit、门禁数字、变异验证、前端类型核实结论。

---

## 3. ARCH-4（#143）：`list_sessions` 去 dict 中转 —— ⏳ 在途

`SessionService.list_sessions` 现返回 `list[dict[str, Any]]`，应由领域 dataclass
（`SessionSummaryStats`）直接承载，去掉 web 层的字符串键中转。

**完成时本节补齐**：commit、门禁数字、行为保持证据（既有 payload 测试零改动通过）。

---

## 4. ARCH-5（#144）：领域异常→HTTP 映射单源化 —— ⏳ 在途

`web/app.py` 有 9 处重复的「领域异常 → HTTP 状态」阶梯（行号见 issue #144）。**动手前必须逐处
审计**：`approve` 端点的 404 有四个不可区分来源（OBS-015 记录），且各 handler 可能只覆盖子集
——不能盲目合并同一张表。

**完成时本节补齐**：commit、逐 handler 审计表、门禁数字。

---

## 5. 集成顺序（§14.9：一次一个分支）

```text
1. feat/backend → main        （先回后正：先 fetch，再在 feat/backend 侧确认与 origin/main 关系）
2. 在 main 跑后端全量门禁 + 真机契约抽查
3. 重新 fetch / diff / merge-base，重新分析 feat/frontend 的冲突面（后端合入后旧判断全部作废）
4. feat/frontend → main
5. 在 main 跑前端五项门禁
6. 完整起项目联调（前后端同时跑）
7. 最后 git push origin main
```

**每个 merge 动作前单独确认**（§14.11：不得因为上一阶段获批就默认本阶段也获批）。
