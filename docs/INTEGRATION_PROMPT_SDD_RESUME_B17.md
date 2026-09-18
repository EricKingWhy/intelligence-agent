# 开工提示词：SDD v2 续批（B-17 起）— intelligence-agent-backend

> **你是下一个 Primary Developer**。本文件是你的开工指令：读完它 + 两份权威文件即可开工，
> 不要凭本文件的记忆副本施工——**事实以 tracker 为准，流程以协议为准，本文件只是起点快照**。

---

## 0. 一句话目标

继续 `D:\intelligence-agent-backend` 的 SDD v2 长任务：**从 B-17 批次起按顺序做剩余 tickets**，
每个 ticket 走 `/implement` → 门禁全绿 → commit → 记 tracker（**跳过该票自带的自审**），
每 2–3 票批量做两轴 code-review（Standards + Correctness，各起一个独立只读子代理），
findings 最小修复 + 红证，落台账行，覆盖闸门 exit 0。

用户原话（原始指令，仍有效）：「**[$implement] [$ponytail] 继续下一票，记得按照规则做code-review**」。

---

## 1. 开工前必读（顺序固定，缺一不可）

1. `docs/SDD_WORKFLOW_PROTOCOL.md` —— **流程唯一权威**（v2 批量审查循环；v1 每票一审已作废）
2. `docs/SDD_TICKET_TRACKER.md` —— 在途 ticket / 批次 / fixed point / 审查结论（事实记录）
   - **必读 B-16 那条批次行**（搜「B-16 收批」）：它的 findings 处置、门禁数字、
     环境红归属是 B-17 的直接上下文
3. `AGENTS.md` —— 项目宪法（§7 不变量 / §8 Scope Lock / §9 Karpathy 准则 /
   §13 仓库模型 / §14 Git 工作流 / §16 SDD 入口）
4. `docs/PHASE_STATUS.md`（索引）+ `docs/phase_status/2026-09.md` 的 B-16 条目（明细）

**自愈条款**：任何时候上下文被压缩 / 不记得批次边界 / 不确定在循环哪一步
⇒ **立即重读 1、2 两份文件，禁止凭记忆继续施工**。

---

## 2. 当前状态快照（2026-09-19 交接时刻，以 git 实测为准）

- **仓库**：`D:\intelligence-agent-backend`（独立 clone，**不是** worktree）
- **干活分支**：**`main`**（施工 clone 直接提交模式，`AGENTS.md` §13.2(b)）——不是 `feat/backend`
- **HEAD**：`ca60905`（B-16 收批落点 + 台账白名单）
- **工作树**：clean；覆盖闸门 `scripts/check_review_coverage.sh` **exit 0**
- **上一批**：B-16（T05/#250 + T06/#251），fixed point 链完整，两轴审查 + findings 全数处置已闭环
- **测试基线**：2537 全绿（pytest `--ignore=tests/web` 2166 + `tests/web/` `-x` 单跑 371）

### 2.1 ⚠ 并行线（不要弄错分支，不要混批）

同一仓库内另有**两个 worktree、两条并行分支**（`git worktree list` 可验）：

| 位置 | 分支 | 谁在干 | 内容 |
| --- | --- | --- | --- |
| `D:\intelligence-agent-backend`（本 clone 主 worktree） | `main` | **你** | 后端 SDD 续批（#237–#247 refactor 线） |
| `C:\Users\王浩宇\WorkBuddy\Worktrees\intelligence-agent-backend\main-f049fadd` | `workbuddy/main-f049fadd` | **WorkBuddy agent** | P1-B2/#272 前端 memo 收敛（Perf #267 线） |
| `D:\intelligence-agent-fixbug` | `feat/FIX-test-BUG` | 第三条线 | BUG-013/014 |

**纪律**：
- WorkBuddy 分支的 merge-base 是 `e1266f8`，**落后于本 clone 的 main**——它看不到你的 commit，
  你也不碰它的 worktree（对它只做只读检查）；
- **真正的冲突风险只在三个共享流程文件**（`docs/review_ledger.tsv`、
  `docs/SDD_TICKET_TRACKER.md`、`docs/phase_status/2026-09.md`）：WorkBuddy 的 P1-B2
  审查行与你的 B-17 审查行最终要合进同一份台账。集成时按 `AGENTS.md` §14.9
  「一次只集成一条线」+ §14.7 冲突九问处理，**禁止机械 ours/theirs**；
- 集成前先看对侧：`git log --oneline main..workbuddy/main-f049fadd`。

### 2.2 ⚠ 环境红（先读，不要算到代码头上）

`tests/web/` **不带 `-x` 串跑时约 60–100 例红**：用例间状态泄漏 +
`.agent/workspace/.instance.lock` 被外部进程（曾见 pid=33740）占用。
**干净工作树同样复现（35 failed）**，非任何一批代码改动引入；`-x` 单跑该目录 371 全绿。
**跑全量门禁的正确姿势**：`pytest --ignore=tests/web` 一次 + `pytest tests/web/ -x` 一次。
建议后续单独开票修测试隔离，不要塞进 refactor ticket。

---

## 3. B-17 候选 tickets（从已解锁的开始）

**本批 fixed point = `ca60905`**（HEAD）。B-16 只收了两票，B-17 可收 2–3 票。

| 顺序建议 | Issue | 标题 | 依赖 |
| --- | --- | --- | --- |
| ① 先开 | **#252** | Session single durable write funnel（#241） | **无阻塞**（#251 已完成）；#251 的 golden 六条语义是它的保护网，**不得暗改** |
| ② | #253 | Recovery adjudication token contract（#242） | 无 |
| ③ | #255 | Read-only Catalog router seam（#243） | 无 |
| ④ | #256 | Bash timeout contract and red evidence（#244） | 无 |
| ⑤ | #259 | JSONL reader behavior golden（#245） | 无 |

依赖链提示：#254 依赖 #253；#257/#258 依赖 #256；#260 依赖 #259；#261/#262 依赖 #261。
**不要开 #267（Perf Umbrella）及其子票**——那是 WorkBuddy 正在干的线，混批会撞同一批文件。

每个 ticket 开工前按 `AGENTS.md` §3 做阅读协议：对应模块 spec + issue 全文 +
父票冻结决策 + 现状代码盘点。父票映射：#241/#252→`03_SESSION_EVENT_MODEL.md`、
#242/#253/#254→`07_STORAGE_PERSISTENCE_RECOVERY.md`、#243/#255→`08_PLUGIN_CAPABILITY_SYSTEM.md`、
#244/#256–258→`04_TOOL_RUNTIME.md`、#245/#259/#260→`06_CONTEXT_ARTIFACT_MEMORY.md`。

---

## 4. 每个 ticket 的执行闭环（v2 单票流程）

1. **`/implement`** 完成 ticket（红证纪律：变异 → 确认精确预期失败 → 还原 →
   `sha256sum` 验证逐字节相同）
2. **门禁全绿才 commit**：`.venv/Scripts/python.exe -m ruff check .` +
   全量 pytest（姿势见 §2.2）+ `git diff --check` 干净
3. commit（message 写工程事实），tracker 追加记录（明细入 `docs/phase_status/<年-月>.md`，
   `PHASE_STATUS.md` 只留索引一行）
4. **跳过该票自带的自审**
5. 每 2–3 票：**两轴批量审查**——`/code-review` 的方式 = 起两个**独立只读子代理**
   （Standards 轴 + Correctness 轴），fixed point = 上一批审查结束 commit，审未提交工作树
   （若审查者未产出文本，SendMessage 让它重发，**不要自己替它审**）
6. findings 处置：能定位的最小修复 + 测试；触及架构/契约才增量复查；**决定不修的必须如实登记**
7. 落台账：审查行 + 修复行入 `docs/review_ledger.tsv`（格式见该文件头注释），
   删除被覆盖的死白名单条目，`scripts/check_review_coverage.sh` **exit 0 才算收批**

---

## 5. 红线（原样继承，违者即事故）

- **凭证零泄漏**：`.env` 的值绝不打印/提交/复制；可列 key 名，不可列 key 值
- **探针陷阱**：真 `SystemCredentialStore` + 带 key 的 `create` 会往系统凭据管理器写假 key 且删不掉；
  探针供应商写进全局 `model-providers.json` 会让 3 条内置目录断言变红
- **Git**：`reset --hard` / `rebase` / `push --force*` / `branch -D` 默认禁止；
  `git pull` 禁用（fetch + 显式 merge）；冲突停下做 §14.7 九问；
  feature 分支 push / PR merge / cherry-pick / revert / 删分支需用户单独批准；
  把 main 合回自己分支、集成、集成后 `push origin main` 属常设授权
- **Scope Lock**：不顺手重构、不提前做未来 Phase、Scope 外问题只报告不修
- **Windows 工具纪律**：Python 用 `.venv/Scripts/python.exe`；中文大文件只用
  `Read`/`Grep`（**严禁 `tail`/`sed`/`cat`**——GBK 控制台乱码）；临时文件写 `%TEMP%`

---

## 6. 详细交接

完整交接文档（本阶段全部事实、B-16 findings 逐条、WorkBuddy 并行线核实、
建议 skills 清单）：`C:\Users\王浩宇\AppData\Local\Temp\HANDOFF_intelligence-agent-backend_B16_2026-09-18.md`
（临时目录，若已被清理则以本文档 §2–§5 + tracker 为准）。
