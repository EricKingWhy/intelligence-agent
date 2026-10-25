# 旧身份/版本映射（记忆转向第 3 步，#1493）

本文件是 `src/agent_harness/memory/v2/identity_mapping.py` 的设计说明与操作手册。
切片的唯一入口是 `agent-harness memory identity <action>`（`plan` / `list` / `show` /
`approve` / `resolve` / `apply`）。

## 它解决什么

`migrate_active_records`（`memory/v2/migration.py`）只导 active 的
`memory_id/kind/tier/content`。它的 `exported + deduplicated == active`、`skipped == []`
只证明**那一个窄导出**，不证明身份治理完成（C 卷宗 §3.2 原文）。本模块把范围补到
ADR-0056 §3 点名的八类事实中的五张表：

| 旧载体 | 本模块的处置 |
| --- | --- |
| `memory_v2_records` active | 按 `目标文件 + 正文哈希` 归并成映射项，**保留全部来源**（同文去重不丢出处） |
| `memory_v2_records` 非 active（superseded / invalidated） | 档案项（不写当前文件；历史仍可从旧库/只读档案读） |
| `memory_v2_settings`（四开关） | 逐旧 key 登记；全一致才映射，否则冲突等人工裁决；**缺行默认 ≠ 显式设默认** |
| `memory_v2_staged`（审批态） | 档案项；pending 仍 pending，`approving` 活租约阻断切换 |
| `memory_v2_jobs`（formation 作业） | 档案项；终态幂等键留档、活 lease 阻断切换 |
| `memory_v2_tombstones` | 档案项；内容/来源 hash 与过期时刻留档，**不清理** |

派生态（`memory_v2_fts`、`memory_v2_outbox`）不登记：ADR-0056 §3.8 明确它们不拥有任何
当前事实，可随时从 Markdown 重建。

## 规则（裁决 3 A 案）

1. **只有逐项映射获批后才写当前文件。** `apply` 先过闸：未获批的映射项、未裁决的冲突、
   活 lease、源数据在盘点后漂移——任一条即 `MappingBlocked`，**零落盘**（连
   `memories/` 目录都不建）。
2. **不自动选赢家。** 两条不同正文是否在讲同一件事无法机械判定，所以正文冲突只能由人
   用 `--conflict m1,m2` 点名；裁决时 `--merge <memory_id>` 指名取哪一条。落选的不删，
   进档案项（`archive:entry_conflict:<id>`）并记进 `decision.archived_sources`。
   不提供"最后写入胜出"。
3. **同文去重保全来源。** 归并键是 `(目标文件, 正文哈希)`；一个映射项的 `sources` 列出
   全部旧行（含各自 `tenant/user/project` 迁移来源标签、`row_hash`、`content_hash`、
   `version`、provenance）。去重只减少**落盘条目数**，不减少**来源登记数**。
4. **不扩预算、不截断、不总结。** 放不下的映射项整体进 `budget:<target>` 冲突（列出
   `entries` 与 `excess`）；裁决只能把它们移进档案（`--archive <id>`），不能抬预算。
5. **批复 ≠ 批准记忆写入 ≠ 批准销毁。** 映射获批只授权"写进当前 Markdown"；`apply`
   不删改任何旧库行，receipt 里 `destruction` 恒为 `not_authorized`（裁决 6 只批加密
   备份至 2027-10-23，销毁另行裁决）。

## 清单

`<workspace>/memory-migration-mapping.json`（与 `memories/`、`memory-v2.db` 并列）：

- **不存正文**，只存哈希/ID/身份/版本。人要读原文时按 `memory_id` 回库
  （`memory identity show <id>` 会打印来源 ID 与哈希）。
- 存人的决定：`approved_at`、`decision`（`merge_source` / `archived`）。
- `partition` 是**每次落盘重算**的投影：`rows_mapped + rows_archived + rows_conflicted
  == rows_total`，`rows_total` 等于登记的源行数（逐行恰好一次）。
- 存在即拒绝重新 `plan`：覆盖会丢已批的裁决。要重来就先把旧清单删掉/挪走。

## 切换前置（数据操作，不是代码依赖）

C §3.2 的顺序是「源数据全保全 → 旧身份/项目分组 → 逐条映射与冲突裁决 → 新 owner 验证 →
切换读写」。真实部署跑 `apply` 之前必须先有 **#1492 式一致备份**
（`scripts/memory_freeze_backup.py` 的 SQLite Online Backup API 快照 + `writer-proof.json`
六类 writer 围住证据）。本模块不检查备份是否存在——它只读旧库、只追加写 Markdown；
备份由操作者在 `apply` 前自行满足并留证。

## 与相邻票的边界

- **#1441**：报告口径复用（`active / exported / deduplicated / memory / user /
  kind_defaulted / skipped`，CLI 输出同形），不另造一套计数。
- **#1442 / #1443 / #1444 / #1494**：设置四开关与其它操作状态的**新持久格式**归 #1494。
  本票只登记"哪个旧 key 的哪组值有归宿"，`settings` 映射项不写文件
  （`applies_to_file=false`）。
- **裁决 7**：不把旧 `tenant/user/project` 换名成 `owner_id` 复制成新路由；旧三元组只作
  迁移来源标签。
- **#1495 / #1496**：读侧切换与旧库退役不在本票。本票不删表、不改消费路径。

## 不做

- 不自动批准 pending 写入（`memory_v2_staged` 行只进档案）；
- 不把拒绝/归档算成去重（`skipped` 与 `archive` 分开记账）；
- 不覆盖人手编辑的 Markdown：`apply` 只走 `MarkdownMemoryStore.add`（幂等追加），
  不 `replace`/`remove` 任何现有条目——手改内容原样保留，漂移检测与备份仍是 store 的事；
- 不把任何操作状态写进 Markdown 正文。

## 方案依据（协议 §1.3）

调研日期 2026-10-25，全部为**本次实读**（`curl` 取回原文并抽句；git 用手册页
`git 2.43.0` 本地 `man git-merge`）。票面原话是"未找到材料已核实的同形成熟产品先例"——
本节区分**已验证**与**仍未核实**，不把线索升格为先例。

### 1. git-merge（已验证，一手手册）

- 来源：`man git-merge`（git 2.43.0，本机 `/usr/share/man/man1/git-merge.1.gz`），
  "HOW CONFLICTS ARE PRESENTED" 一节；线上同文 <https://git-scm.com/docs/git-merge>。
- 机制摘要：原文 "When both sides made changes to the same area, however, Git cannot
  randomly pick one side over the other, and asks you to resolve it by leaving what both
  sides did to that area." 冲突区用 `<<<<<<< / ======= / >>>>>>>` 标记，`merge.conflictStyle
  =diff3|zdiff3` 再补共同祖先段；合并不自动收敛，等人工改完再 `git add`。
- 契合点：与裁决 3 A 案同形——**两侧改动都在场、不挑赢家**；但它作用于整文件文本，
  我们的迁移单元是条目，且必须先把候选清单交人（清单文件）而不是把冲突写进正文。
- 判定：**ADAPT**（借"两侧共存 + 人工裁决后才继续"的语义，不借文件内冲突标记）。

### 2. dpkg conffile 处理（已验证，一手手册；最接近的同形先例）

- 来源：`dpkg(1)`，Debian bookworm manpages
  <https://manpages.debian.org/bookworm/dpkg/dpkg.1.en.html>（2026-10-25 实读，
  `--force-confdef` / `--force-confold` / `--force-confask` 段落）。
- 机制摘要：包升级时若某个 conffile 被本地改过且包内版本也变了，dpkg **停下来问用户**；
  `--force-confold`/`--force-confnew` 是**显式**的非交互覆盖，`--force-confdef` 明写
  "If there is no default action it will stop to ask the user"。`--status-fd` 会把
  `conffile-prompt` 作为结构化记录发给外部程序（人可脚本化裁决）。
- 契合点：几乎逐条对上本票的三条形状——**多来源同一事实冲突 → 不自动决定 → 等人逐项选 →
  选择结果可被结构化消费**；而且"缺省没有默认动作就问人"正是我们 `MappingBlocked`
  的语义。差异：dpkg 管的是"同一文件的旧/新版本"，我们管的是"多个旧身份下的多行"，
  且我们必须额外保住每个旧行的来源映射（dpkg 没有这层要求）。
- 判定：**ADAPT**（借"逐项提问 + 显式覆盖开关 + 结构化裁决记录"，不借它的包/文件模型）。

### 3. Apache CouchDB 复制冲突模型（已验证，一手官方文档；**部分反向**）

- 来源：Apache CouchDB 3.5 Documentation, "Replication and conflict model"
  <https://docs.couchdb.org/en/stable/replication/conflicts.html>（2026-10-25 实读）。
- 机制摘要：默认读**会**用确定性算法挑一个 revision 作为 "winner"（"By default, CouchDB
  picks one arbitrary revision as the winner, using a deterministic algorithm"），
  但冲突 revision **全部保留**，应用须显式解决："they could attempt to merge them,
  write back the merged version, and delete the conflicting versions"。
- 契合点：保留全部冲突版本 + 解决动作显式，这两点与我们一致；**但"默认读挑确定性赢家"
  与裁决 3 A 案相反**——我们不在当前文件里挑赢家，未裁决就阻断切换。所以这个来源只用来
  印证"冲突副本必须保留"，不借它的 winner 语义。
- 判定：**ADAPT（仅保留侧）**；winner 选择机制明确 **不采用**。

### 4. Dropbox conflicted copy（已验证，一手帮助中心；线索已澄清）

- 来源：Dropbox Help Center, "What's a conflicted copy?"（Updated May 15, 2025）
  <https://help.dropbox.com/organize/conflicted-copy>（2026-10-25 实读）。
- 机制摘要：多端同时编辑时生成一份带 "conflicted copy" 与保存者的副本，原文件不被覆盖；
  原文 "The best way to resolve a conflicted copy is to compare both versions and merge
  them manually"，并且 "The last version saved will always appear as the conflicted copy"。
- 契合点：证实票面那条**未核实线索**的方向——它是"保留两份 + 人工比对合并"，**不是**
  最后写入胜出。但它**不能**作为"隔离归档"（选项 C）的先例：冲突副本仍在同步目录里可见，
  与"进只读档案、当前不可见"不同形。
- 判定：**DEFER**（不作为机制来源；只用于纠正"最后写入胜出是业界常见做法"这一误判）。

### 仍未核实（不在本节主张范围内）

- 没有任何来源给出"跨多个旧租户身份把记忆合进一个单人文件"的同形先例。上述四家都是
  **同一文件/文档/配置项的多版本冲突**，不是**多身份 → 单一 owner** 的归并。
  因此票面"未找到已核实同形先例"在这一层仍然成立，本票不声称找到先例。

### 综合判定

- **REUSE**：`MarkdownMemoryStore.add`（幂等追加、分隔符/扫描/预算三道校验）、
  `entry_digest`、`target_for`、`migration._kind_defaulted`（#1441 报告口径）、
  `APPROVING_LEASE`（审批租约常量）。不为迁移另写一套写盘/哈希/分拣。
- **ADAPT**：把 dpkg 的"逐项提问 + 显式覆盖"与 git 的"两侧共存"合成一个**清单文件 +
  三个动作**（approve / resolve / apply）的流程边界。
- **BUILD（最小）**：仓库没有"清单 + 闸门 + 归档"这套判定层，故新增
  `memory/v2/identity_mapping.py`（一个模块 + 一个 CLI 子命令 + 一份 runbook）。
  不新增依赖、不新增持久格式（清单是 JSON 文件，与 `writer-proof.json` 同类）。
- **DEFER**：设置四开关与操作状态的新持久格式（#1494）；备份工具本体（#1492 已独立
  实现，本票只在操作顺序上引用）；真实数据盘点与销毁（所有者现场）。
- **License**：无代码复制；仅借用机制语义，来源按上列 URL 具名引用。
