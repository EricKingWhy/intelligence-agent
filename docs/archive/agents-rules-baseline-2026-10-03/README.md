# 原版保留与恢复说明（2026-10-03）

原版来源：`aefffe6f8cb54a15953ff8c04f810b781d7f708f`。实测 v10 来源：`a315bab0509019a07ed3900acc7484ef4ec9860e`；最终来源：`18bc317934ec53bbe013621369f899d93c155331`（仅澄清 CLAUDE 的 Skill 位置）。
本目录保留原版、v9、实测 v10 和最终版的 AGENTS/CLAUDE Git blob 原字节，以及原版/v10 的 SDD 协议、审查 playbook；manifest.json 给文件与 SHA256。使用带版本后缀的文件名，避免备份被 Agent 当作目录规则自动加载。

备份不是现行指令。原版中用户已批准删除的凭证条款和过期操作历史仅为回滚保存；不重新当作现行政策。

## 需要恢复时

当前没有执行恢复。本说明只针对规则文件，不覆盖业务代码或整仓历史。恢复前确认当前仓库/分支/HEAD/status，并审阅这些路径是否有后续改动；有改动先保留 diff，不覆盖其他 Agent 的工作。

在目标仓库确认可恢复后，可执行：

```powershell
git restore --source=aefffe6f8cb54a15953ff8c04f810b781d7f708f -- AGENTS.md CLAUDE.md docs/SDD_WORKFLOW_PROTOCOL.md docs/agents/review-debug-playbook.md
git diff --check
git diff --stat
```

以上四文件是本次规则变化中原版已存在且需恢复一致性的文件；新 Git 手册可保留作闲置档案，不必删除。docs/README 等入口索引不定义授权或流程，旧根不依赖新手册。先核对四文件内容，再把恢复作为新的本地提交；发布仍按现行授权，不执行 reset --hard / force push / 整分支 revert。

若原 commit 在另一 clone 不可见，先从包含它的仓库 fetch 该对象；或依据 manifest 核验本目录原版快照后，用按文件的原子替换恢复上述对应文件。不要把 snapshot 目录里的 v9/v10 文件误当原版。

HTML 对比页在 docs/agents/agents-md-comparison-2026-10-03.html；完整逐条定位仍以 docs/agents/agents-md-rule-migration-2026-10.tsv 为准。
