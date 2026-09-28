# ADR-0032 — 自定义模型供应商管理（全局 CRUD + 密钥进 Windows 凭据管理器）

- **Status**: Proposed（契约已冻结，待实现）
- **Date**: 2026-09-13
- **Deciders**: 用户（密钥存储、只做 OpenAI 兼容、作用域、测试方式逐条裁定）+ 本 Agent（机制设计）
- **Related**：Issue #203；`model/config.py`（`PROVIDER_PRESETS` / `parse_model_catalog`）；`model/provider.py`（`create_chat_model`）；ADR-0014（Model Fallback 两级链）；`web/app.py:548-575`（`_render_model_option` 的硬编码 `is_available`）；AGENTS §13（凭据零泄露）、§6（Reuse First）

> 本文档同时承担 ADR 与设计稿角色（决策 + API 契约 + UI 规格在一处），避免两个文件漂移。
> **读者**：接下来实现 #203 的人。**任何情况下不得在本文件或实现里写入真实 API Key。**

---

## 1. Context

### 1.1 现状：今天"加一个供应商"必须改代码 + 改 `.env` + 重启

| 事实 | 证据 |
| --- | --- |
| catalog 只来自环境变量 | `model/config.py:124-198` `parse_model_catalog` 读 `settings.agent_models`（`SecretStr` JSON，`config.py:113-118`） |
| provider 必须是硬编码的 preset key | `config.py:27-63` `PROVIDER_PRESETS`；`:153-157` 强制 provider ∈ preset keys |
| 只有 OpenAI 兼容线 | `model/provider.py:81-129` `create_chat_model` 无条件构造 `ReasoningChatOpenAI` |
| **连接测试不存在** | `/api/models`（`web/app.py:870-919`）只回显 catalog；`_render_model_option` 把 `"is_available": True` **写死**（`app.py:564`）——UI 里的"可用"没有任何探测支撑 |
| 没有供应商实体 | 无持久化、无 CRUD、无删除、无加密存储 |
| 密钥只在 `.env` | `AGENT_MODELS` 的 JSON 里带 key |

### 1.2 用户裁定（逐条，不得偏离）

| # | 裁定 |
| --- | --- |
| 1 | 入口放「管理模型」处，设计仿照图 1：用户自填 **API Key / Base URL / 模型名**，能**测试连接**、能**删除** |
| 2 | 密钥存储 = **Windows 凭据管理器**（不用自研加密文件） |
| 3 | **只做 (i) OpenAI 兼容**：不做 Anthropic adapter、不做"API 格式"下拉 |
| 4 | 作用域 = **全局**（不是 per-session / per-project） |
| 5 | 测试连接 = **打最小的一次 chat completion**；**不**顺带验证"能否工具调用" |
| 6 | 用户已授权本轮直接采用成熟库（"能直接拿来用的就直接拿来用，不要手写"） |

---

## 2. 决策

| # | 决策 | 一句话 |
| --- | --- | --- |
| D1 | 新增"用户自定义供应商"为**全局配置实体**，与 `AGENT_MODELS` catalog 并存 | 同 id = 覆盖内置 preset（用户换自建代理是首要场景）；新 id = 新增 |
| D2 | 非密字段落本地 JSON；**密钥只进凭据管理器** | 配置文件里连 key 的哈希都不存 |
| D3 | 密钥用 **`keyring`（MIT）**，Windows 后端即凭据管理器 | REUSE 而非 BUILD（见 §5） |
| D4 | 只做 OpenAI 兼容线，走**同一条** `create_chat_model` | 不新增 provider 分支、不装 anthropic SDK |
| D5 | 连接测试 = 走真实构造路径 + 一次最小 chat completion | 避免"测试通过但实际不能用"的错配（§6） |
| D6 | 删除 = 先删凭据、再删配置（凭据删失败则中止） | 把"孤立可用密钥"的窗口压到零 |
| D7 | `is_available` 改为**真实本地判定**（不再是硬编码 true） | 语义明确为"已配置"而非"网络可达"；可达性由「测试连接」给结论 |
| D8 | 解析入口收敛为**一个** `resolve_provider(id)` | `create_chat_model` 与会话模型推导共用，禁止两处各写一遍 if |
| D9 | 被删除的 provider **不静默 fallback** | 明确报错"供应商已删除，请改选"，符合不变量 #9 的精神 |
| D10 | 凭据后端不可持久化时**明确报错**，绝不落明文 | 见 §7.3 |
| D11 | 保留 `/api/models` 既有契约，仅把 `is_available` 改为真实判定 + 增 `unavailable_reason` | 行为变更在票面注明 |

---

## 3. 数据模型与合并规则

### 3.1 非密配置（本地 JSON）

```json
{
  "version": 1,
  "providers": [
    {
      "id": "my-proxy",                     // slug，见 §4.1
      "label": "自建代理",                   // 显示名，可空
      "base_url": "https://api.example.com/v1",
      "models": [
        {"model_id": "deepseek-chat", "label": "DeepSeek Chat",
         "context_window": 64000, "capabilities": {"tool_calling": true}}
      ],
      "created_at": "2026-09-13T10:00:00Z",
      "updated_at": "2026-09-13T10:00:00Z",
      "last_test": {"ok": true, "at": "2026-09-13T10:05:00Z", "detail": "200 · 412ms"}
    }
  ]
}
```

- **落点**：实现时优先复用 `Settings` 里**已有**的用户级目录字段；若没有，新增 `provider_store_path`（默认 `%USERPROFILE%\.agent-harness\model-providers.json`）。
  **不得**放进 workspace 目录（那是 per-session 的，作用域不对；用户裁定作用域 = 全局）。
- `last_test.detail` **不得**含密钥；失败时也只放归类 + 截断摘要（§6.3）。
- 该文件**不含**任何密钥字段。`api_key` 只存在于请求体（写路径）与凭据管理器。

### 3.2 合并规则（`resolve_provider` 的唯一口径）

| 维度 | 规则 |
| --- | --- |
| provider 同名（自定义 id == 内置 preset） | `base_url` / 凭据**覆盖**内置（`AGENT_MODELS` 里的 key 不再生效）；`models` 取**并集**（按 `model_id` 去重，自定义优先） |
| provider 新名 | 直接加入列表 |
| 列表展示顺序 | 内置（按现有顺序）→ 自定义新增（按 `created_at`） |
| 标记 | UI 必须能区分：`"内置"` / `"自定义"`（新 id）/ `"已覆盖"`（同 id 覆盖内置） |
| 会话模型解析 | `resolve_provider(id)` 返回 `(base_url, api_key, model_id)` 或抛类型化错误；`create_chat_model` 与 `_amend_with_session_model` **都**走它 |

> **为什么允许覆盖内置**：最现实的诉求是"把 deepseek 指向自建代理"，禁止覆盖会逼用户改 `.env` 并重启——那正是本票要消灭的体验。代价是覆盖后的行为不再等于出厂配置，所以 UI 必须打「已覆盖」标记。

---

## 4. API 契约

所有端点都**永不含密钥**（响应体、错误体、日志三处）。

| 方法 / 路径 | 作用 | 关键约束 |
| --- | --- | --- |
| `GET /api/model-providers` | 列出（含 `kind: builtin\|custom\|override`、`has_api_key: bool`、`is_available: bool`、`unavailable_reason: str \| null`、`last_test`） | **不返回** key、不返回 key 片段；不做网络探测 |
| `POST /api/model-providers` | 创建（body: `id`, `label?`, `base_url`, `models[]`, `api_key?`） | id 冲突且 `kind=custom` ⇒ 覆盖更新；id 与内置同名 ⇒ 覆盖内置；`api_key` 只在此处/`PUT` 出现 |
| `PUT /api/model-providers/{id}` | 更新 | **`api_key` 省略 = 不改密钥**；`api_key: ""`（空串）= 显式清除凭据（独立动作，不是"顺手清"） |
| `DELETE /api/model-providers/{id}` | 删除（配置 + 凭据） | 顺序：先删凭据（失败 ⇒ 中止并返回可读错误，配置保持原样），再删配置 |
| `POST /api/model-providers/{id}/test` | 连接测试 | 见 §6；结果写回 `last_test`（非密） |
| `GET /api/models`（既有） | 保留契约 | `is_available` 改为真实判定 + 新增 `unavailable_reason`；`models`/`providers` 结构与字段名不变 |

### 4.1 校验规则

| 字段 | 规则 | 违反 |
| --- | --- | --- |
| `id` | `^[a-z0-9][a-z0-9-_]{0,63}$` | 422 |
| `base_url` | 必须 `http` / `https` scheme；**拒绝** `file:` / `ftp:` / 其他 | 422（`file://` 会把本地文件读进请求） |
| `models[]` | 非空；`model_id` 非空；每条 `context_window` 可选正整数 | 422 |
| `api_key` | 非空（创建时可选 = 稍后再填）；长度上限 4096 | 422 |
| 删除不存在的 id | — | 404 |
| 无可用凭据后端 | — | 503 + 可读原因（§7.3） |

---

## 5. 密钥存储：REUSE `keyring`（Reuse First 决策记录）

| 方案 | 结论 |
| --- | --- |
| **(a) `keyring`（MIT）** | ✅ **选它**。Windows 后端就是凭据管理器（Generic Credentials）；自带错误映射、可注入 memory backend（测试友好）、跨平台降级行为明确 |
| (b) 自写 `ctypes` 调 `crypt32.dll` 的 `CredWriteW/CredReadW/CredDeleteW` | ❌ 需要自己处理 Win32 错误码、字符串编码、无测试替身；违反"不重复造轮子" |
| (c) 加密文件（自研/AES） | ❌ 用户已明确要凭据管理器；自研加密的密钥保护问题（密钥放哪）无解 |

**凭据坐标**：`service = "agent-harness:model-provider"`，`username = <provider id>`，`password = <api_key>`。

**依赖**：`keyring>=24`（MIT）。若它拉入平台特定依赖（Windows 上 `pywin32-ctypes`），在 `pyproject.toml` 里以 `keyring` 单一依赖表达，不逐个钉平台包。

**不做的事**：**不**把 `.env` 里 `AGENT_MODELS` 的既有 key 迁移进凭据管理器（迁移是对用户凭据存储的不可逆外部副作用；内置 provider 继续读 env，行为不变）。

---

## 6. 连接测试（= 最小 chat completion）

### 6.1 为什么必须走真实构造路径

测试**必须**用 `create_chat_model(...)` 构造出真实模型对象再 `ainvoke`，**不得**自己拼 HTTP 请求。
理由：自拼 HTTP 测的是"另一条线"，会出现"测试通过、实际跑不通"（或反之）的错配——这是本功能最容易犯的错。

### 6.2 参数（固定，不得自行加料）

```
messages = [{"role": "user", "content": "ping"}]
max_tokens = 1                     # 最小输出；不要求模型说任何有意义的话
tools = 不传                        # 用户裁定：不验证工具调用
其他参数（temperature/top_p 等）不传（用 provider 默认）
超时 = settings.model_test_timeout_seconds（新增，默认 15.0）
```

成功判定：调用返回且**没有抛异常**。内容为空也算成功（本测试只证明"认证 + 地址 + 模型名可用"）。

### 6.3 失败归类（回给 UI，不入事件流）

| 情况 | 归类 `reason` | UI 文案 |
| --- | --- | --- |
| 401 / 403 | `auth_failed` | 「认证失败：API Key 无效或无权限」 |
| 404 | `model_or_route_not_found` | 「模型或路径不存在：检查 Base URL 与模型名」 |
| 超时 | `timeout` | 「连接超时（>15s）」 |
| 429 | `rate_limited` | 「被限流（429）：配置本身可能是对的，稍后再试」 |
| 其他 4xx/5xx | `provider_error` | 「供应商返回 <code>：<截断 500 字符的正文摘要>」 |
| 网络层异常 | `network_error` | 「网络不可达：<异常类型名>」 |

- **必须**：摘要截断 500 字符、**不含** api_key；错误响应与日志里都不得出现 key 或带 query 的完整 URL。
- 测试**不落 durable SessionEvent**（不是会话事实）；只写一条结构化日志（provider id / 结果 / 耗时 / reason，无密钥）。
- 结果写回配置文件的 `last_test`（非密），供列表显示"上次测试：通过（3 分钟前）"。

---

## 7. 安全

### 7.1 密钥生命周期（实现必须逐条满足）

1. 写入路径（`POST`/`PUT`）是**唯一**能见到明文 key 的地方；写进凭据管理器后立即丢弃，不缓存、不落盘、不进日志。
2. `api_key` 省略 ≠ 清除；清除需要显式传空串。
3. **任何**响应都不回传 key 或 key 片段（连末 4 位也不给：半个密钥进截图/日志没有收益）。
4. 更新 `base_url`/`models` 时**不得**触碰已存在的凭据。
5. 删除：先删凭据（失败 ⇒ 中止），再删配置（§2 D6）。
6. 既有 `.env` 行为不变；两条来源并存时：**自定义 provider 的凭据优先于 env**（因为它是用户对内置 provider 的显式覆盖）。

### 7.2 测试断言（防回归）

- `GET /api/model-providers` 与 `GET /api/models` 的响应序列化结果里不含 `api_key` 字段、不含 `sk-` 子串（沿用既有 `tests/web/test_web_models.py:56-58` 的形状并扩展）。
- 结构化日志断言：创建/更新/测试/删除四条路径的日志里不含 key。
- 使用**假 key**（如 `sk-test-not-a-real-key`）做测试数据；**任何**真实 key 不得出现在仓库、文档、测试夹具里。

### 7.3 凭据后端不可用时的行为（诚实优先）

`keyring` 在无桌面会话的 Windows 服务账户、或未配置的 Linux 上可能落到**不可持久化**后端。要求：

- 检测后端能力；不可持久化 ⇒ 创建/更新带 `api_key` 的请求返回 **503** + 可读原因（「当前平台没有可用的凭据存储，无法保存 API Key」）。
- **绝不**静默改写进明文文件，也**绝不**静默丢弃 key 后报成功。
- 读不到凭据（服务不可用或凭据被外部删除）⇒ 该 provider `is_available=false`、`unavailable_reason="credential_unavailable"`，**不影响**其他 provider 与既有会话（不变量 #21）。

### 7.4 SSRF 说明（明确的范围界定）

`base_url` 由用户填写，服务端会去请求它。**本功能定位为本地单用户工具**（凭据进本地凭据管理器），因此：

- 只做 scheme 限制（`http`/`https`，§4.1），**不做** URL 白名单 / 内网地址封禁。
- **未决**：若 Web 服务将来暴露到非本机，必须重新评估（另开 ADR）。这条写进 §11。

---

## 8. UI 规格（图 1 样式，impeccable 设计依据）

### 8.1 入口与布局

- 入口：「管理模型」项位于模型选择器（#199 的两级飞出菜单）内，与"默认链"同级。
- 弹层两栏：
  - **左**：provider 列表。每行 = 状态点（可用绿 / 不可用灰）+ 名称 + 标记（`内置`/`自定义`/`已覆盖`）+ 上次测试时间；底部「+ 新建供应商」。
  - **右**：选中 provider 的表单：显示名 / Base URL / 模型列表（可增删行，每行 model_id + 可选 label）/ API Key / 底部按钮「测试连接」「保存」/ 危险区「删除供应商」。

### 8.2 交互约束

| 交互 | 要求 |
| --- | --- |
| API Key 输入框 | `type="password"`，已有凭据时占位符为「已保存（不回显）」；不显示任何片段；留空 = 不改（**不是**清除） |
| 清除密钥 | 独立的小动作（如「清除已保存的密钥」），需二次确认 |
| 测试连接 | 按钮进 loading；结果**内联**显示：成功 = 绿勾 + 「连接正常」+ 耗时；失败 = 红叉 + 归类文案 + 可展开的原始摘要 |
| 保存 | 不自动触发测试（用户裁定测试是显式动作）；保存成功后列表状态点即时更新 |
| 删除 | 二次确认（确认框内明确写出 provider id）；失败时保留原状态并显示可读原因 |
| 新建 | id 按 slug 规则即时校验；与内置同名时表单内明确提示「将覆盖内置 <id> 的配置」 |
| 键盘/无障碍 | `Esc` 关闭（与既有 picker 一致）、焦点陷阱、每个输入有 `<label>` 关联、状态点有 `aria-label` |
| 主题 | 新增颜色/尺寸 token 必须在 `:root` **与** `:root[data-theme='light']` 双份定义（AGENTS §15） |

### 8.3 文案（空态/错误态，逐条给定）

| 状态 | 文案 |
| --- | --- |
| 无自定义供应商 | 「还没有自定义供应商。添加一个后，它的模型会出现在模型选择器里。」 |
| 凭据缺失 | 「未配置 API Key」 |
| 凭据不可用（后端问题） | 「无法访问系统凭据存储」 |
| 测试成功 | 「连接正常 · <ms>」 |
| 测试失败 | 按 §6.3 归类文案 |
| 被删 provider 的会话 | 「供应商 <id> 已被删除，请在模型选择器里改选模型」 |

---

## 9. 不变量

| 不变量 | 守法 |
| --- | --- |
| #21 Optional 故障不拖垮 Core | 列表接口不做网络探测；单个 provider 凭据不可用不影响其他 provider 与既有会话 |
| #9 Model Fallback 与 Tool Retry 分离 | 被删 provider **不静默 fallback**，明确报错（D9） |
| #8 / #10 唯一构造入口 | 自定义 provider 也走 `create_chat_model`；解析收敛到 `resolve_provider` |
| #11 权限/密钥是运行时边界，不靠 Prompt | 密钥只进凭据管理器；模型永远看不到自己 provider 的 key |
| #22 前端不维护第二套真相 | 前端只渲染服务端列表；密钥状态以 `has_api_key` 为准 |
| AGENTS §13 凭据零泄露 | §7.2 的断言 + 本文档无真实 key |

---

## 10. 测试要求

| # | 用例 | 断言 |
| --- | --- | --- |
| T1 | CRUD 基础 | 创建 → 列表出现（`kind="custom"`）→ 更新 → 删除 → 列表消失 |
| T2 | **无密钥回显** | `GET /api/model-providers`、`GET /api/models`、错误体、结构化日志里均无 `api_key` 字段与 `sk-` 子串 |
| T3 | 凭据生命周期 | 创建带 key ⇒ 凭据管理器存在该条；`PUT` 省略 key ⇒ 凭据不变；`PUT` 空串 ⇒ 凭据被清；`DELETE` ⇒ 凭据消失（用 `keyring` memory backend 注入） |
| T4 | 覆盖内置 | 自定义同 id ⇒ `kind="override"`，base_url 生效、models 并集去重；内置 env key 不再适用于该 provider |
| T5 | `is_available` 真实判定 | 无 key ⇒ `false` + `unavailable_reason="missing_api_key"`；有 key ⇒ `true`（**不**做网络探测） |
| T6 | 连接测试走真实构造 | spy `create_chat_model` 被调用且传入解析后的 base_url/key/model；`tools` 未传；`max_tokens=1` |
| T7 | 失败归类 | mock 401/404/429/timeout/网络异常 ⇒ 对应 `reason` 与文案；摘要截断 ≤500 字符且不含 key |
| T8 | scheme 校验 | `file:///etc/passwd` ⇒ 422 |
| T9 | 删除顺序 | 凭据删除抛错 ⇒ 配置**保留**且返回可读错误（不出现"配置没了凭据还在"） |
| T10 | 无可用凭据后端 | 注入 fail backend ⇒ 带 key 的创建返回 503 + 可读原因；**不**落明文文件 |
| T11 | 被删 provider 不静默 fallback | 会话引用已删 provider ⇒ 下一轮返回明确错误（含 provider id），不切到默认链 |
| T12 | 前端 e2e | 新建 → 测试（mock）→ 保存 → 列表出现；删除（二次确认）→ 消失；key 输入框始终不显示明文；亮色主题下新 token 生效 |

---

## 11. 分期、依赖、Out of scope

**分期**：单批（批次 ⑤），后端 + 前端同批交付（入口在模型选择器内，依赖 #199 的两级菜单先落地；后端契约可先行冻结）。

**依赖**：新增 `keyring>=24`（MIT）。

**Out of scope**：

- 不做 Anthropic / Google / 其他 wire format adapter（用户裁定只做 OpenAI 兼容）。
- 不做"API 格式"下拉或协议探测。
- 不做 per-session / per-project 作用域（用户裁定全局）。
- 不做工具调用能力的自动探测（测试只打最小 chat completion）。
- 不做密钥轮换、多 key、配额/用量统计。
- 不做代理/自定义 header 配置。
- 不做 SSRF 白名单（§7.4 的范围界定；服务对外暴露时另开 ADR）。

**未决（实现时回填）**：

1. `keyring` 在无桌面会话的 Windows 服务账户下的实测行为（T10 覆盖了"不可持久化 ⇒ 报错"，但真实环境表现需记录）。
2. Linux CI 上 `keyring` 的后端落点（决定 T3/T10 的测试注入方式）。
3. 是否需要 provider 级别的"启用/停用"开关（用户在模型选择器里能按 provider 折叠已经是筛选，暂不引入）。
