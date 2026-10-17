# #1200 插件管理窗口 + `/` 菜单 Skills 分组 — 最小设计

状态：实现中（施工执行器）。效果图权威：`plugin.html`（PR #1199 已批准）。

## 1. 范围

- Phase 1（只读）：插件窗口（左 `MCP SERVERS` / `SKILLS` 分组 + 右详情）。
- Phase 2（管理）：server 启停/重连/断开、工具级启停、`require_confirm`、skill 启停。
- Phase 3（`/` 菜单）：后端单一注册表 + 前端渲染/fuzzy/键盘导航；MCP 工具不进菜单。

## 2. 方案依据（协议 §1.3）

| 来源 | 机制 | 判定 |
|---|---|---|
| Claude Code `/plugin` `/mcp`（票面 §3） | 左列表+右详情；禁用≠删除；`N tools`；Disable/Reconnect；skill 目录名=命令名、插件前缀、`argument-hint`、描述 1536 截断 | ADAPT |
| Windsurf MCP（票面 §3） | server + 工具双层粒度（`disabledTools`） | ADAPT |
| Cursor Customize（票面 §3） | 统一管理页 + 作用域过滤；`/` 直接运行 skill | PORT DESIGN |
| 本仓既有 | `GET /api/skills`、`GET /api/mcp/servers`、`MCPPackageManager`、`SkillDiscovery`、#1195 slash 组件、`PermissionRuleSet` | REUSE |
| 冻结规格 | `09 §1.1` 允许重启生效；`08 §7` OPTIONAL_RUNTIME 降级；ADR-0010 无第二份 YAML | 合规 |

### 2.1 Phase 3「多源合并为单一注册表」的来源（D2=A）

票面 §3 把这条列在 Claude Code 名下；实际证据来自四家成熟产品的**同一**形态，逐条记 commit：

| 来源 | 机制 | 判定 |
|---|---|---|
| Claude Code `/` 菜单 | 内置命令与自定义 skill 同处一个列表；`↑↓`/Enter/Esc；skill 用目录名作命令名 | ADAPT |
| VS Code Copilot Chat `/` 命令面板 | 单一命令面按 kind 分组渲染，前端不维护"哪些命令存在" | ADAPT |
| JetBrains AI Assistant 斜杠命令 | 同一次查询返回全部候选（无第二份前端清单） | ADAPT |
| Cursor `/` 菜单 | skill 直接可运行；插件来源加前缀消歧 | PORT DESIGN |

判定 = **A（后端单一注册表）**：前端只保留按 name 查表的 `run` 回调，命令是否存在由
`GET /api/plugins/slash-registry` 一处决定（前端 `slashCommands.ts` 不再维护 builtin 元数据）。

### 2.2 显式 `/skill` 的正文注入（ADR-0011 Gate 2 的唯一例外）

票面 §4「执行语义 = Claude Code：消息开头出现 skill 名则执行其内容」要求当轮把正文注入
Context。这与 ADR-0011 Q3 的「全文绝不在此路径出现」**表面冲突**，故按 §5 的边界登记：
Gate 2 的默认面（目录注入零正文）不变；例外只在**用户亲手**在消息开头写 `/<skill>` 时成立，
注入走 `load_skill` 同款 untrusted framing 与 `cap_skill_body` 截断。判定点唯一——
`user_source_events()` 的最新一条用户消息（HTTP / 排队 / steer / CLI 四条路径共用），
不在前端 payload 上另设判定。ADR-0011 Q3 的措辞已同步补记（见该 ADR 的 #1200 增补）。

## 3. 决策（用户 11:17 裁决）

- A1：原地扩展 `GET /api/skills`，不新建端点；新增 `scope/origin/plugin?/enabled`，描述截断 1536，保留既有字段与可选 HMAC 回执。
- B1：`GET /api/mcp/servers` 数据源 = config ∪ runtime；新增 `tool_count/needs_auth/require_confirm`（向后兼容）。
- C1：持久化 `<workspace_dir>/plugin-settings.json`（运行时选择态，非配置源）；与 `mcp-plugin-installs.json` 取交集，任一禁用胜出。
- D1：落盘后**下次装配生效**，不做热重载；UI 明示「重启后生效」。
- E：`require_confirm` 检查放在 `_check_approval` 规则引擎 `ALLOW` 早退之前；命中后跳过授权缓存短路。

## 4. 数据形状

`plugin-settings.json`：

```json
{
  "disabled_servers": ["filesystem"],
  "disabled_tools": {"playwright": ["browser_upload"]},
  "require_confirm": ["github"],
  "skill_overrides": {"code-review": {"enabled": false}}
}
```

- 原子写（临时文件 + `os.replace`），复用 `skills.package_lock.registry_lock` 跨进程锁。
- 读侧逐字段容错：坏 JSON / 坏字段进 `errors` 并按默认值继续（可选子系统不得拖垮 core），管理面可见。

## 5. 生效链路

- MCP：`_wire_mcp` 读 settings → 过滤 disabled server（config 与 packaged 并集）→ `build_mcp_capability(..., disabled_tools, require_confirm)`；**禁用工具在 build 期就不进 `tools` 表**（管理面仍可从 `server_tools` 看到全量）；`MCPTool.require_confirmation` 供 Executor。
- Skills：`SkillDiscovery` 接收 `disabled_names` 与 root 元数据；条目带 `scope/origin/plugin/enabled`；`catalog()` 只出启用项（模型 context + `/` 菜单），`all_entries()` 出全量（管理面）；`load()` 只认启用项。
- 审批：`_check_approval` 在 `RuleVerdict.ALLOW` 早退前检查 `tool.require_confirmation`；命中时跳过会话 grant / 项目持久规则。
- 注册表：`GET /api/plugins/slash-registry` = 5 条 builtin（后端元数据）+ `catalog()` ∩ settings 的 skills；MCP 工具永不进入。
  · **两个只读面（菜单 / 面板）现读、执行面按装配期快照**（裁决 D1 的直接后果）：面板与 `/` 菜单都读 `plugin-settings.json` 现值，故刚禁用即消失；但 `_invoked_message` → `capability.load()` 走装配期 `catalog()`，手敲 `/<name>` 在下次装配前仍会执行。这是 D1「不做热重载」的固有语义，不是缺口——面板行为区已明示该时间差。
- 显式 `/skill`：`SkillCatalogContextProvider._invoked_message` 读最新用户消息，匹配
  `^\s*/([\w:-]+)(?:\s+([\s\S]*))?$` 且显示名在 `catalog()` 内 → 注入正文（`$ARGUMENTS` 插值）。
  锚定**整条消息开头**——前端点选 skill 因此不保留命令前的前缀（与内置命令的定位切除相反）。

## 6. 兼容性

- `/api/skills`、`/api/mcp/servers` 仅增量字段；既有测试断言的键与顺序不变。
- HMAC 回执按 `response.content` 计算，扩展载荷不破坏挑战流程。
- 前端 `slashCommands.ts` 不再维护 builtin 元数据；`run` 回调按 name 查表。
