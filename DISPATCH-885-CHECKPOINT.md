# DISPATCH-885 CHECKPOINT

> 断点续跑状态。每完成一个阶段更新。

- 任务：#885 Windows installer smoke 跑在 GitHub Actions windows-latest
- 分支：`codebuddy/885-windows-installer-smoke-ci`
- worktree：`~/workspace/intelligence-agent-wt/885-win-smoke/`
- 开工时 `origin/main`：`b43efca8717a4b4da93ed998f06adfb5a0891515`
- PR：#887（https://github.com/EricKingWhy/intelligence-agent/pull/887，OPEN，未 merge）
- 当前尖端：`66424156`（task 派工时同步于远端）

## 规则已读证据（五项启动检查表）
1. `~/workspace/system/agent-workflow-prompt.md`（全文）— 身份/第零~五步/诚实红线
2. `~/workspace/intelligence-agent/AGENTS.md`（全文 546 行）— §3 阅读协议 / §4.4 施工授权 / §6 Reuse / §8 Scope Lock / §9 编码准则（§9.1.1 票面变更 / §9.5 懒惰阶梯）/ §14 Git 门禁台账 / §16 SDD 入口
3. `~/workspace/intelligence-agent/CLAUDE.md`（全文）— 指向 AGENTS.md 的入口
4. `~/workspace/intelligence-agent/docs/SDD_WORKFLOW_PROTOCOL.md`（全文 908 行，V3.1-lite）— §1 Ticket/§1.3 方案依据 / §2 风险 review / §3 完成集成 / §7 闸门（第 8 条覆盖闸门）/ §8 提速增补（§8.1 冻结树 / §8.2 三路并行+派单前 Gate-0 / §8.3 审查预算+修后重审 / §8.8 失败回退边界表）/ §9 skill 引用
5. `~/workspace/intelligence-agent/docs/agents/implementation-discipline.md`（全文）— §9.5 懒惰阶梯边界 / ints 八荣八耻
6. Issue #885 全文（`gh issue view 885`）— 6 条验收 + 不做
7. 任务书 `~/workspace/system/dispatch/885-codebuddy-dispatch.md`（全文）

## 五项启动检查表（V3.1-lite / AGENTS §3）
| 前置项 | 实际读取依据 | 状态 |
| --- | --- | --- |
| Vision 相关原则 | 本票为 CI 基础设施（workflow + NSIS 配置面），非 Agent Runtime/Session 能力；`SPEC_ROOT/00_PROJECT_VISION.md` 的轻量/边界原则不直接约束，**本任务触发细则**第 5 项覆盖 | READY（下文说明依据） |
| 当前任务规格 | 无对应 SPEC 模块（本票是 `.github/workflows/**` + `desktop/installer/**` 施工面，非 §3 模块映射内模块）| READY（无对应模块，理由见下） |
| Reuse 相关判定 | `SPEC_ROOT/13_OPEN_SOURCE_REUSE_MATRIX.md` 无 CI workflow / NSIS 行；复用判定走 electron-builder 官方（§9.1.1 成熟产品研究）| READY（无对应行，理由见下） |
| Phase 依据 | `SPEC_ROOT/14_IMPLEMENTATION_ROADMAP.md` 未列 W-16 CI 票；本票为 #831 验收的 CI 补齐，独立 enhanc                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          ement | READY（非 Phase 内，独立 CI 票） |
| 本任务触发细则 | `docs/agents/implementation-discipline.md`（§9.5-9.6）；`scripts/gate0.py`+`scripts/check_review_coverage.py`（§7 第 8 条覆盖闸门）；`docs/SDD_WORKFLOW_PROTOCOL.md` §8.1/§8.2/§8.3；本任务书。Issue 操作读 `docs/agents/issue-tracker.md` | READY |

## 两处红的根因（已定位）

### 红 A：gate0 coverage FAIL
- `d82b3ab8 ci(#885): Windows installer smoke...` 未审查且未声明。
- 该 commit 改 `.github/workflows/windows-installer-smoke.yml`（**代码/配置面**，非 docs）⇒ 需**真实审查行**，不能靠 docs-only 自动归属。
- coverage 实测：**恰 1 条 ❌**（= d82b3ab8），无其它缺口。

### 红 B：windows-installer-smoke FAIL（run 37828964065, step 6）
- 报错：`!include: could not find: "D:\a\intelligence-agent\intelligence-agent\desktop\installerinstaller-directories.nsh"`
- 根因（**NSIS 主源实锤**）：`66424156` 把 `${__FILEDIR__}` 解析期捕获进 `IA_INSTALLER_DIR`（方向正确——electron-builder 用绝对路径 `!include installer.nsh`，解析期该值 = installer 目录），但拼接时写成 `"${IA_INSTALLER_DIR}installer-directories.nsh"` **缺分隔符**。
- 关键：`__FILEDIR__` 在 Windows **无尾分隔符**：NSIS `Source/scriptpp.cpp:97-100` `#ifdef _WIN32` 走 `GetFullPathName`+`PathRemoveFileSpec`（**连文件名前的反斜杠一起删掉**）⇒ `...\desktop\installer`；POSIX 分支 `my_strncpy(dir,filename,p-filename+1)` **保留尾 `/`**。故同一代码在 Linux `makensis`（本机 3.09：`.../installer/`）编译通过，但 Windows CI（`:97` 分支）拼成 `installerinstaller-directories.nsh`。
- CI 报错路径本身即是权威一手证据（目录部分 = `...\desktop\installer` 正确，仅缺 `\`）。

## 已完成
1. ✅ sync merge origin/main（`23fe62c3`）
2. ✅ 根因定位（红 B 的 NSIS 源级实锤，见上；本机 makensis 3.09 复现平台差异）
3. ✅ 修复分隔符（commit `23b37c92`）— A/B 红证：旧形复现 CI 错串逐字节、新形编译过；
   desktop `node --test` 57/57 passed
4. ✅ Gate-0 机械面 5/6（唯一红 = coverage，❌ ⊆ 本批代码笔，符合 §8.2第4条派单前形状）
   - 环境准备：worktree 软链 `.venv` + `web/node_modules`（main clone）
   - 冻结 SHA：HEAD=`23b37c92`，tree=`0dff64dd`
5. ⏳ code-review 双轴（对冻结面：workflow + installer.nsh）
6. ⏳ 台账补归属行（d82b3ab8 / 66424156 / 23b37c92 真实审查行 + 23fe62c3 同步合并机械行）
7. ⏳ 最终 gate0 6/6（含台账 commit）
8. ⏳ Windows runner 真跑
9. ⏳ 更新 PR #887

## coverage ❌ 集合（实测，本批代码笔）
- `23b37c92` fix installer.nsh separator
- `23fe62c3` Merge origin/main（先回后正同步合并）
- `66424156` fix installer.nsh parse-time ${__FILEDIR__}
- `d82b3ab8` ci workflow
（覆盖区间 09ca47a1..HEAD，提交总数 3225 / 已审查 3152 / 待判定 73，其中 69 自动归属）

## 上次失败证据
- run 37828964065（50s 红，tip `66424156`）：step 6 makensis 编译失败
  `!include: could not find: "D:\a\...\desktop\installerinstaller-directories.nsh"`，
  `Error in macro customHeader on macroline 3`
- run 37825349697（58s 红，tip `d82b3ab8` 前）：`...\templates\nsis\installer-directories.nsh`

## ⚠ 双轴审查推翻原计划（Spec 轴 P1 实锤，2026-10-09）
**发现**：`origin/main` **已经独立修好同一个 `${__FILEDIR__}` bug**（`e06a4719` #816，经 PR #886
`fix/w21-windows-gate-fixes` 合入），且写法与我们**等价但更正确**：
- 主 main：`!define IA_INSTALLER_DIR "${__FILEDIR__}"` + `!include` 包在 **`!ifndef BUILD_UNINSTALLER` 守卫**里。
- 本分支：同样 `IA_INSTALLER_DIR` + `\` 分隔符，但**丢了 `BUILD_UNINSTALLER` 守卫**。
**两块问题**：
1. **重复劳动**：`66424156` 的 `${__FILEDIR__}` 解析期捕获 = 重造 main 的轮子；`23b37c92` 的 `\`
   分隔符 main 也已含（`"${IA_INSTALLER_DIR}\installer-directories.nsh"`）。整文件 delta 应归零。
2. **P1 回归（已本机复现）**：NSIS `nsis` target 一次同时建 installer + uninstaller。uninstaller 构建
   也插 `customHeader`，无守卫会 include 进 `installer-directories.nsh` 的 install-only 函数 →
   `warning 6010: install function "..." not referenced` → electron-builder 的 warnings-as-errors
   （`build-windows-installer.mjs:171` 佐证）⇒ **uninstaller 构建失败** ⇒ Windows 烟测必红。
   - A/B 实证（`/tmp/885-guard-test`，makensis 3.09 `-WX`）：
     无守卫形 → `warning 6010 ... Error: warning treated as error`；
     有守卫形（main 写法）→ 正常产出 EXE。
   - `installer-directories.nsh:101` 定义 `Function iaPromoteApplication`（install-only），`:118`
     自己也用 `!ifndef BUILD_UNINSTALLER` 包了 `.onGUIEnd` —— 仓内既有惯例即「卸载器须守卫」。
**正确最小动作（拟）**：
1. `git merge origin/main`（同步到最新 `00238569`，它已含 `e06a4719` 的 canonical 修）。
2. `desktop/installer/installer.nsh` **reset 到 origin/main 版本** ⇒ 本 PR 的 installer.nsh delta = 0。
3. 本 PR 只剩 workflow（#885 真正交付物，main 上确实**没有**该 workflow）。
4. 台账：workflow commit 需真实审查行；main 同步 merge 补机械归属行；`installer.nsh` 两笔因
   「最终树 = main 树、无净新增」走零新增内容 / 或并入审查行范围（按 coverage 实测定）。
**为何不硬顶**：任务书「改 workflow/config 面」授权内可做；main 版严格更优，硬留自造版 = 交付带 P1
回归的 PR，违反「P0–P4 全修无例外」。
**已执行（2026-10-09）**：
- ✅ 双轴审查完成（Standards NEEDS-FIX P1:1/P2:3/P3:1/P4:2；Spec FAITHFUL-WITH-FINDINGS P1:1/P2:2/P3:1）。
- ✅ 按 Standards 发现出修 `22149ee3`（删死 glob `installer/**`、加 `actions: write`、精简注释、去投机 `!ifndef`）。
- ✅ `git merge origin/main`（tip `00238569`）→ 冲突仅 `installer.nsh`，取 main 侧（guard 超集）→ 本文件净改动归零。
- ✅ 合并 commit `f5e3f753`。
- ✅ 环境读数：合并树 desktop 官方测试 172/154pass/16fail = pristine origin/main 逐项一致（16 条 Linux 预存在）。

**新 HEAD**：`f5e3f753`（merge `22149ee3` + `00238569`）

## 收敛后精确状态（2026-10-09 复核）
- **合并已提交**：`f5e3f753`（parents `22149ee3` × `002385691e`），工作树干净（仅 checkpoint 未跟踪）。
- **PR #887 净面 vs `origin/main` = 恰 1 文件**：`.github/workflows/windows-installer-smoke.yml`（+130，新增）。
  `git diff origin/main..HEAD --stat` 只这一行；`installer.nsh` 与 origin/main **逐字节相同**（净 delta=0）。
- **coverage 实测 ❌ = 恰 3 笔**（跑 `check_review_coverage.py` 全量）：
  - `66424156` fix installer.nsh parse-time（代码笔，最终树归零但历史笔未覆盖）
  - `d82b3ab8` ci workflow（**本 PR 真正交付物**，需真实审查行）
  - `f5e3f753` 同步 merge（树 ≠ 两父 ⇒ 非零新增 ⇒ 需机械归属行）
  - **已不再报**：`23b37c92`、`23fe62c3`（合并后其 installer.nsh delta 对某父树逐字节相等 ⇒ 零新增/自动归属）。
- **环境读数**：合并树 desktop 官方测试 172/154pass/16fail = pristine origin/main 逐项一致（Linux 预存在，非本 PR 所致）。

**待用户裁决项（停下报告）**：范围已从「聚焦 CI PR」扩为「同步 main（164 文件）+ CI」。上面复核证明：**同步后本 PR 净面已收敛回单 workflow 文件**，installer.nsh 归零，实际交付面未膨胀。是否按此路线继续（补 3 笔台账 → gate0 6/6 → 推 → Windows 真跑 → 更新 PR）？推荐：是。

---

## 接续会话（第 2 会话，2026-10-09）进度

**裁决已下（协调员）**：走路线 A，按现状交付，净交付面=单 workflow 文件；照做不重问。

### 阶段 0：读规则 + 五项启动检查表 ✅
- 已完整读：`~/workspace/system/agent-workflow-prompt.md`（全文 100 行）、仓库 `AGENTS.md`（全文 546 行，§3/§4.4/§6/§8/§9/§14/§16）、`CLAUDE.md`、Issue #885 全文、任务书、`docs/SDD_WORKFLOW_PROTOCOL.md` §7（集成闸门/coverage）/§8.1（冻结树单次全量）/§8.2（派单前 Gate-0）/§8.3（审查预算）/§8.8（失败回退边界）。
- 五项启动检查表：见文件上部（Vision/规格/Reuse/Phase/触发细则，均 READY，理由见下）。

### 阶段 1：台账 ✅（覆盖闸门 RC=0）
- **实测：清空 885 台账后 coverage ❌ = 6 笔**（非协调员说的 3 笔）：`d82b3ab8` / `23fe62c3` / `66424156` / `23b37c92` / `22149ee3` / `f5e3f753`。
- **落 4 行**（协议要求「审查线 vs 机械线」分离，故比照实际笔数需 4 行才能既覆盖 6 笔又保持 claim 分离）：
  1. `885-win-smoke-ci-workflow-2862455d-d82b3ab8.tsv` — d82b3ab8 workflow **真实审查行**（任务第 1 行）
  2. `885-win-smoke-ci-sync-merge-d82b3ab8-23fe62c3.tsv` — 23fe62c3 同步 merge **机械归属行**（任务未点名但必须）
  3. `885-win-smoke-ci-installer-nsh-23fe62c3-22149ee3.tsv` — 66424156+23b37c92+22149ee3 **真实审查行**，含任务要求的 66424156 明示结论（"已被 main 侧 canonical 修复替代，终树零新增，审查通过"）（任务第 2 行）
  4. `885-win-smoke-ci-sync-merge-22149ee3-f5e3f753.tsv` — f5e3f753 同步 merge **机械归属行**（任务第 3 行）
- 全部 BOM=True、3 字段、行长 < 800。coverage `RC=0 ✅`（3322 提交 / 已审查 3274 / 待判定 48）。
- **事实核验**：`git diff origin/main..HEAD -- desktop/installer/installer.nsh` 为空（逐字节相同）；HEAD installer.nsh = main 的 `e06a4719` canonical 版。
- **偏离说明**：协调员说「共 3 行」，实测需 4 行（6 ❌ 笔 + claim 分离）。已按事实落 4 行，覆盖闸门绿。此为记录性修正，不扩 scope。
