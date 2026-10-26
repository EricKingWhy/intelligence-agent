# ADR-0058 — Memory 转向第 4 步：唯一提交 seam、操作状态持久格式与可对账恢复

- **Status**: Accepted（工程侧正式设计，2026-10-26；闭合 ADR-0056 §3.2–§3.6 五处「未闭合」的格式决策去向）
- **Date**: 2026-10-26
- **Deciders**: 本 Agent（格式选择与 seam 设计，受 ADR-0056 已裁边界约束）+ 用户（批次授权与长期指令）
- **Related**: Issue #1494（本 ADR 对应票，AC1–AC5）；ADR-0056（owner 点名与五项阻塞切换）；
  ADR-0042 / 0046-memory / 0049（退役面归 #1496，本 ADR 不处置）；ADR-0014（归 #1507）
- **非目标**: 不改读侧（#1495）；不退役 records / outbox / FTS（#1496）；不动 #1491/#1492/#1493 产物；
  不新增身份路由；不把任何操作状态写进 Markdown 正文；不引入外部服务或新依赖

---

## 1. Context

ADR-0056 点名了八类 owner 并把五处（job/lease/幂等、approval/pin、settings、tombstone、receipt）
标为「阻塞切换」：它们当前全部落在 `memory-v2.db`，而「删除 SQLite 表」不等价于「改成文件真相」。
第 4 步要闭合的正是这五处的**持久格式 + 恢复入口 + 验收判据**，并把内容写者收敛到唯一 seam。

现状缺陷（#1494 复核定级：D6-A CONFIRMED；F5-1 P2、F6-1 PARTIAL P2）：

1. **写序分叉**：`capability.py` 的 create/update/invalidate/edit 先写 Markdown 再写 SQLite；
   而 formation apply（`executor._apply`）、blocked promote、`delete`/`bulk_delete` 先写 SQLite
   再补 Markdown。同一份内容有两个方向相反的「先做什么」，跨介质失败窗口各有各的泄漏形状。
2. **完成后失败只警告**：`executor._apply` 在 `commit_with_outcome` 的同一个 SQLite 事务里写完
   records + outbox + job 终态，**事务提交之后**才 `mirror_committed` 补 Markdown，失败仅 warning
   ⇒ 「DB completed、文件没写」的 job 既不在恢复集（终态被排除）、也没有任何对账入口。
3. **恢复集排除终态**：`list_recoverable` 只扫非终态；上述形态永远不再被看见。
4. **新会话能收敛、派生层不收敛**：会话首读磁盘快照，所以用户下一轮看到的是文件真相；
   但 SQLite 检索（records/FTS）、治理列表与墓碑抑制仍停在旧状态。
5. **单文件原子 ≠ 跨文件事务**：`_atomic_write_text` 是 tempfile + `os.replace`（无 fsync），
   两文件操作（如 `remove_by_digest`、批量删除的镜像）在中间崩溃留下部分发布，无对账手段。

## 2. 决定（D1–D12）

### D1 状态根与文件布局

操作状态根：`<workspace_dir>/memory-state/`（与 `memories/`、`memory-v2.db` 并列的一等目录）。

```text
memory-state/
  settings.json                     # 本机设置四开关（D6）
  tombstones/<memory_id>.json       # 删除抑制（D7）
  staged/<staged_id>.json           # 待审写入（D5）
  blocked/<blocked_id>.json         # 被拦下的后台写入（D5）
  jobs/<job_id>.json                # 形成作业（D4）
  receipts/<operation_id>.json      # 提交回执（D2/D3）
  legacy-import.json                # 旧 SQLite 操作状态的一次性导入回执（D9）
```

锁：每个被改文件旁的 `<file>.lock`（advisory，进程崩溃自动释放）；作业侧另加
`jobs/claim.lock` 串行化「选候选 + 写 lease」（同一把全局锁已保证「同用户只有一个在途
job」，不需要按用户分锁）；回执侧另加 `receipts/locks/<operation_id>.lock` 串行化同 op
的重复执行与对账。
所有文件写入一律 `tmp + fsync + os.replace`（单文件对并发读者原子；POSIX 上补目录 fsync）。

**不冒称**：跨文件不原子、断电不保证；恢复一律走 receipt 对账（D3），不假设文件系统给了事务。

### D2 `MemoryCommit`：唯一内容提交 seam

`src/agent_harness/memory/v2/commit.py` 的 `MemoryCommit` 是当前内容与 revision 的**唯一**可变
事实入口（ADR-0056 §3.1）。它拥有：版本/来源扫描、pin 与 revision 校验、单文件发布、回执。

```python
class MemoryCommit:
    async def add(self, target, content, *, operation_id=None, origin=None, payload=None) -> dict
    async def replace(self, target, old_text, new_content, matched_entry=None, *, fallback_add=False, operation_id=None, origin=None, payload=None) -> dict
    async def remove(self, target, old_text, matched_entry=None, *, missing_ok=False, operation_id=None, origin=None, payload=None) -> dict
    async def apply_batch(self, target, operations, *, trusted_pins=False, allow_empty=False, operation_id=None, origin=None, payload=None) -> dict
    async def apply_batch_many(self, plans, *, trusted_pins=False, allow_empty=False, operation_id: str, origin=None, payload=None) -> dict
    async def remove_by_digest(self, digests, *, digests_by_target=None, operation_id=None, origin=None, payload=None) -> list[str]
    def content_error(self, content) -> str | None            # 系统镜像的碰盘前全量预检
    async def resolve_entry(self, target, old_text, verb) -> dict
    async def resolve_batch_entries(self, target, operations) -> dict
    async def reconcile(self) -> ReconcileReport
```

要点：

- **响应形状不变**：沿用 `markdown_store` 既有的响应字典（`success` / `error` / `current_entries` /
  `replaced_entry(s)` / `removed_entry(s)` / `drift_backup` / `deduplicated` …），增加一个
  `operation_id` 键；失败响应**必带** `operation_id`（AC 要求「错误响应携带 operation id」）。
- **拒绝零落盘**：来源（provenance）、delimiter / secret / threat 扫描、pin 名不符、expected revision
  不符，全部在**写回执之前**判定（回执也不算内容落盘，但拒绝路径连回执都不写）——AC4。
- **幂等键 = operation id**：同一 op 已有 COMMITTED 回执 ⇒ 直接回放当时的结果，不再碰磁盘；
  同一 op 处于 NEED_RECONCILE ⇒ 原样返回待对账，不重试。
- **目标集增长 = 新 scope**：确定性 id（`jobp_` / `blkp_` / `del_dg_`）重试时，如果
  COMMITTED 回执不覆盖本次请求的某个 target，commit **不改写旧回执**，而是为每个缺失
  target 派生稳定子回执 `<operation_id>_g<sha256(target)[:16]>`，只发布缺失 target。
  同一缺失 target 的重试回放同一子回执；target 启用变化不会让 formation/blocked 永久
  重试同一个 id，也不会重放已发布内容。`del_dg_` 的幂等键本身也包含 target 集。
- **系统镜像的写前预检**：formation / blocked promote 一次发布多条记录时调用
  `apply_batch_many`：先对每个目标跑同一 mutator（扫描/匹配/最终预算全判），任何一条不合格
  就在写第一条之前抛 `MemoryBatchRejected`（零落盘、零回执）；通过后**先写一份覆盖全部目标的
  PREPARED 回执**，再逐目标一次原子替换。后一目标失败时已发布目标仍在该回执里，重试与启动
  对账都能按 before/after 继续，不会出现「内容已发布、却没有回执可查」的孤儿。
  `allow_empty` 只给系统镜像（全部撤回是有意清空），模型工具面的 batch 仍拒绝一次删空。
- **markdown_store 降为文件引擎**：只保留只读 API（`render_snapshot` / `disk_entries` /
  `resolve_entry` / `resolve_batch_entries` / `target_for` / `entry_digest` / `load_from_disk` /
  上限与启用开关）与一个 `mutate_now(target, mutator)` 临界区（锁 + 重读 + 漂移备份 + 原子写）；
  `add/replace/remove/apply_batch/remove_by_digest` 的**实现**整体迁入 `commit.py`。
  静态与运行时两路证明唯一写者（D12）。

### D3 receipt：状态机与对账判定

回执文件名 = `operation_id`，四种状态（命名即 ADR-0056 §3.6 待定项，此处定稿）：

| 状态 | 含义 |
|---|---|
| `PREPARED` | 意图与期望摘要（每目标 before/after digest）已落盘；该目标文件尚未替换 |
| `PUBLISHED` | 多目标操作中 ≥1 个目标已替换、尚有目标未替换 |
| `COMMITTED` | 全部目标已替换且操作自身的后置条件已满足 |
| `NEED_RECONCILE` | 某目标磁盘内容**既非 before 也非 after**（外部编辑或未知），不盲重跑 |

回执字段：`schema` / `operation_id` / `state` / `origin` / `created_at` / `updated_at` /
`targets{<name>: {before_digest, after_digest, planned, published}}` / `operations[]`（可重放的意图） /
`payload`（调用方元数据，如 formation 的 apply 计划） / `result`（成功响应的可回放快照） / `reason`。
终态回执把 `operations` / `payload` / `result` 中的正文（含 `current_entries`、
`replaced_entries`、`removed_entries` 等）替换为 `<field>_digest(s)` + `<field>_chars`：
回执保留「是不是同一份意图」的可核对性，但不长期复制一份记忆正文——删除回执尤其不能把
已删正文留在 `receipts/` 里（未终态回执保持原样供补发布）。
`result` 仍是可回放快照；净化后的条目字段只保留摘要与长度。重放通道（审批重试）
需要被删/被替换原文时，从 stage 时钉住的 `pinned_entry` 重建 `removed_entry(s)` /
`replaced_entry(s)` 后交给派生层，不把回执当第二份正文真相；其它调用方需要原文时从
内容面/记录层取。

摘要口径：`digest = sha256("\n§\n".join(entries))`——对**解析后的条目序列**取摘要（不是原始字节），
与 `entry_digest` 同一族，行尾归一化不影响判定。

**提交算法**（每目标在自己的文件锁内完成「校验 → 回执 → 发布」三步，杜绝 TOCTOU）：

1. 取 op 锁；已有回执则按 D2 的幂等/对账分支先行处理；
2. 取该目标的 `<file>.lock` → 重读条目 → 跑 mutator（扫描/上限/pin/revision 全在这里判）
   → 校验失败 ⇒ 返回失败（零落盘，无回操作执）；
3. 在同一临界区内写 `PREPARED` 回执（记 before/after digest）→ `os.replace` 发布 → 释放锁；
4. 多目标顺序执行；已有目标发布而尚有未发布 ⇒ 回执置 `PUBLISHED`；
5. 全部完成 ⇒ `COMMITTED`（`updated_at` 刷新）。

**对账判定**（`reconcile()`，启动期与 CLI 共用）：对每个未终态回执，逐目标比对磁盘摘要 ——

- 全部命中 `after`（或该目标本来无事可做）⇒ 标 `COMMITTED`；
- 全部命中 `before` ⇒ 尚未发布：按回执里的 `operations` 在锁内**重放**（确定性：before 相同 ⇒
  mutator 输出相同），完成发布后标 `COMMITTED`；
- 部分 `after`、部分 `before` ⇒ 「两文件部分发布」窗口：补发布未完成的 `before` 目标并标 `COMMITTED`；
- 任一目标既非 `before` 也非 `after` ⇒ 标 `NEED_RECONCILE` 并记录原因（**不覆盖新编辑、不盲重跑**）；
- 回执本身缺失但文件已是 `after`（写回执前崩溃）⇒ 该次操作没有可判定意图，不动作；由 `memory reproject`
  （派生层）与 `render_snapshot`（内容面）各自收敛，并在报告中如实列出。

收尾校验（`finish` 前）以回执的 `published` 位为发布事实：目标已发布后，前台并发写仍可能
继续修改同一个文件（formation 不持 content 锁），磁盘离开 `after` 不能反推“这次没发布”。
只有未发布目标才按 `before` / 不可读 / 既非 before 也非 after 判定；后两者分别留待
reconcile 补发布或人工对账。

reconcile 使用同一事实：已发布目标不再重放。发布后用户删除/前台改写属于新的当前内容，
不能把旧 `after` 再写回去（否则会撤销用户的显式删除）；`COMMITTED` 只承诺本次发布发生过，
不承诺文件此后一直停在 `after`。

**重试路径同样以 `published` 位为准**：未终态回执里已发布的目标，即使磁盘回到 `before`
（用户删除了刚写入的条目、或恢复了刚删除的条目），重试也不重跑 mutator，只补终态/回执；
否则 add 会把用户删除复活、remove 会把用户恢复的内容再次删掉。未发布目标仍按
`before` / `after` 判定补发布。

### D4 job / lease / 幂等（Formation 状态职责）

一 job 一文件，文件即幂等键：

- `job_id = "job_" + sha256(idempotency_key)[:32]`——**确定性派生**。`enqueue` 在全局
  `jobs/enqueue.lock`（与 claim 同一把锁）内先查同 id 文件、再原子写：已存在（含终态）⇒
  原样返回既有记录。同一 key 物理上不可能产生第二个 job；不用 `O_CREAT|O_EXCL` 是因为
  先写临时文件再 `os.replace` 才是本仓统一的崩溃安全形状。
- 字段沿用现有语义与取值：`MemoryJobStage`（`queued/forming/adjudicating/applying/completed/degraded`）、
  `MemoryJobOutcome`（`committed/no_write/blocked`）、`lease_owner` / `lease_expires_at`（默认 300s，
  语义与现值一致）、protected-fact 三列、`state` / `reason`。
- 变更一律在 `<job_id>.lock` 内读-改-写；`claim` 先持全局 `jobs/claim.lock` 选候选，再在 job 锁内
  写 lease（固定锁序：claim 锁 → job 锁）——跨进程也是同一个 CAS。全局 claim 锁同时把「同用户只有
  一个在途 job」的扫描串行化，因此不需要按用户再分一把锁。
- **lease 丢失零 commit**：`commit_with_outcome` 全程持 job 锁；先在锁内复核「lease 属我且未过期」
  （不满足则 `work` 根本不执行），`work` 的记录事务提交前再复核一次（期间被抢占则回滚、零写入，
  返回 `None`）；`after_work`（markdown 补落）与终态写入仍在**同一持锁区间**内完成——抢占者只有在
  我们释放后才可能拿到锁，而那时终态已经落盘，job 不可再被 claim。`transition` 与 protected-fact
  的写方法同样在 job 锁内复核 lease。
- `list_recoverable()` = 全部非终态 job（含 lease 已过期者）；旧 `commit_with_outcome` 的
  「一个事务里写 records + 终态」被拆为「内容（D2）→ 派生投影 → 终态」三步，终态 CAS 语义保留。

### D5 approval / pin（Approval 状态职责）

待审写入 = `staged/<staged_id>.json`，被拦下的后台写入 = `blocked/<blocked_id>.json`。

- 状态值：staged `pending / approved / rejected`；blocked `blocked / promoted / discarded`。
  新写入**不再产生** `approving` 中间态——裁决由 `<staged_id>.lock` 串行化（见下）；
  旧库/旧文件里的 `approving` 只按租约读（`APPROVING_LEASE = 5min`，超期视作 pending）。
- **pin 语义照 Hermes 移植面收紧**：stage 时把目标条目全文钉进 `pinned_entry`；
  approve 重放时逐条比对，名不符 ⇒ 拒绝并**留在 pending**（不静默降级、不自动批准）。
- approve 在 `<staged_id>.lock` 内完成「重放 → 状态」：发布走 `MemoryCommit` 的
  `add / replace / remove / apply_batch`（`operation_id="stgp_<staged_id>"`），成功 ⇒ 写
  `approved` + `disposed_at`；失败或 pin 失效 ⇒ **不写状态**，记录留在 pending 并返回
  blocked。并发的第二个 approve 等锁后看到终态，返回 "already approved/rejected"。
- kill 在「发布后、状态更新前」⇒ 回执已是 `COMMITTED`；用户重试 approve 时同一
  `stgp_` operation id 直接回放回执（幂等、不重复写内容），随后写 `approved`。
  不需要启动扫描专门修 staged：pending 记录只在一次新的裁决动作里前进，回执回放
  保证重复裁决不会二次落盘。
- promote 同样改为「内容（`blkp_<blocked_id>_apply`）→ records → 状态 promoted」；被拦下的输入在
  终态前不消失，`insert_in` 的去重键 `(identity, content_hash)` 保留，重复插入返回既有行（同 job 且
  仍 `blocked` 时）而不是静默丢弃。

### D6 settings（本机设置职责）

`settings.json`：`{schema, values:{四开关}, updated_at}`。

- **只存显式写过的键**——「缺行用默认」与「显式设默认」由此可区分（ADR-0056 §3.4 判据）。
- **不带身份路由**：文件不存 tenant/user/project（裁决 7：禁换名 `owner_id` 复制三元路由）；
  `get_settings/update_settings` 保留 `trusted` 形参以兼容调用面，但不再据此分片——本机设置就是本机设置。
- 旧 `(tenant_id,user_id)` 行的导入见 D9：**不静默折叠**。

### D7 tombstone（删除抑制职责）

`tombstones/<memory_id>.json`：`{schema, memory_id, root_id, version, scope, project_id,
deleted_at, expires_at, deletion_reason, content_hashes[], source_hashes[]}`。

- 过期策略不变（30 天）；`purge_expired()` 只删已过期文件。
- **先写墓碑，再发布内容删除**：任何时刻都不存在「内容已消失、抑制记录缺失」的窗口——
  这是保守方向（宁可短暂抑制一条仍在文件里的内容，不可漏抑制一条已被删除的内容）。
- 抑制判定 `suppresses(draft, trusted)` 由**本 store** 实现，`SqliteMemoryV2Store._insert` 的
  AUTOMATIC 重放检查改为读它（store 不再拥有墓碑表）。
- records 行、outbox、FTS 仍在 SQLite —— 它们是派生态（ADR-0056 §3.8），本 ADR 不动其载体。

### D8 统一写序（消除双向分叉）

所有写者统一为：**① 计划与校验（纯计算，拒绝零落盘）→ ② 内容发布（`MemoryCommit`，权威）
→ ③ 派生态投影（records / outbox / FTS）→ ④ 操作终态（job / 审批 / 被拦下状态）**。

多记录系统镜像（formation apply、blocked promote）的 ① 是 `apply_batch_many` 内的**跨目标全量预检**
（`_preview_batch`：内容扫描 / pin / 最终预算；不合格抛 `MemoryBatchRejected`、不写回执），
② 是在同一份回执下逐目标一次原子替换：首个目标发布后回执转 `PUBLISHED`，全部落地转
`COMMITTED`（`jobp_<job>_apply` / `blkp_<blocked_id>_apply` 覆盖全部目标）。
这样「第二条超限」在写第一条之前就被拒；跨文件仍不原子，IO 失败的半批由回执对账补齐（D3），
不会出现「records 回滚了、内容却已发布且无人可对账」的孤儿。

被翻转的路径：formation apply（原 SQLite 先、Markdown 后仅警告）、blocked promote、
`delete` / `bulk_delete`（原 SQLite 先、墓碑同事务、Markdown 后仅警告）。
created/update/invalidate/edit 原为 Markdown 先，只需把「Markdown 直写」换成「经 commit」。
「文件拒写 ⇒ 不报 committed」由此成为结构性事实：② 失败时 ③④ 根本不执行。

系统镜像的跨文件发布（formation / blocked promote）用 `apply_batch_many`：全部目标先预检，
再写一份覆盖全部目标的 PREPARED 回执，最后逐目标替换。跨文件替换不可能原子（D1），
后一目标失败时 job / blocked 拿到 `MemoryCommitError`（带 operation id，不进入不可恢复的
DEGRADED），重试或启动对账按同一回执补齐；失败目标与已发布目标都可从回执定位。

### D9 旧 SQLite 操作状态的一次性导入 + 旧 completed-but-unpublished 对账

`legacy_import.py`（只读旧库、幂等、不删任何旧行）：

| 旧载体 | 导入规则 |
|---|---|
| `memory_v2_settings` | 恰一行 ⇒ 原值导入；多行且四值全同 ⇒ 导入（值等价，记日志）；多行且有差异 ⇒ **不导入**、不折叠，写 `memory-state/settings-conflict.json` 供人工裁决并保持 marker 未完成；该文件存在而 `settings.json` 缺失时，settings store 抛损坏状态（runner fail-closed：审批门按开处理），不把冲突静默成默认值。`settings.json` 已存在 ⇒ 先校验可读，坏文件不写完成标记；可读则不覆盖（崩溃重跑时先写的那份是证据） |
| `memory_v2_tombstones` | 未过期者按 `memory_id` 逐条落到文件；已过期者不导入 |
| `memory_v2_staged` | 全部状态按原值导入：`PENDING` 可被 approve；`APPROVING` 读时按租约回收；`APPROVED/REJECTED` 保留终态，使并发审批看到「已处理」而不是重新执行 |
| `memory_v2_blocked` | 全部状态按确定性 id 导入（含已处置）：`insert` 的查重不分 status，保留终态才不会把同一个决定重新问一遍；只有 `BLOCKED` 出现在待处置列表 |
| `memory_v2_jobs` | 全部 job 按幂等键导入：非终态可被 claim 续跑；终态保留使同一 key 的 `enqueue` 回放既有结果，而不是新建第二个 job |

**旧 completed-but-unpublished 对账**：终态 `completed` 且 `outcome=committed` 的旧 job，按其
`state.memory_ids` 回查 records 的 `content`，对 `target_for(kind, tier)` 的目标文件逐条核验；
文件里没有 ⇒ 经 `MemoryCommit` 补发布（`operation_id = "legp_" + sha256("<job_id>:<memory_id>")[:32]`，
内容去重幂等）；records 行已不可查 ⇒ 写 `NEED_RECONCILE` 回执，不编造内容。导入回执落
`legacy-import.json`（计数 + 旧库指纹）；有未收敛项时不写完成标记，下次启动重试。

### D10 恢复入口

1. **启动**：装配层在起服务循环之前跑 `legacy_import` + `commit.reconcile()`（幂等、可重入；
   只做对账与状态修正，不重放有副作用的用户操作、不阻塞启动的 job 执行在后台仍照常 claim）。
2. **CLI**：`memory reconcile`——打印每条回执的判定（committed / republished / needs_reconcile）
   与旧库导入计数；有未收敛项 ⇒ 非零退出（与 `memory reproject` 同口径）。
3. **没有独立的 handler 注册表**：对账只把回执推进到终态；状态机由各 owner 的既有入口
   在下一次动作里前进（staged 重试 approve ⇒ `stgp_` 回放后置 approved；blocked 重试
   promote ⇒ `blkp_` 回放后置 promoted；job 被重新 claim ⇒ `jobp_` 回放后写终态）。
   这些 owner 本来就有幂等重试入口，再维护一份启动期 handler 表会形成第二套状态推进路径，
  违背 ADR-0056 §3 的单一 owner。`mcf_` / `del_` / `legp_` / `mig_` 无状态机需要收尾——
  内容已是真相，派生收敛走 `memory reproject`。

### D11 并发原语（理由与边界）

`flock(2)` / `msvcrt.locking` 的 advisory 锁（仓内既有先例：`instance_lock.py` 与
`markdown_store._file_lock`，本次抽到 `file_state.py` 复用一处实现）：进程崩溃自动释放，
无死锁残留；单机单用户场景不需要分布式租约。锁序固定：**op 锁 → 目标文件锁 → 无**；
claim 路径：**claim 锁 → job 锁**。反向获取一律不写进代码。

另有服务实例内的 `asyncio.Lock`（`MemoryV2Service._content_lock`）：`create` / `update` /
`invalidate` / `edit` / `delete` / `bulk_delete` 与 `MemoryTool.content_write_scope` 先取
**content 锁**，再走 op 锁 → 目标文件锁 → SQLite；锁序是 **content → op/文件 → SQLite**。
`delete` / `bulk_delete` 在 content 锁内先写墓碑文件（`write_json` 的 tempfile +
`os.replace` 原子替换；墓碑 store 没有独立文件锁，并发 record 是最后写者胜），再走
op 锁 → 目标文件锁 → SQLite；墓碑写入不获取目标文件锁。
formation 的 `mirror_committed` 在 SQLite 写事务内发布，反向取 content 锁会与 delete 形成
锁序反转，因此不进入该临界区（跨进程仍由墓碑重入收敛）。

### D12 唯一写者的机器化证明

- **静态**：`mutate_now` 的调用者只允许 `commit.py`；测试遍历 `src/agent_harness/**/*.py`，
  断言除 `markdown_store.py` 自身与 `commit.py` 外没有任何模块调用它，也没有第二个模块打开
  `memories/` 下的写入句柄。
- **运行时**：同一测试驱动真实入口（`memory` 工具、service create/update/invalidate、edit、
  delete/bulk、staged approve、formation apply、blocked promote），对每次内容变化断言
  **恰好一条**回执、且 op 前缀与入口对应；任何未走 seam 的写入都会绕开回执而被断言抓住。
  `mig_`（migration / identity apply）与 `legp_`（旧 state 补发布）不在该用例的驱动面上，
  由 `test_1493_identity_mapping.py` 与 `test_1494_legacy_import.py` 覆盖；两者同样只经
  `MemoryCommit` 入口，静态图（本节第一条）已排除第二写者。

## 3. 方案依据（AGENTS §6.1 / 协议 §1.3）

| 来源 | 机制（实读） | 契合点 | 判定 |
|---|---|---|---|
| [Hermes memory 文档](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/memory.md)（2026-10-26 实读；MIT 仓库的文档页） | MEMORY.md（2200 字符）/ USER.md（1375）为有界内容文件，位于 `~/.hermes/memories/`；工具面 `add` / `patch` / `remove` 用 `old_text` 唯一子串寻址；`memory.write_approval` 默认关，开启后非交互写入**按条落盘待审**（skills 的待审文件为 `~/.hermes/pending/skills/<id>.json`），`/memory pending` / `approve <id>|all` / `reject <id>|all` 处置；**待审的 patch/remove 记录它要改的整条条目，审批只在目标条目未变时生效，否则拒绝并留在待审**；缺 pin 的旧待审项必须拒绝重建 | 本仓 M1/M6 的既有移植面上游；D5 的 pin + 「名不符拒绝、留 pending」正是该机制的逐条对应；「按条一文件待审」与 D1 的 `staged/` 目录同形 | **ADAPT**（机制移植已在仓内，本 ADR 只把载体从 SQLite 换成文件；不复制上游代码） |
| [AWS Transactional outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html)（2026-10-26 实读） | 跨介质双写必留失败窗口（DB 成、通知败；通知发、DB 败）；正解是把「要发布的事件」与主写入放进同一事务（outbox 表），由独立扫描器只转发**已提交**的行，成功后删除；重复投递是常态 ⇒ 消费方必须幂等（track processed messages）；顺序按时间戳/序号保持 | 回执就是内容面的 outbox 行：意图与 before/after 摘要在发布**之前**落盘，扫描器（启动/CLI reconcile）只处理已落盘意图，完成后置终态；operation id 即去重键 ⇒ 重复执行幂等 | **PORT DESIGN**（借判据与形状，不引 AWS 任何服务、不复制示例代码；本仓旧 outbox 表的存在与否归 #1496） |
| DeepSeek Harness 本地浅克隆 `D:\reference\deepseek-harness` @ `5badb15009ae1756c3afe0ae0cef1faafc290ccc`（MIT）`packages/attachment/attachment-local/src/store.ts:127-215` | `syncDirectory`：文件同步不够，目录项未落盘则崩溃后引用悬空，故发布目录也要 fsync（Windows 无目录 fsync ⇒ 由 NTFS 元数据日志承担，代码显式跳过）；`publishImmutableObject`：staged write → fsync → 硬链接入位 → digest 校验的 EEXIST 去重；`ensureDurableDirectory` 对每一级祖先补 fsync（「已存在」不等于「已持久」） | D1/D11 的落盘原语：tmp + fsync + `os.replace` + POSIX 目录 fsync + 平台分支；「摘要校验的去重」对应 D9 的补发布幂等 | **ADAPT**（同一实现家族的先例，本仓按 Python 重写，不复制代码） |
| 本仓既有 | `markdown_store._file_lock`（flock/msvcrt 双平台分支、崩溃自动释放）与 `instance_lock._take_os_lock`；`docs/agents/reference-sources.md` §原子文件写/跨平台文件锁（POSIX `rename(2)`/`flock(2)` 语义权威、`filelock` 事实标准） | D11 直接复用仓内已证实现，不引新依赖 | **REUSE** |

**License 结论**：Hermes 为 MIT（本仓 `memory_tool.py` 头部已记录来源与上游 commit，未新增复制）；
DSH 为 MIT；AWS 文档仅借判据。**本次无代码复制**；未来实质 Port 须按 §6 末段补 License 与来源。

## 4. AC 映射

| AC | 落点 | 验证 |
|---|---|---|
| AC1 同一 commit | D2/D8：tool / service create-update-invalidate / 已授权 edit / delete / bulk / staged approve / formation apply 全部经 `MemoryCommit`；可信用户动作不二次审批（`evaluate_gate` 不变） | D12 运行时回执断言 + 各入口既有 AC 测试 |
| AC2 各 owner 非 SQLite 持久 + 重启验证 | D4–D7 的五份文件 store；旧值经 D9 导入 | 各 store 的重启/重读测试 + `legacy_import` 测试 |
| AC3 真实 kill 四窗口可对账 | D3 的对账判定；四个窗口 = 文件发布前 / 替换后回执前 / 审批状态更新前 / 两文件部分发布 | 真实子进程 kill 测试（`_commit_kill_child.py` 复用既有模式） |
| AC4 幂等 / 零 commit / 拒绝零落盘 | D2 幂等 + D4 lease 守卫 + D2 拒绝路径 + D9 旧 completed-but-unpublished | 同 key 一次逻辑更新、lease 丢失零 commit、拒写不报 committed、扫描/pin/revision 拒绝零落盘 |
| AC5 唯一写者 + 可观察 | D12 静态 + 运行时；失败响应带 `operation_id`（ToolResult 可观察）；单文件原子不冒称跨文件原子/断电耐久（D1 末） | D12 测试 + 响应断言 |

## 5. 红线与不做

- 不设「终态无 SQLite 例外」的例外：job/审批/设置/墓碑/回执**全部**离开 SQLite（D1–D7）。
- 不把操作状态写进 Markdown 正文；Markdown 只有内容（D2/D8）。
- 不扩大为「全仓禁 SQLite」：records / outbox / FTS / 投影账本仍是派生态，载体归 #1496 处置。
- 不新增身份路由；不改状态值**字面**（`staged.approving` 等仍可读）以免既有观测面漂移；
  但新写入不再产生 `approving` 中间态（D5，裁决改由文件锁串行化）。
- 不删旧库任何行、不删旧表；`memory-v2.db` 的退役属 #1496。
- 不重做 #1461/#1437/#1482 的既有残余语义（提案/投影/对账各自的判据保持）。

## 6. 未决

- 派生层（records/outbox/FTS）的退役或重建入口（ADR-0042 D7.1 的运维命令缺口）→ #1496。
- 读侧切换（markdown 为检索真相）→ #1495。
- 墓碑在检索注入路径的拒绝语义在切换后如何保留 → #1495 AC4 / #1496 AC3（本 ADR 只保证载体迁移）。
- 多身份（PG 级）若在未来启用，settings 的「本机单份」需要重新裁决——本 ADR 明确按本机语义落地。
