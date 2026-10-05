# issue #684 设计方案：approve_policy 持久授权规则（v3）

> **状态：DESIGN PROPOSAL v3 —— 待用户裁决，未施工。**
> v2 被用户要求"多参考成熟产品，CLI 和 Web 都要做"。v3 加深三家对标、补上 Web 管理面；
> 保持 v2 的简单内核（project 级、allow、精确键、显式确认）不变。

## §1 一句话

审批时第三档"以后都允许"；规则以精确键存项目配置文件；**CLI + Web 都可查可删**。

## §2 交互：三档 + 粒度（对标 ZCode / Codex）

| 选项 | 语义 | 来源 |
|---|---|---|
| 仅这次 | `APPROVE_ONCE` | 已有 |
| 本会话有效 | `APPROVE_SESSION` | #526 |
| 以后都允许 | `APPROVE_POLICY` | **本票** |

选"以后都允许"时再选粒度（**PORT DESIGN**，直接抄 ZCode）：

| 粒度 | 对应 | 键 |
|---|---|---|
| 精确（含参数） | ZCode `exactCommand` | #526 完整身份键（含 args hash） |
| 命令级（不含参数） | ZCode `commandPrefix` | tool + command，不含 args hash |

Codex 的做法更进一步值得抄（**PORT DESIGN**，来源 agentclientprotocol/codex-acp）：
"Approve and install **the exact proposed command-prefix rule**"——amendment 必须与
proposal 逐字段一致，不一致就拒绝；渲染后含换行的前缀**不展示**（防换行走私）。
本票照搬：命令级规则的 command 必须与用户批准时的提议逐字一致。

## §3 成熟产品深度对标

### Claude Code：settings.json（**PORT DESIGN**，形态与存储）

```jsonc
// .claude/settings.json（项目级，团队共享，可提交）
// .claude/settings.local.json（项目级个人，gitignored）
// ~/.claude/settings.json（用户级，跨项目）
{
  "permissions": {
    "allow": ["Bash(npm run lint)", "Bash(npm run test:*)"],
    "ask":   ["Bash(git push:*)"],
    "deny":  ["Read(./.env)", "Bash(rm -rf *)"]
  }
}
```

抄的点：
- **三级作用域文件**：项目共享 / 项目个人 / 用户。本票 v1 只做第一级
  （`.agent-harness/approve-policy.json`），后两级留扩展位。
- **规则即字符串列表**：`Tool` 或 `Tool(specifier)`，人类可读、可手改、可 diff。
  本票的 key 用 #526 身份键的字符串形态，保持同样可读。
- **求值顺序** `deny → ask → allow`，首个命中胜出，deny 绝对优先。
  本票 v1 只有 allow，但求值器按此顺序写死，deny 位留空以后填。
- 来源为第三方整理文档（[1][2]），非一手官方文档；机制已多源交叉。

### Codex：exec-policy amendment（**PORT DESIGN**，创建语义）

- `acceptWithExecpolicyAmendment` = "批准并安装提议的精确 command-prefix 规则"；
  amendment 是 Codex 给出的**精确结构化值**，与 proposal 不一致即拒绝。
- 持久化位置：`exec_policy.rs` 的 `append_amendment_and_update` 落盘 command-prefix allow 规则；
  命中时 `with_cached_approval` 跳过弹窗。
- 本票照搬：规则内容必须来自 Runtime 派生的精确值（用户只能选粒度，不能手写规则文本）；
  命令级规则含换行符直接拒绝安装。

### ZCode：四档决策（**PORT DESIGN**，交互分层）

- `Allow` / `Allow for session or project` / `Reject` / `Always Reject`——
  "这次 / 会话 / 项目 / 永久"四档正是本票三档交互的直接依据。
- `exactCommand`（仅此命令）vs `commandPrefix`（前缀）即 §2 的两档粒度。

## §4 规则形状

```jsonc
// .agent-harness/approve-policy.json（项目根目录，可提交）
{
  "rules": [
    {
      "id": "uuid",
      "tool": "bash",
      "key": "bash:exact:npm run build",      // 或 "bash:command:npm run build"
      "granularity": "exact" | "command",
      "permission_at_approval": "WORKSPACE_WRITE",  // 命中时重验
      "created_at": "2026-10-05T12:00:00Z"
    }
  ]
}
```

v1 范围（故意小）：project 级、allow、exact/command 两档粒度。
不做：user 级、持久 deny、自由通配、contract_version 绑定系统。

## §5 求值

```
1. 会话级 grant 命中（#526）→ 放行
2. 持久 allow 命中：
     a. 规则存在且 granularity 匹配
     b. 工具当前 permission == permission_at_approval（防提权）
   → 放行
3. 否则 → 默认逐调用审批
```

## §6 存储与审计：文件即真相

- 项目根 `.agent-harness/approve-policy.json`（对标 Claude Code 项目级 settings.json）。
- 会话级 grant 是运行时状态 → 走事件；project 级持久规则是**配置** → 进文件。
  可版本控制、可 diff、可 code review，删文件即清空；git 历史即审计链。
- **项目根口径（v1 已知边界）**：v1 以**进程当前工作目录**（`Path.cwd()`）为项目根
  ——`ToolExecutor.__init__` 回落、CLI `_approve_policy_store`、Web `approve_policy_store()`
  三处逐字一致，保证管理面与执行域读写同一份文件。**多 workspace 同进程**场景
  （同一进程内并存多个项目根）v1 **不支持**，待后续票；届时应改为由调用方显式传入
  项目根、不再回落 cwd，并把管理面也按当前 workspace 解析。

## §7 管理面：CLI + Web（都要）

### CLI

```bash
agent-harness approvals policy list                  # 列出本项目规则
agent-harness approvals policy remove <id> [--yes]   # 删除（默认二次确认）
```

### Web API（对标 #526 revoke 端点形态：幂等、422 先于 404）

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/approve-policy/rules` | 列出本项目规则 |
| `POST` | `/api/approve-policy/rules/{id}/revoke` | 撤销（幂等，不存在也 200；二次确认由前端做） |

创建走审批流（选"以后都允许"即创建），不另开创建端点——入口唯一，禁止静默创建。
前端：工作台加一个"持久规则"列表页（读 GET、调 revoke），形态对标 #526 的审批管理。

## §8 安全红线（三条）

1. **显式确认**：必须用户主动选"以后都允许"（F21）。
2. **无模糊匹配**：只有 exact / command 两档，系统不猜（F22）；命令级规则含换行符拒绝安装（抄 Codex）。
3. **防提权**：每次命中重验 `permission`（§5-2b）。

## §9 与 #526 的关系

- 复用 `approval_identity` 键派生（exact 档直接复用；command 档去 args hash）。
- 持久规则不受 1800s TTL 约束；不并入 `permission_policy`（ADR-0041 D6）。

## §10 验收

1. 批"以后都允许"后，同项目新会话不再弹窗。
2. 换项目不生效（project 级隔离）。
3. CLI `list`/`remove` 可用；Web 列表页可查可撤销；删除后恢复弹窗。
4. 工具 permission 变化后规则不命中；含换行的命令级规则拒绝安装。
5. gate0 6/6，TDD。

## §11 待拍板

1. 配置文件名 `.agent-harness/approve-policy.json` 是否 OK？
2. 命令级粒度（不含参数）是否接受？
3. Web 列表页的形态：独立页面还是并入现有审批/预算页？

---

**来源标注**：
[1] https://github.com/ai-infra-curriculum/ai-agent-guidebook/blob/HEAD/guides/claude-code/settings-and-permissions.md
[2] https://github.com/paulo-yamagishi/claude-code-setup/blob/HEAD/core/settings-permissions.md
[3] https://github.com/agentclientprotocol/codex-acp/blob/HEAD/docs/permission-extension.md
（以上为第三方整理/协议文档，非一手官方文档；关键机制已多源交叉。ZCode 机制来自此前已核实的调研。）
