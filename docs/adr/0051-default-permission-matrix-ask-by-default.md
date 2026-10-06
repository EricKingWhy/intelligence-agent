# ADR-0051 — 默认权限矩阵（workspace-write + ask）与审批层路径规则：ADR-0027 D2 的 supersession

- **Status**: Accepted（用户 2026-10-06 裁决，#358 评论：路线 A「诚实版」先行、default
  矩阵定稿、路线 B 单独立项 #729）
- **Date**: 2026-10-06
- **Deciders**: 用户（#358 裁决）+ 本 Agent（实现口径）
- **Supersedes**:
  - **ADR-0027 的 D2**——默认权限档从 `workspace-write + auto-approve` 改为
    `workspace-write` + **ask**（读免问、写/Bash 逐次问）。
  - **ADR-0027 的 Security「明确不新增的防护：写路径白名单 / 目录级 deny 清单」**条款——
    引入目录/命令级审批规则，但**诚实标注为审批层，不是 OS 隔离**（见 D2）。
- **Refines**: ADR-0027 的 Context「权限档从来不是路径围墙」事实修正**予以保留并加注**：
  本 ADR 新增的路径规则同样是审批路由，不改变"policy 不做路径校验"这一事实。
- **Related**:
  - 票面 **#358**（W-14）；**#729**（路线 B：真 OS 隔离，单独立项）
  - **ADR-0041**（会话内改档 `permission/changed`）；**ADR-0050**（D4 把 SB-04 / MCP-F5
    "web 默认 auto-approve 放行 DANGER bash" 的权限默认问题移交 #358）
  - Reuse 判定：`goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/13_OPEN_SOURCE_REUSE_MATRIX.md`
    （Agent Harness 领域）；两步调研 `~/workspace/issue-358-study/codebuddy-pi-dsh.md`
    （Pi + DeepSeek Harness）与 `~/workspace/issue-358-study/claudecode-cline.md`
    （Claude Code + Cline）
  - 代码落点：`agent_harness/tooling/permission_rules.py`（规则引擎本体）、
    `agent_harness/tooling/executor.py::_check_approval`（接入）、
    `agent_harness/assembly.py::build_runtime`（composition root 注入）、
    `agent_harness/session/service.py`（D2 默认 + 迁移）、
    `agent_harness/session/approval.py`（常量 + 迁移写入口）、
    `agent_harness/web/app.py`（请求默认值）
  - 一手来源（2026-10-03/04 核，读法见调研报告）：
    - **S1** https://code.claude.com/docs/en/permissions —— 三层 `allow/ask/deny`、
      求值顺序 **deny → ask → allow**（first match）、**deny 永远赢**（跨层 union）、
      默认"工作区内读免问、写/Bash 逐次问"
    - **S3** https://code.claude.com/docs/en/sandboxing —— "On native Windows, Claude
      Code runs commands unsandboxed"（Windows 原生无 OS 沙箱，答案是 WSL2）
    - **DSH** DeepSeek Harness `cordis.patch.yml:232`（默认 `workspace-write`）/
      `:248`（`ask`/`never` 双轴），commit `5badb150`
    - **Pi** `docs/security.md:19`（"it does not prevent commands from accessing other
      paths"——目录不是边界），commit `28dcce2`

---

## 1. Context

### 改动前的默认（被 supersede 的对象）

- 默认 = `workspace-write + auto-approve`：`SessionService.create_and_launch` 与
  `web.CreateSessionRequest` 两处默认 `auto_approve=True`；未选档位的会话在
  `build_runtime` 里拿到全自动批准回调。**读/写/Bash 全部免审批**。
- `WORKSPACE_WRITE` policy 是**工具级**授权闸（`tooling/approval.py::needs_approval`），
  **不做任何路径校验**（ADR-0027 已确认）。无路径规则、无破坏性命令分类。
- 文件工具越界靠 `sandbox/base.py::resolve_within_workspace` 的硬围墙（`Path.resolve()`
  + `is_relative_to`）；bash **无任何**命令/路径分类（`permission=DANGER` 一刀切）。

### 问题

除 Pi 外的三家成熟产品默认都比本仓严格（Claude Code 写/Bash 逐次问；Cline 官方推荐
只开"读工作区"；DSH `workspace-write + ask`）。本仓"全自动批准 + 无审批矩阵"等于把
"每一步问你"的承诺默认关闭——与 #549 SB-04 / MCP-F5 同一根因（ADR-0050 D4 已移交本票）。

### 两条路线（用户 2026-10-06 裁决）

- **路线 A（诚实版）——本 ADR**：抄 Claude Code 在 Windows 原生的做法：权限规则 + 审批
  做到位，**明确不宣称 OS 隔离**。
- **路线 B（真墙版）——另立项 #729**：抄 DSH 的 Windows restricted-token + ACL、Linux
  bwrap/Landlock，fail-closed。
- **不能选（四家一致证伪）**：字符串黑名单当安全边界。Pi 文档亲口承认拦不住路径访问；
  DSH 宁可 fail-closed 也不做字符串过滤。

## 2. Decision

### D1 — 默认矩阵翻转（supersede ADR-0027 D2）

默认从 `workspace-write + auto-approve` 改为 **`workspace-write` + `ask`**（读免问、
写/Bash 逐次问）。

- `SessionService.create_and_launch`：`auto_approve` 默认 `True → False`；审批路由判据加
  第三支"两字段都未声明 → interactive（ask）"。
- `web.CreateSessionRequest.auto_approve` 默认 `True → False`。
- **旧参数兼容（不暗选）**：显式 `auto_approve=true` 仍走 auto-approve（向后兼容）；
  显式 `auto_approve=false` 或显式选档位仍走 interactive（#423）；`danger-full-access`
  仍是"无需审批"档。三支语义与创建 / 续聊两条路径同一判据。

### D2 — 路径规则是审批层，**不是** OS 隔离（保留 ADR-0027 的核心条款）

本 ADR 新增的"工作区外路径 → ask""破坏性命令 → deny"是**审批层路由**，不是内核边界：

- deny 只**拒绝执行**（返回 `PERMISSION_DENIED`），**不阻断**进程访问；
- 即使用户批准越界路径，`sandbox` 的路径围墙**仍会独立拒绝**（审批 ≠ 越墙）；
- 破坏性命令分类器**已知可绕过**（见 Honesty）。

ADR-0027 的"policy 不做路径校验"事实修正**依然成立**：规则引擎是新的一层，它读路径只
为路由"要不要问人 / 要不要拒"，不改写 policy 语义。

### D3 — default 矩阵（R1–R6，`default_rule_set()`）

| # | 工具面 | 条件 | 裁决 | 抄谁 |
|---|---|---|---|---|
| R1 | `bash` | 破坏性命令（见 D3.1） | **DENY**（不可被 allow 覆盖） | Claude Code deny 永远赢（S1）+ DECISION-BRIEF §三② |
| R2 | 文件工具（带 `path`） | path 解析到工作区外 | ASK + 越界提示 | DSH workspace-write 语义 |
| R3 | `bash` | 只读子集（`ls`/`cat`/`grep`/… 白名单，无 shell 连接符） | ALLOW | Claude Code 内置只读命令集（S1） |
| R4 | `bash` | 其余 | ASK | Claude Code（S1）+ DSH ask |
| R5 | 编辑类（`write`/`edit`/`apply_patch`） | 工作区内 | ASK（per-call，不默许整个 session） | Claude Code 写/编辑需问（S1） |
| R6 | 读类（`read`/`glob`/`grep`） | 工作区内 | ALLOW | Claude Code 工作区内读免问（S1） |

- 求值顺序 **deny → ask → allow**（first match 胜出；deny 永远赢）。未命中任何规则 →
  `NO_MATCH` → 回落既有 `needs_approval(tool_permission, policy)`（**语义不变**）。
- R2 的越界判定**复用** `resolve_within_workspace` 的同款语义（`Path.resolve()` +
  `is_relative_to`），**不手写字符串前缀比对**（那是 theater）。
- **规则引擎只在非 `danger-full-access` 档生效**：danger 是用户**显式**的"无需审批"档
  （见 D1；创建路径的 interactive 判据同样排除它）。规则引擎服务的是 workspace-write /
  read-only 下的默认矩阵，不把一个显式选择降级成不可用。
- **机制默认关闭**：`ToolExecutor(permission_rules=None)` = 不启用规则引擎（沿用既有
  `needs_approval` 语义，逐字不变）。产品级默认矩阵由 **composition root**
  （`assembly.build_runtime` 的两处 `ToolExecutor`：主执行器 + 子执行器工厂）显式注入
  `default_rule_set()`。裸构造（兜底 / 单测 / READ_ONLY 端点）**不启用**。

#### D3.1 破坏性命令分类（审批层启发式，**不是安全边界**）

`shlex.split(command, posix=True)` → 首 token 小写归一（**不剥离** `sudo` 包装）：

- **DENY**：`sudo`/`doas`/`runas`/`su`（提权）；`rm` 且参数含递归旗（`-r`/`-R`/
  `--recursive`，含 `-rf` 等合并短旗）；`mkfs`/`format`/`dd`/`diskpart` 或以 `mkfs.` 开头。
- **ALLOW**：首 token ∈ 只读白名单 **且** 命令不含 shell 连接符
  （`;` `&&` `||` `|` `$(` 反引号 `>` `<` `&`）。`git` 不在白名单（子命令读写难分）。
- **ASK**：其余（含解析失败 / 空命令——保守）。

分类器是**审批路由**启发式；真隔离靠 Docker sandbox 或路线 B（#729）。

### D4 — 旧 Session 迁移（一事一提示）

已有会话（`session/started` 无 `permission_defaults_version=2`、无 `permission/changed`、
未显式声明档位/auto_approve）续聊时：

- **沿用旧默认**（`workspace-write + auto-approve`，**永久**，不强制翻成 ask）——
  迁移只加提示与显式化，不改写用户的历史选择；
- **一次性提示**（三面：`logger.warning` + `LaunchResult.warnings` +
  落一条 `permission/changed` 迁移事件 `permission_migration="legacy-v1-defaults"`，
  文案："此会话创建于默认权限收紧（#358）之前，沿用旧默认 workspace-write + 自动批准。
  新会话默认为 workspace-write + 逐次询问。可在会话内改档切换。"）；
- 迁移事件同时是"已提示"标记——二次续聊幂等，不再提示；
- **新会话** `session/started` **恒写** `permission_defaults_version=2`，续聊按新默认（ask）。
- 迁移标记判据取"当前生效的那条 `permission/changed`"（最后一条）：迁移后用户再显式
  改档（ADR-0041）时，后写的 changed 胜出，不被旧默认覆盖。

### D5 — 明确**不做**

- **不**做字符串黑名单当安全边界（四家证伪；本 ADR 的破坏性分类器只做审批路由 + 本文
  Honesty 列出绕过）；
- **不**做 `APPROVE_SESSION` 会话级免问语义扩展（票面要求逐 Tool Call，不默许整个
  session；本次不动既有 APPROVE_SESSION 实现）；
- **不**改 ADR-0027 的 D1（cwd 任意目录）、D3（旧契约并存）、D4（存量会话不迁移归组）；
- **不**承诺 Windows 原生 OS 级 shell 沙箱（见 Honesty）。

## 3. Honesty（诚实边界）

### 破坏性启发式的已知绕过（DENY 不是 containment）

- `sh -c 'rm -rf /'`（首 token 是 `sh`，不是 `rm`）；
- 变量拼接 / 命令替换：`X=rm; $X -rf /`、`$(printf rm) -rf /`；
- 全路径：`/bin/rm -rf /`（首 token 不是裸 `rm`）；
- 间接执行：`base64 -d | sh`、`python -c 'import shutil; ...'`、`eval ...`；
- 管道 / 重定向里藏破坏性命令（连接符使其归入"其余" → ASK，仍可被人工批准）。

### Windows 原生无 OS 沙箱

抄 Claude Code S3 官方原文立场："On native Windows, Claude Code runs commands
**unsandboxed**."。本 ADR 的规则 + 审批**不提供进程级隔离**——真正的 OS 隔离靠
DockerSandbox（`05 §5`）或路线 B（#729）。任何"规则引擎 = 隔离"的表述都是误用。

## 4. Consequences

- 新会话默认"读免问、写/Bash 逐次问"；`danger-full-access` 与显式 `auto_approve=true`
  仍是显式 opt-out。旧会话行为**逐字不变**（只是多了一次性提示）。
- 规则引擎接在 `_check_approval` 的**最前**（非 danger 档）：`DENY → PERMISSION_DENIED`
  （不调 callback）、`ALLOW → 放行`、`ASK → 既有审批流但 reason 用规则文案`、
  `NO_MATCH → 既有 `needs_approval` 逐字保留`。
- 工具执行路径**不变**：仍走 `Contract → Registry → Validation → Permission →
  Scheduler → ToolExecutor → Ledger → ToolResult → SessionEvent`（不变量 §7.1），规则引擎
  是 Permission 阶段的一个前置判定，不新开隐藏路径。
- `LaunchResult` 新增 `warnings: list[str]`（向后兼容的带默认值字段）；Web 层暂无现成
  warnings 透传通道，字段先留给调用方，不为它新造通道。
- 事件 / 账本 / 恢复语义零改动：本 ADR 不触碰 Operation Ledger、Reconcile 合同
  （ADR-0047 / #547）。
