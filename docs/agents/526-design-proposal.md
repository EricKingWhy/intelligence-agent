# #526 设计方案：审批决策缓存 + Plan 模式

> **状态：DESIGN PROPOSAL —— 待用户裁决，未施工。** 本文只是设计提案，不含实现，也未改动票面。
> 两项都涉及规格合同扩大，按 `AGENTS §9.1.1` 先停在分析/报告阶段；只有用户明确批准后，
> 才能转 Ticket、定 AC 并施工。未确定项一律写「需用户裁决 / 需核实」，不代拍。

## 1. 背景与问题

issue #526（IMP-09/16）指出两个缺口：

1. **审批无决策缓存**：审批只有 per-call 语义（每次 `execute` 独立检查、不存已批准状态），同类操作反复弹审批。issue 的诉求是「类似工具自动批准 / 允许本会话」。
2. **缺 Plan 模式**：无法让 Agent 先产出只读计划、确认后再执行。

仓库审计（`docs/research/2026-10-03-open-issues-audit.md:149`）已裁决方向：「类似工具自动批准」属**权限合同扩大**而非 UX 修复；Plan 只读执行**必须与平台 Sandbox 独立界定**，**Windows 不能照搬 Linux 隔离**。本设计只给两轴的选项、代价与推荐，供裁决。

## 2. 事实依据

以下均为上一轮实读代码/规格的行号（本仓相对路径）。

| # | 事实 | 依据 |
| --- | --- | --- |
| F1 | `PermissionDecision` 有 deny/approve_once/approve_session/approve_policy 四值；runtime **只兑现前两者**，后两者注解为「留后续批次（需要 session 级授权缓存）」 | `src/agent_harness/tooling/approval.py:21-31` |
| F2 | `ApprovalResponse` 不传 decision 时只推导 approve_once/deny | `src/agent_harness/tooling/approval.py:64-70` |
| F3 | 交互式 callback 落到 `tool/approval-requested` 的 `allowed_decisions` **只暴露 deny / approve_once**，UI 合同层就没有 session/policy 选项 | `src/agent_harness/session/approval.py:81-84` |
| F4 | `approve_session` / `approve_policy` 目前只是 durable 读取侧的「放行决策族」，没有写侧实现 | `src/agent_harness/session/approval.py:365-372` |
| F5 | per-call scoping 由设计保证：每次 execute 独立检查、不存「已批准」状态；批准只对本次 execute 生效 | `src/agent_harness/tooling/executor.py:413-422, 1093-1096` |
| F6 | 无审批回调 → 默认拒绝（安全默认，绝不静默放行） | `src/agent_harness/tooling/executor.py:1081-1091` |
| F7 | 审批门在 validate 之后、execute 之前；`_check_approval` 是唯一审批关卡 | `src/agent_harness/tooling/executor.py:413-422, 1051-1106` |
| F8 | `needs_approval` 只按 `ToolPermission × PermissionPolicy` 层级判定，不看工具名/文本 | `src/agent_harness/tooling/approval.py:80-104` |
| F9 | 审批队列是进程内内存 dict（`_pending` / `_resolved`）；durable 版是 SessionEvent（`tool/approval-requested` / `permission/resolved`） | `src/agent_harness/tooling/approval_queue.py:45-53`；`src/agent_harness/session/approval.py:85-100, 153-169` |
| F10 | resolve 时**先持久化决策，再唤醒**（HTTP 200 不早于落盘） | `src/agent_harness/tooling/approval_queue.py:96-103` |
| F11 | 权限策略是 **per-run 快照**（`ToolExecutor` 绑 `self._policy`），改档下一轮 run 生效 | `docs/adr/0041-session-permission-mode-mutability.md` §1.2 |
| F12 | ADR-0041 已实现：`permission/changed` 事件、declared vs effective 派生、下一轮生效、**有 pending 审批则 409 禁改档**（D5）、declared/effective/policy_at_approval **三概念分离**（D6） | `docs/adr/0041-...md` §2 D1-D8 |
| F13 | Sandbox 仅 local / docker 两档；**local 无进程级隔离、无内存/CPU/网络配额**（已声明边界） | `src/agent_harness/sandbox/local.py:8-15` |
| F14 | DockerSandbox 只挂 volume（rw），**未设 `--network` / `--memory` / `--cpus` / `--pids-limit` / `--read-only`** | `src/agent_harness/sandbox/docker.py:128-140` |
| F15 | Windows 路径已用 **Job Object**（`KILL_ON_JOB_CLOSE`）兜底整树终止 | `src/agent_harness/sandbox/local.py:372-425` |
| F16 | `AppState` 固定 `backend="local"`（Docker 档没有从 web 装配入口） | `src/agent_harness/web/app.py:870` |
| F17 | 无 Plan 模式概念；`session/plan.py` 是**进度清单**（W-26/#380），与「计划执行模式」无关 | `src/agent_harness/session/plan.py:1-19` |
| F18 | `update_plan` 工具本身是 `MUTATING` + `WORKSPACE_WRITE`（会话内非破坏性持久写） | `src/agent_harness/tools/update_plan.py:100-107` |
| F19 | `bash` 是单个 `DANGER` 级工具，覆盖所有命令（无法按工具名区分只读/写命令） | `src/agent_harness/tools/bash.py:139-144` |
| F20 | `PermissionPolicy` 冻结三层：read-only ≤ workspace-write ≤ danger-full-access | `src/agent_harness/tooling/contract.py:94-104` |
| F21 | 规格：Approval 应是**单次 Tool Call 授权，不默认永久升级 Session 权限** | `SPEC_ROOT/04_TOOL_RUNTIME.md:170` |
| F22 | 规格：高风险操作 Approval 只针对本次 Tool Call，不因一次批准永久放开后续命令 | `SPEC_ROOT/05_SANDBOX_CODING_TOOLS.md:131-142` |
| F23 | 不变量：「Sandbox 是 Runtime 安全边界，不是 Prompt」 | `SPEC_ROOT/05_SANDBOX_CODING_TOOLS.md:5`；`AGENTS §7` 不变量 #11 |
| F24 | Reuse Matrix：Sandbox policy 判 **PORT DESIGN**（read-only/workspace-write/approval） | `SPEC_ROOT/13_OPEN_SOURCE_REUSE_MATRIX.md:61` |
| F25 | 审计：#526 方向——拒绝「依赖 prompt、相似文本或字符串命令过滤做 Runtime 边界」；#561 不依赖本新功能 | `docs/research/2026-10-03-open-issues-audit.md:108, 128, 149` |
| F26 | #358（W-14）：破坏性操作逐 Tool Call 范围，不默许整个 Session；高风险权限**不因 Fork 静默扩大** | `AGENTS` §16 引用；审计 `:108` |
| F27 | local 不是路径硬围墙（ADR-0027）；RLIMIT 是 POSIX 进程限制、非 Windows 全树隔离；Docker `--init` 只 reap 僵尸 | `docs/research/2026-10-03-open-issues-audit.md:140` |
| F28 | #521：before 可拦截、after 已执行仅反馈；**改写参数后重新 Validation/Permission/Scheduler，不让 allow hook 提升权限** | `docs/research/2026-10-03-open-issues-audit.md:154` |

## 3. 方案依据（§6.1 风格）

来源分两类：**(A) 成熟产品一手依据**（2026-10-05 已核实：ZCode 执行模式与权限文案、Codex 审批协议、Claude Code 权限规则与 modes 文档）；**(B) 本仓已冻结规格/合同**（实读，行号见 §2）。

| 来源 | 机制 | 契约点映射（我们怎么用 / 边界） |
| --- | --- | --- |
| ZCode（已核实 2026-10-05） | 4 执行模式 + 权限决策 Allow / Allow for session or project / Reject / Always Reject；命令作用域 exactCommand（仅此命令）/ commandPrefix | 「Allow for session」→ 兑现 `approve_session`（会话级授权是成熟形态）；exactCommand → 批准键用**精确命令串**而非相似文本；Allow for project / Always → `approve_policy`，建议**拆票另议** |
| Codex（已核实 2026-10-05） | 命令审批 accept / **acceptForSession** / acceptWithExecpolicyAmendment（持久规则）/ decline / cancel；文件变更 accept / acceptForSession / decline / cancel | 会话级接受 vs 持久规则是两回事——正好对应 approve_session（本票）vs approve_policy（拆票）的切分 |
| Claude Code（已核实 2026-10-05） | 权限规则 `Bash(npm run build)` 精确匹配 vs `Bash(npm run *)` 前缀匹配；复合命令每个子命令须单独匹配；permission modes 含 **plan（Read-only, no modifications）**，Shift+Tab 切换；Sandbox 与 Permissions 正交（defense-in-depth） | 精确匹配 → 禁用相似文本做批准键；**plan mode 是现成的只读工作流档**（B1 的直接先例）；Sandbox 独立于权限 → Plan 与 Sandbox 后端正交 |
| DeepSeek Harness（Reuse Matrix F24） | read-only / workspace-write / approval 分层 | PORT DESIGN：Policy 只做**上限**，审批是独立关卡；本设计不新增 Policy 档位 |
| Cline（`docs/agents/reference-sources.md:44`） | 计划-执行分离、checkpoint 与回滚 | Plan 作为独立**工作流档**的先例；我们只取「先计划后执行」的档位切分，不引入其 checkpoint 机制 |
| 本仓 Spec04 §8（F21） | Approval 是单次 Tool Call 授权，不默认永久升级 Session | **硬约束**：任何缓存都只能是显式、窄域、可撤回的授权，不能把逐调用默认改成自动放行 |
| 本仓 Spec05 §6（F22）/ 不变量 #11（F23） | Approval 只针对本次调用；Sandbox 是 Runtime 边界不是 Prompt | Plan 的只读必须由 **Runtime 门禁**兑现，不能靠 prompt；Plan 与 Sandbox 后端正交 |
| 本仓 ADR-0041 D5/D6（F12） | pending 禁改档；declared / effective / policy_at_approval 三分离 | 授权缓存必须绑定 `policy_at_approval`，改档后失效；有 pending 审批时禁改档的既有规则保持 |
| Windows 平台能力（**需核实**） | Job Objects（内存/CPU 限额）、AppContainer（能力隔离）、ACL/icacls（路径）、WFP/防火墙（网络） | Windows 侧可达的隔离面；**不能**照搬 Linux 的 rlimit/seccomp/namespaces/cgroup（F27） |

## 4. 待裁决 (a)：审批决策缓存

### 4.1 选项与代价

- **A1（维持现状，不缓存）**：保持逐调用审批、不存任何已批准状态。代价：issue 的审批噪音诉求不解决，UX 无改善；但不动任何合同，零风险。
- **A2（推荐）：会话内、按「精确操作身份」的显式授权**（兑现 `approve_session`）。代价：需新增授权事件 + 派生 + 撤回路径，审批 UI 增加「本会话允许此操作」选项，且批准键必须由 Runtime 从**已校验参数**派生（工程量与合同面都在这里）。
- **A3：跨会话的持久白名单 / 「相似工具」自动批准**（`approve_policy` 或文本相似）。代价：把一次性授权升格为**长期、跨会话**的权限扩张，攻击面最大，且与 F21/F22 直接冲突（默认逐调用审批不得被相似性绕过）。

### 4.2 推荐 A2，为什么是这个

`SPEC_ROOT/04_TOOL_RUNTIME.md:170` 规定 Approval 是**单次 Tool Call 授权、不默认永久升级 Session 权限**（F21）；`05_SANDBOX_CODING_TOOLS.md:131-142` 规定不因一次批准永久放开后续命令（F22）。这两条**不禁止**显式授权，但**禁止**把「逐调用默认」替换成「按相似性自动放行」。因此唯一在合同上成立的缓存形态是：**用户明确表达、仅限本会话、绑定精确操作身份、有 TTL、可撤回、每次调用仍重过 `needs_approval` 与 `policy_at_approval` 的授权**——这正是 A2。A2 也正好兑现已预留的 `approve_session`（F1/F4）。A3 不满足「精确身份 + 会话隔离 + 可撤回 + 不绕过 policy」中的多条，故否。A1 不作推荐只是因为它是票面要解决的现状，而非它不安全。

### 4.3 批准键设计：为什么不能用工具名 / 相似文本

- **不能用工具名**：`bash` 是单个 `DANGER` 工具、覆盖所有命令（F19），批准 `tool_name="bash"` 等于批准全部命令，是粗粒度无差别放行；以工具名为键等于跳过 Spec04 §8 的逐调用语义。
- **不能用「相似文本」/前缀/正则/字符串命令过滤**：命令文本由**模型/外部输入**控制、可被注入构造，审计已明确「不要依赖 prompt、相似文本或字符串命令过滤做 Runtime 边界」（F25）；`ls` 与 `ls; rm -rf` 的相似度无确定性，任何相似匹配都可被绕过。
- **用什么代替**：由 **Tool Contract 声明的结构化 scope 描述符**，Runtime 从 **post-validation 的规范化参数**（`args_schema.model_validate` 之后、即 `executor.py:413` 之前的合法入参）派生 **canonical 批准键**：
  - `command` = 规范化后的**完整命令**（精确串，绝不做前缀/glob/相似）；
  - `path` = `resolve_within_workspace` 解析后的**workspace 相对路径**（越界在成键前就被拒，path traversal 无法搭车）；
  - `server` = MCP **server 身份**（name + endpoint/transport）+ 远端 tool/method，而非本地工具名；
  - `resource` = 复用既有 `resource_keys` / metadata（`SPEC_ROOT/04 §7`，`contract.py`）；
  - `version` = 规范化参数的 **hash**（+ 工具/合同版本），任一参数变化即变键。
- **键必须由 Runtime 生成，模型不得提交键**（否则模型可自造键命中授权）。这与 Scheduler 的「显式 depends_on / resource_keys，不用 LLM 自由文本猜 DAG」是同一条工程纪律。

### 4.4 scope 界定

| 维度 | 取值方式 | 边界 |
| --- | --- | --- |
| command | 规范化完整命令串 | **精确**；不做前缀/通配/相似；变化即重新审批 |
| path | `resolve_within_workspace` 后相对路径 | 越界解析先于成键被拒；目录级授权是更大的决定 → 需用户裁决 |
| server | MCP server 身份 + 远端 tool/method | 不能只按本地 MCP 工具名 |
| resource | `resource_keys` / metadata | 复用既有合同，不新造资源模型 |
| version | 规范化参数 hash（+ 工具/合同版本） | 参数或工具定义变更 ⇒ 授权自动失效 |

### 4.5 TTL 与 revoke

- **TTL 必须有**；上限建议 = `min(会话生命周期, 墙钟上限 N)`，到期回落逐调用审批。**N 的具体值需用户裁决。**
- **revoke**：用户显式撤回；撤回必须持久化并立即使授权失效、清掉相关 pending。
- **表示**：走 append-only SessionEvent（不变量 #3 / #22：事件流是唯一真相），内存索引只是投影，不入 SessionEvent 的内存队列不作为真相（F9）。**具体事件形状、是否新增 `EVENT_TYPE`（触发 `scripts/gen_event_types.py` 生成物）需用户裁决。**

### 4.6 跨 Session 隔离

- 授权以 `session_id` 为界，**绝不跨会话泄漏**。
- **Fork**：推荐 **不继承授权**。理由：#358（W-14）明确高风险权限不因 Fork 静默扩大（F26）；ADR-0041 D7 只规定继承 **effective 档**，不等于继承**细粒度、风险特定的授权**。**是否继承需用户裁决。**

### 4.7 二次校验（参数变化重验 / policy 绑定）

每次调用在审批门（F7）都按下列顺序重验，全部通过才可用授权替代人工决策：

1. 重算 canonical 键；与授权键**不一致 ⇒ 不覆盖**，回到逐调用审批。
2. 授权仅替代「人的决定」，**永不绕过 `needs_approval`**（F8）：仍按**当前** policy 判是否需要审批。
3. 绑定 `policy_at_approval`（ADR-0041 D6，F12）：当前 policy ≠ 授权时 policy ⇒ 授权失效（改档后必须重批）。
4. **权限级别上界**：低级别授权（如 WORKSPACE_WRITE）不能覆盖更高级别（DANGER）的调用。
5. 保持 ADR-0041 D5（有 pending 审批时 409 禁改档）。

### 4.8 合同影响与拆票建议

- `allowed_decisions` 目前只暴露 deny/approve_once（F3），启用 `approve_session` 需要**同时**改：审批 UI 合同 + `tool/approval-requested` 事件 + `permission/resolved` 派生 + 授权撤回路径。属合同扩大，**需先写 ADR 并经用户批准**。
- **`approve_policy` 建议本票不做**，单开票并先定 scope（是否 project 级持久、如何版本绑定、如何审计）。票面 `approve_session` 与 `approve_policy` 是两种风险量级，不应一次合并。

## 5. 待裁决 (b)：Plan 模式

### 5.1 选项与代价

- **B1（推荐）：Plan 作为会话级「工作流档」**，Runtime 在统一 Tool 管线里做门禁（只读执行），与 Sandbox 后端正交。代价：需在 Tool 管线加一个门禁 + 一个新会话档 + UI 切换，并要显式定义会话本地工具（如 `update_plan`）的豁免。
- **B2：把 Plan 做成 `PermissionPolicy` 的新权限档**（枚举第 4 值或「低于 read-only」）。代价：打破冻结的三层枚举与 `read-only ≤ workspace-write ≤ danger-full-access` 层序（F20），牵连 `PERMISSION_MODE_DESCRIPTIONS`、#282/#283 派生与前端，并把「工作流」与「信任边界」混为一谈。
- **B3：Plan 只靠 prompt/指令约束模型**（system prompt 告诉它别写）。代价：prompt 不是 Runtime 边界（F23；不变量 #11），可被注入绕过，等于没有 Plan。

### 5.2 推荐 B1，为什么是这个

`SPEC_ROOT/05_SANDBOX_CODING_TOOLS.md:5` 与不变量 #11 要求安全边界必须落在 **Runtime**（F23），故 B3 直接否。`PermissionPolicy` 的定义是「Agent 在这个 Session 里的**最大权限边界**」（`contract.py:95`，F20），是**上限**；而 Plan 是「本轮意图/工作流」——它**收窄**可提供的工具，不是新的信任边界。两者**正交**，把 Plan 塞进 policy 枚举（B2）会破坏冻结层序并制造「工作流=权限」的错误耦合，故否。B1 同时对齐成熟产品的「独立 Plan mode」（ZCode，票面引用）与 Cline 的「计划-执行分离」（reference-sources:44）。

### 5.3 Plan 只读执行语义（禁什么 / 怎么判）

- **按 Tool Contract 判定**，不看工具名、不解析命令文本：禁 `side_effect == MUTATING` 与 `permission > READ_ONLY` 的工具。
- **`bash` 必须整体禁**：它是单个 `DANGER` 工具（F19），无法用「只读命令白名单」安全细分——字符串过滤被 F25/§4.3 否定。
- **`update_plan` 豁免问题**：它现在是 `MUTATING` + `WORKSPACE_WRITE`（F18），按上面的规则会被 Plan 门禁禁掉，导致 Plan 无法发布计划。推荐给「仅写会话状态、无外部资源副作用」的工具一个**显式 Contract 标记**来豁免门禁，**不要**按工具名硬编码。**是否豁免需用户裁决。**

### 5.4 计划输出形态

- 复用现有进度清单载体：`task/plan_updated` / `update_plan`（`session/plan.py`，F17），Plan 阶段产出的就是这份结构化清单（pending/in_progress/completed），前端按 PRD D9 渲染。**不新增第二套计划格式。**
- Plan → 执行的切换：用户确认计划后退出 Plan 档，**下一轮 run 生效**——与 ADR-0041 D4 的 per-run 快照同构（F11）。

### 5.5 Sandbox 与 Plan 独立界定

- **Plan = 权限/工作流门禁，OS 无关，在 Runtime；Sandbox = 执行隔离后端（local/docker），OS 相关。** 两者正交。
- Plan 不因 Sandbox 后端不同而改变语义；但 **local 不是硬围墙**（F13/F27；ADR-0027），所以 Plan 在 local 上只能承诺「Runtime 门禁」，**不能**承诺「进程级只读」。
- **不要用 `READ_ONLY` 档冒充 Plan**：`READ_ONLY` 是用户可选的正常上限档；Plan 额外禁 `bash`、产出计划、可切换。二者重叠但不相等。

### 5.6 Linux 可用机制 vs Windows 可用机制

| | Linux | Windows |
| --- | --- | --- |
| 已用 | POSIX `setsid` 进程组 + `killpg` 整树击杀 | **Job Object** `KILL_ON_JOB_CLOSE`（F15，`local.py:372-425`） |
| 资源配额 | `RLIMIT_*`、cgroup v2；Docker `--memory/--cpus/--pids-limit`（当前**未启用**，F14） | Job Object 可扩展 `PROCESS_MEMORY` / `JOB_MEMORY` / CPU rate control（**需核实**） |
| 文件/路径 | mount namespace、`chmod` 模式、bubblewrap/firejail | AppContainer（能力隔离，需 manifest/低完整性）、ACL / `icacls`（**需核实**） |
| 网络 | network namespace、seccomp、Docker `--network none` | WFP / Windows 防火墙（繁琐，**需核实**） |
| 系统调用过滤 | seccomp | **无等价物** |

**Windows 不能照搬的（必须明确）：**
- Linux 的 `rlimit` / `seccomp` / namespaces / mount / cgroup 语义（F27）；
- POSIX `setsid` 进程组（Windows 用 Job Object 替代）；
- `/proc` 杀树（`DockerSandbox` 用 `/proc/$pid/task/$pid/children`，`docker.py:634`）——**DockerSandbox 现依赖 `/bin/sh -lc` 与 `/proc`，本身是 Linux-only**（`docker.py:104, 634`）；
- Unix 文件模式 / `chmod` 权限。

### 5.7 Plan 与 Sandbox 的落地建议

Plan 是 OS 无关的 Runtime 门禁，可在 Windows/Linux 同时交付，**不阻塞在 Sandbox 深化上**；平台隔离（尤其 Windows 与 Docker 的配额/网络）按 #358 / #549 独立推进。这正是票面「Plan 只读执行与平台 Sandbox 独立界定」的要求。

## 6. 与 #521 的同图关系、风险与开放项

### 6.1 与 #521 的同图关系（一句话）

#521 定义「执行前干预缝」（before 可拦截、after 仅反馈、**改参后重走 Validation/Permission/Scheduler、allow 不得提升权限**，F28）；#526 的两个能力都是该缝上的消费者——审批缓存的**参数二次校验**与 Plan 的**只读门禁**都必须在同一个 before 决策点（`executor.py:413-422`，F7）执行，且都不得绕过 `needs_approval`/policy。

### 6.2 风险

- **合同扩大**：两项都超出当前冻结合同，须先写 ADR、经用户批准后才能转 Ticket 施工（§9.1.1）。获批前不得施工。
- **审批缓存被误实现为「按工具名/相似文本放行」**：这会直接违反 F21/F22/F25，是本设计最大的安全风险。
- **Plan 被误实现为 prompt 约束或在 local 上承诺「进程级只读」**：前者违反 F23，后者违反 F13/F27。
- **`approve_session` 与 `approve_policy` 混做**：两者风险量级不同，混做会把会话级授权悄悄升格为跨会话持久授权。

### 6.3 开放项（需用户裁决 / 需核实）

- **需用户裁决**：授权 TTL 的具体值 N；授权的 durable 事件形状、是否新增 `EVENT_TYPE`；**fork 是否继承授权**；`update_plan` 是否豁免 Plan 门禁；`approve_policy` 是否本票处理（建议拆票）；目录级 path 授权是否允许。
- **需核实**：Codex / ZCode 票面引用机制的版本细节；Windows 网络隔离（WFP/防火墙）与 AppContainer 落地可行性；Job Object 内存/CPU 限额的可用性。
- **归属他票（不在本设计）**：DockerSandbox 缺 `--network/--memory/--cpus/--pids-limit`（F14）→ #358 / #549；local 无隔离（F13）→ #358。
- **无依赖关系**：#561（外部 run 可达性）**不依赖**本设计（F25，审计 `:128/:149`）。
- **必须保持的既有约束**：ADR-0041 D5（pending 禁改档）、D6（declared / effective / policy_at_approval 三分离）；#358 W-14（逐 call 范围、不经 Fork 静默扩大）；#521（allow 不提升权限）。
