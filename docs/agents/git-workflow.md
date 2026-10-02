# Git 操作手册

> 同步、merge、push、PR merge、冲突处理或关单前，必须读取对应章节。
> 授权唯一维护在根 `AGENTS.md` §14.4；本手册定义执行步骤，不新增权限。
> 必读文件缺失/读取失败时停止依赖它的动作并报告，其他独立只读分析可继续。

## 1. 操作前检查

1. 明确当前用户授权和动作；按 AGENTS §14.4 判断可自行执行、常设授权或需单独批准。
2. 对所有涉及的仓库用 `git -C <repo>` 核实 `branch --show-current`、`rev-parse HEAD`、`status --short`。
3. 用 `worktree list --porcelain` 看单个 clone 内的 linked worktree；三个 D:\intelligence-agent* clone 不是彼此的 worktree。
4. merge/rebase/reset 的目标工作树须 clean；fetch 不改工作树，但仍先确认仓库/ref。
5. 只读查看别的分支，用 log/show/diff，不为了检查切换分支。
6. 同时只集成一条线；上一条线集成后重新分析下一条线。

完成条件：仓库、branch、HEAD、工作树、动作授权已明确；尚未处理的冲突/用户改动不被覆盖。

## 2. 仓库与同步

### 2.1 获取最新基准

开工前或跨 clone 集成前，先取得最新集成基准，再判断是否落后。未经更新的本地 main 不能证明已同步。

```text
git -C <施工仓库> fetch origin --prune
git -C <施工仓库> fetch D:/intelligence-agent main:refs/remotes/audit/integration-main
git -C <施工仓库> rev-parse refs/remotes/audit/integration-main
git -C <施工仓库> merge-base --is-ancestor refs/remotes/audit/integration-main HEAD
```

- `refs/remotes/audit/integration-main` 是明确定义的审计 ref，不是本地工作分支。
- 施工仓库就是集成 clone 时，现场核实本地 main；不要把旧缓存当作最新 ref。
- 本地集成以集成 clone 的 main 为基准；准备 PR 发布时还要核对新的 `origin/main`。
- 若 origin/main 比本地集成 main 新，先处理该差异再决定集成；不要选一个旧 ref 来得到绿检查。
- 基准对象缺失时先 fetch。fetch 失败或 refs 不明确，报告后停止依赖该基准的合并/施工。

基准不是 HEAD 的祖先时，先分析双方独有提交与 diff，再显式 merge 最新基准。
自己的短分支同步 main 属常设授权；发生冲突转 §4，不能自动改冲突文件。
不默认用 git pull；rebase 仍按根授权表处理。

完成条件：记录实际基准 SHA；祖先判断针对该最新基准，或已完成获授权同步及验证。

### 2.2 文件级避让

修改共享文件前检查另一条施工线。先读取对侧 branch、HEAD、status，然后同时检查：

```text
git -C <对侧> status --short
git -C <对侧> diff --name-status -- <拟改路径>
git -C <对侧> diff --cached --name-status -- <拟改路径>
git -C <对侧> log --oneline <已取得的最新集成SHA>..HEAD -- <拟改路径>
```

未跟踪文件由 status 显示；未暂存由 diff 显示；已暂存由 diff --cached 显示；提交由相对最新集成基线的 log 显示。
对侧缺该基准对象时先 fetch 到审计 ref。检查对侧用只读命令；未经授权不要改变它的 checkout/本地分支。
不要只看 main..HEAD：对侧直接在 main 施工时该范围为空，未提交改动也不在 log 中。
发现同文件在途工作就先协调，等它集成或指定一条线负责；其他不冲突路径可继续。

完成条件：同文件的未提交、暂存和已提交在途工作均已检查，没有并行覆盖。

### 2.3 跨 clone 内容比较

```text
git -C <repo> rev-parse <rev>:<path>
git -C <repo> rev-parse <rev>^{tree}
git -C <repo> diff --name-status <可见SHA-A>..<可见SHA-B>
```

跨 clone 使用 Git blob/tree/commit。对象不在本 clone 时先 fetch；不把工作树 CRLF/LF 差异报告为内容漂移。
必须比较工作树时先明确行尾归一化；不要据原始文件哈希判断 tracked 内容是否相同。

## 3. 集成与发布

### 3.1 先回后正

1. 现场刷新 origin/main 和集成 main；锁定来源 HEAD、共同基线、diff 和文件交集。
2. 短分支先合最新 main，再在施工线处理冲突、测试和独立审查。直接在施工 main 提交时，没有合回自身的步骤。
3. 必须读取 `docs/SDD_WORKFLOW_PROTOCOL.md` §7/§8.1 和 `docs/agents/verification.md` §1–§2，按当前完整门禁取得证据。
4. 检查工作树 clean、diff 可解释、无误删/越界/覆盖其他线成果，运行 git diff --check。
5. 运行 `python scripts/check_review_coverage.py` 并确认 exit 0；未覆盖代码补真实审查。判据和机械归属例外读协议 §7 第8条，不在本手册重复枚举。
6. 门禁和 review 条件满足后，将来源集成到 D:\intelligence-agent 的本地 main，快进优先；异常复杂冲突转 §4。
7. 按 §3.2 核对被集成树与已验证树；确认前后端集成行为。
8. 先取得分支 push 的明确批准，再推集成分支并创建指向 main 的 PR。
9. 等服务端 gate0 绿；strict 要求与 origin/main 同步时，返回施工分支同步、再验证新增 diff。
10. 单独取得 PR merge 批准，再按当前服务端允许的合并方式合并 PR。
11. fetch origin，显式分析并将本地 main 同步到新的 origin/main；非快进或冲突不能强制对齐。
12. 通知另一条施工线或用户回补；另一条线下次开工前按 §2 获取新基准。

完成条件：每一步对应当前授权；集成树有可核对验证证据，发布有 CI 和批准，其他线已获回补通知。

### 3.2 已验证树与集成树

```text
git -C <施工仓库> rev-parse <已验证SHA>^{tree}
git -C D:/intelligence-agent rev-parse main^{tree}
```

树不同：在集成树重新跑完整门禁。树相同：按协议 §7 第8条及 §8.1 核实读数可传递，不重复跑同一冻结树全量。
树相同不能单独证明 review coverage 或基于 commit 图的检查通过；这些仍按当前图独立对账。
Gate-0 使用机器落盘 docs/gate/<sha>.json；其他重车道保留可复跑命令、SHA/tree 和输出证据。不得手抄读数替代证据。
落盘前工作树与 HEAD 的一致性由 gate0 自身检查；校验失败不能引用该 HEAD 的树冒充被测树。

### 3.3 服务端保护与 CI

发布前只读核实 main 的保护与仓库合并设置。当前约定是必需 gate0、strict 同步、管理员同样受约束、禁止 force push/删除 main，PR 使用 merge commit。
核实命令：

```text
gh api repos/EricKingWhy/intelligence-agent/branches/main/protection
gh api repos/EricKingWhy/intelligence-agent --jq '{allow_merge_commit,allow_squash_merge,allow_rebase_merge}'
```

工作流 `.github/workflows/gate0.yml` 说明检查如何执行；服务端 API 才能证明它当前是否被设为 required。
本地绿不代替 CI 绿。Gate-0 的机械快车道不代替完整门禁、review、e2e 或真机验证。
保护/闸门自身修改仍需人检查 diff；名字叫 gate0 的绿检查不能证明判据没有被放宽。
不走 push origin main 或 --admin 绕过路径。若确有破窗需求，先说明理由并取得修改保护的明确授权。
保护变化不自动产生新授权，也不取消项目的 CI/质量要求。

## 4. 冲突处理

发生冲突后停止自动解决，未经批准不改冲突文件、不暂存。逐文件报告：

1. main 改了什么、目的是什么；
2. 施工线改了什么、目的是什么；
3. 冲突原因；
4. 两侧逻辑能否同时保留；
5. 推荐最终语义；
6. Contract 影响；
7. Runtime Behavior 影响；
8. Test 影响；
9. 风险等级。

取得对具体推荐语义的批准后，做最小修复、必要回归、diff 核对，再暂存。
不机械使用 ours/theirs，不为消冲突删一侧逻辑。批准某个冲突不自动批准 push、PR merge 或其他冲突的任意处理。
本地 main 出现未预见的复杂冲突，优先 git merge --abort，再回来源仓库处理。

完成条件：批准的语义被保留、冲突已消除、相关验证通过；失败或新架构取舍按 AGENTS §9.1.1 报告。

## 5. 关单与交付

关单前实际检查代码、AC、测试和集成状态，不凭记忆或进度文档。

- 完成交付且合入 main、门禁通过：直接关闭对应 issue，comment 给 commit 和验证证据。
- 完成交付但未集成：按现行关单授权关闭，comment 明确分支、commit、集成负责人。
- 部分交付：不关单；comment 记录已完成与剩余项。

交付按 AGENTS §11 报告改动、规格符合性、验证、剩余与风险；记录落点按 §16.1 与协议 §5/§8.5。
执行关单使用结构化参数或 UTF-8 body 文件，避免把中文长文本放进 shell 命令行；写盘步骤按协议 §8.9。

## 6. 历史与来源

本手册由原 AGENTS.md §13–§14 的操作细则迁移；授权以当前根文件为准。
旧正文及事故叙述可从基线 commit `aefffe6f8cb54a15953ff8c04f810b781d7f708f` 的 AGENTS.md 查看。
保护开启前的直推授权、旧 worktree/长期分支模型、旧测试基线都是历史，不作为当前操作指令。
本次迁移、纠错和模型验收记录：`docs/agents/agents-md-optimization-2026-10.md`。
