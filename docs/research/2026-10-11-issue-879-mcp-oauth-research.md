# #879 OAuth-only HTTP MCP server 授权兼容：官方资料与产品机制调研

核实日期：2026-10-11
范围：MCP 当前授权规范、仓库锁定的 MCP Python SDK 2.1.1、七个成熟 MCP 客户端的官方文档、OAuth 撤销标准、Windows Credential Manager 与 keyring 上游资料。只记录公开一手来源明确说明的机制；文档未说明的能力标为“未说明”，不推断产品不支持。
对应票面：[T10 OAuth follow-up](../tickets/plugin-portability-2026-10-08/T10-mcp-oauth-followup.md)。

## 结论摘要

1. **票面所引 2025-11-25 Authorization 页面已不是核实日的当前版。** 当前正式页面为 2026-07-28。Python SDK 2.1.1 同版本 OAuth 文档也明确指出：2026-07-28 版将 CIMD 置于 DCR 之前，并弃用 DCR。锁文件固定 mcp 2.1.1（[uv.lock](../../uv.lock#L1655-L1657)），本地安装元数据也报告 2.1.1。SDK 主要 wire 机制与当前版对齐：PRM/授权服务器发现、PKCE S256、state 与授权响应 iss 校验、resource 参数、CIMD（调用方提供稳定 metadata URL 时）及 DCR 回退、refresh、scope step-up。来源：[当前规范](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)、[票面引用旧版](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)、[SDK 2.1.1 OAuth 文档（tag commit 0921d94a74db900dccd2d534842aa7b6160542d2）](https://github.com/modelcontextprotocol/python-sdk/blob/0921d94a74db900dccd2d534842aa7b6160542d2/docs/client/oauth-clients.md)。
2. **SDK 是 OAuth 协议实现，不是登录产品。** 调用方必须提供 TokenStorage、重定向处理器和回调处理器；TokenStorage 只有读取/写入 token/client_info 四个异步方法，没有删除接口。SDK 不负责本仓的浏览器/无浏览器路径、凭据持久化选择、状态呈现、注销 UX 或失败恢复政策。来源：[SDK API 文档](https://github.com/modelcontextprotocol/python-sdk/blob/0921d94a74db900dccd2d534842aa7b6160542d2/docs/client/oauth-clients.md#L193-L209)、[TokenStorage 定义](https://github.com/modelcontextprotocol/python-sdk/blob/0921d94a74db900dccd2d534842aa7b6160542d2/src/mcp/client/auth/oauth2.py#L127-L144)。
3. **票面“撤销”须区分本地注销与服务端撤销。** 当前 MCP Authorization 规范全文没有定义客户端 logout/RFC 7009 调用。RFC 8414 的 revocation_endpoint 是可选元数据；RFC 7009 的远端撤销要求客户端向可信 HTTPS endpoint 发 POST。SDK 2.1.1 能解析可选 revocation_endpoint，也有可选的**服务端** /revoke handler；但其**客户端** OAuth 实现没有调用 revocation endpoint。因此清除内存 token 或本地凭据不能称为服务端撤销。若 AC 要求对任意 OAuth-only server 远端撤销，协议与 SDK 不提供普遍保证；若 AC 的“撤销”意为本地安全注销，应明确命名为“清除本地凭据”，并将远端撤销限定为服务器支持时提供。来源：[RFC 8414](https://www.rfc-editor.org/rfc/rfc8414#section-2)、[RFC 7009](https://www.rfc-editor.org/rfc/rfc7009#section-2)、[SDK OAuthMetadata](https://github.com/modelcontextprotocol/python-sdk/blob/0921d94a74db900dccd2d534842aa7b6160542d2/src/mcp/shared/auth.py#L203-L230)、[SDK server revoke route](https://github.com/modelcontextprotocol/python-sdk/blob/0921d94a74db900dccd2d534842aa7b6160542d2/src/mcp/server/auth/routes.py#L193-L196)、[SDK server handler](https://github.com/modelcontextprotocol/python-sdk/blob/0921d94a74db900dccd2d534842aa7b6160542d2/src/mcp/server/auth/handlers/revoke.py#L32-L80)。
4. **现有 keyring seam 不能默认承载一个 OAuth JSON cache。** Windows CredentialBlob 官方上限是 5×512 = 2560 bytes；SDK token/client_info 是结构对象，序列化后可能超过单条上限，单个 JWT 也可能超限。keyring 上游 #540 记录过约 1280 个双字节字符的实际边界，并讨论了分片升级后降级会静默读回截断值的风险。七个客户端的公开资料没有披露一个可复用且证明可承载超长 token 的通用存储格式/上限。本报告不建议把合并 JSON 直接写入一个 entry，也不把未经验证的分片视为成熟方案。来源：[Microsoft CredentialBlob](https://learn.microsoft.com/en-us/windows/win32/api/wincred/ns-wincred-credentiala#credentialblobsize)、[keyring 上游 #540](https://github.com/jaraco/keyring/issues/540)。
5. 项目现有 [MCP config](../../src/agent_harness/mcp/config.py#L33-L49) 仅表达 headers；[HTTP client seam](../../src/agent_harness/mcp/client.py#L85-L95) 仅注入静态 headers；[OAuth 预检](../../src/agent_harness/plugins/mcp_precheck.py#L667-L677) 将 OAuth 描述标为 oauth_required_unsupported。SDK provider 可接入 HTTPX AsyncClient auth seam，但 token cache、回调/UI、登录状态、失效清理需单独适配；新增路径仍应走现有 MCP client/wiring 与 Tool Permission、Executor、Ledger、SessionEvent。

## 规范与 SDK 2.1.1：覆盖面和边界

### 当前规范

MCP Authorization 是 HTTP transport 的可选授权流程；stdio 按规范从环境取得凭据，不走该 HTTP OAuth 流。当前规范要求客户端用 Protected Resource Metadata 发现授权服务器，并支持 RFC 8414 与 OIDC discovery；注册优先级为已有预注册信息、支持时使用 CIMD、支持时 DCR 回退。2026-07-28 页面明确将 DCR 标为已弃用、保留兼容。授权请求和 token 请求都绑定 MCP server canonical URI 的 resource；访问 token 放 Authorization header，不放 query string。客户端按需申请最小 scope，授权响应按 metadata 校验 iss；refresh token 必须保密，但 AS 不保证签发。以上是 wire contract，不规定用户设置页、跨进程存储格式或“退出是否调用 revoke”。来源：[当前 Authorization 规范](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)。

票面原链指向 2025-11-25 版。旧版已经含 CIMD 优先、DCR 回退、resource、PKCE 和 token 安全要求；当前版的重要变化之一是把 DCR 明确标为 deprecated，并澄清 issuer、scope challenge 等要求。实施依据应引用当前版，不自行发明 OAuth 流程。来源：[旧版注册顺序](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization#client-registration-approaches)、[当前版注册优先级](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization#client-registration)。

### 锁定 SDK 的实际能力

本仓 uv.lock 锁定 mcp 2.1.1；本地安装元数据报告 2.1.1；上游 tag 对应提交 0921d94a74db900dccd2d534842aa7b6160542d2。SDK 同版本文档显式提到 2026-07-28 Authorization 修订。因此依据同版本文档与源码审阅，票面锁定 SDK 并未落在旧授权流程上。来源：[SDK 2.1.1 tag](https://github.com/modelcontextprotocol/python-sdk/tree/0921d94a74db900dccd2d534842aa7b6160542d2)、[该 tag OAuth 文档](https://github.com/modelcontextprotocol/python-sdk/blob/0921d94a74db900dccd2d534842aa7b6160542d2/docs/client/oauth-clients.md#L261-L290)。

| 能力 | SDK 2.1.1 可核实内容 | 边界 |
|---|---|---|
| 触发与 discovery | OAuthClientProvider 是 httpx2.Auth；接入 AsyncClient 后用于 Streamable HTTP；在未授权 401 流程中解析 PRM 和授权服务器 metadata。 | 需由项目把 provider 接入 HTTP client；不替本仓决定静态 headers 与 OAuth 模式的配置策略。 |
| OAuth wire | 生成 PKCE S256/state，回调比对 state 并验证授权响应 iss；支持授权码换 token。 | redirect_handler/callback_handler 由调用方提供；SDK 不启动系统浏览器、不实现终端粘贴 UX。 |
| 注册 | 调用方可提供 client_metadata_url；AS 宣告支持 CIMD 时用 URL client ID，否则 DCR 回退；可从 storage 复用 client_info。 | CIMD URL 是可选入参，SDK 不会为本项目自动托管稳定公开 HTTPS metadata；没有该 URL 时按 AS 能力回退。 |
| audience/resource | SDK 基于 server URL/PRM 校验资源；按协议版本或 PRM 条件，在授权与 token 请求中设置 RFC 8707 resource。 | 应用当前 HTTP MCP 版本和真实服务端验证最终参数，避免绕开 SDK 手写请求。 |
| refresh 与 scope | token 过期时尝试 refresh；响应省略 scope/refresh_token 时沿用先前值；失败后转完整重新授权；insufficient_scope 时合并 scope 后 step-up。 | 用户拒绝、无浏览器、存储失败、重登入口与状态文案属于应用路径；refresh 失败会清 SDK 内存 token，但 TokenStorage 无 delete，调用方需清理持久化旧凭据。 |
| 持久化/注销 | storage 持久化 OAuthToken 和 OAuthClientInformationFull。 | 仅读写，没有 delete/clear 或安全擦除方法；不提供 keyring、DPAPI 或加密文件策略。 |
| 撤销 | OAuthMetadata 模型能读取可选 revocation_endpoint。 | 在 mcp/client/auth 源码检索没有发现客户端向 RFC 7009 endpoint 发请求的实现；SDK server route/handler 属于另一角色。 |

来源：[SDK OAuth 文档](https://github.com/modelcontextprotocol/python-sdk/blob/0921d94a74db900dccd2d534842aa7b6160542d2/docs/client/oauth-clients.md)、[OAuthClientProvider API](https://github.com/modelcontextprotocol/python-sdk/blob/0921d94a74db900dccd2d534842aa7b6160542d2/src/mcp/client/auth/oauth2.py#L279-L332)、[PKCE/state/iss](https://github.com/modelcontextprotocol/python-sdk/blob/0921d94a74db900dccd2d534842aa7b6160542d2/src/mcp/client/auth/oauth2.py#L390-L431)、[refresh 失败处理](https://github.com/modelcontextprotocol/python-sdk/blob/0921d94a74db900dccd2d534842aa7b6160542d2/src/mcp/client/auth/oauth2.py#L489-L548)、[discovery/注册](https://github.com/modelcontextprotocol/python-sdk/blob/0921d94a74db900dccd2d534842aa7b6160542d2/src/mcp/client/auth/oauth2.py#L580-L747)、[scope step-up](https://github.com/modelcontextprotocol/python-sdk/blob/0921d94a74db900dccd2d534842aa7b6160542d2/src/mcp/client/auth/oauth2.py#L755-L785)。

### 撤销与登出的协议边界

RFC 8414 的 revocation_endpoint 是可选字段；MCP Authorization 没有将它设为必需 endpoint，也没有要求客户端 logout 时调用。RFC 7009 的远端操作是客户端向可信 endpoint 发 HTTPS POST；实现该规范的 AS 必须支持 refresh token 撤销，并 SHOULD 支持 access token 撤销。来源：[RFC 8414 metadata 字段](https://www.rfc-editor.org/rfc/rfc8414#section-2)、[RFC 7009 endpoint 与请求](https://www.rfc-editor.org/rfc/rfc7009#section-2)。

SDK v2.1.1 shared OAuthMetadata 含 revocation_endpoint；server auth 可选启用 /revoke 并调用 server provider.revoke_token。client OAuthProvider 没有相应 HTTP 请求。把本地 token 设为 None、从凭据库删除或删除 MCP server，最多证明本地不再持有/使用凭据；除非能证明向 endpoint 发出请求并处理响应，否则不能说授权服务器已撤销授权。来源：[SDK metadata 字段](https://github.com/modelcontextprotocol/python-sdk/blob/0921d94a74db900dccd2d534842aa7b6160542d2/src/mcp/shared/auth.py#L203-L230)、[server revoke 路由](https://github.com/modelcontextprotocol/python-sdk/blob/0921d94a74db900dccd2d534842aa7b6160542d2/src/mcp/server/auth/routes.py#L193-L196)、[server revoke handler](https://github.com/modelcontextprotocol/python-sdk/blob/0921d94a74db900dccd2d534842aa7b6160542d2/src/mcp/server/auth/handlers/revoke.py#L32-L80)。

**对 AC 的影响：** “设备上注销”可验收为本地 token/client_info 清理；“注销必须令远端授权失效”只能对公布并接受 RFC 7009 请求的服务器有条件承诺。若票面要求所有 OAuth-only server 均可远端撤销，则与可选 metadata/服务器能力冲突，应由主开发确认票面语义；本报告不替票面改范围。

## 成熟客户端机制对比

以下每项均于 2026-10-11 查看所列官方产品文档。文档未公开 token 后端/协议调用时，不推测产品内部实现。判定只作设计参考，不代表复制产品专属服务或源代码。

### Claude Code

来源：[Claude Code MCP 文档](https://code.claude.com/docs/en/mcp)，核实日期 2026-10-11。

- **发起/回调：** /mcp 或 claude mcp login 发起 OAuth；支持 DCR/CIMD 自动流程和预注册 client。callback 默认本机端口，可设固定端口。无浏览器时显示 URL；用户在浏览器授权后将完整 redirect URL 粘回交互终端。
- **存储/刷新/重登：** 文档称 token 安全存储并自动刷新；refresh token 被拒后提示重登。公开文档未披露 OAuth token 后端。另明确说 client secret 存于 macOS keychain 或 credentials file，不写 config；不能据此推断所有 token 都用同一后端。
- **注销/状态：** claude mcp logout 清除本地存储凭据；移除 remote server 删除本地 token 与 client registration。/mcp 的 Clear authentication 文案称 revoke access，但没有说明是否调用 RFC 7009 endpoint，不能以 UI 措辞证明远端撤销。列表可显示 Connected、Needs authentication、Failed to connect。
- **契合与判定：PORT DESIGN。** 借鉴显式登录、无浏览器回调和可见状态/重登；不照搬 Claude 专属配置/secret store，也不将其 revoke 文案当作远端调用证据。

### Visual Studio Code

来源：[VS Code MCP authorization](https://code.visualstudio.com/api/extension-guides/ai/mcp#authorization)、[VS Code 1.101 authentication UX](https://code.visualstudio.com/updates/v1_101)，核实日期 2026-10-11。

- **发起/回调：** 有 GitHub、Entra 内建 provider，也支持其他 OAuth 2.1/2.0 provider。文档描述先尝试 DCR，再以用户输入静态 client ID/secret 的 client-credential workflow 回退；这是配置凭据，不是 OAuth client_credentials grant。需登记 http://127.0.0.1:33418 与 https://vscode.dev/redirect。provider 文档也提到 code/device code flow，具体可用性依赖 AS。
- **存储/刷新：** 官方 MCP 文档本次未找到 token storage backend 或 refresh 持久化细节。
- **注销/状态：** Accounts 菜单显示认证状态；Authentication: Remove Dynamic Authentication Providers 清理动态 client ID 与该 provider 关联数据。另有 GitHub/Entra 专属 Manage Trusted MCP Servers。通用 sign-out/remove 是否调用远端 RFC 7009 未说明。
- **契合与判定：PORT DESIGN。** 借鉴状态入口和清理动态 registration 的用户动作；新流程按当前规范优先 CIMD，并把静态 client 作为预注册配置，不能照搬旧 DCR-first 顺序或假定远端撤销。

### Zed

来源：[Zed MCP 文档](https://zed.dev/docs/ai/mcp)，核实日期 2026-10-11。

- **发起/回调：** 远程 MCP 未配 Authorization header 时，Zed 提示使用标准 MCP OAuth flow。公开页面未说明浏览器 callback、client registration 或无浏览器方式。
- **存储/刷新/注销：** 本次官方文档未披露 token 安全存储、refresh、重新登录、logout 或 RFC 7009 机制。
- **状态：** Settings → AI → MCP Servers 用状态点展示运行状态；绿点 tooltip 为 Server is active，其他颜色与 tooltip 表示不同状态。
- **契合与判定：PORT DESIGN。** 借鉴按需授权和简单状态反馈，不猜测未公开的 token lifecycle。

### Goose

来源：[Goose Using Extensions](https://goose.vcorp.ai/docs/getting-started/using-extensions/)，核实日期 2026-10-11。

- **发起/注册/回调：** remote Streamable HTTP extension 通常通过 CIMD 或 DCR 自动取得 client ID；均不支持时可配预注册 client ID。callback 监听 127.0.0.1 临时端口；需预注册固定 URI 时可设 GOOSE_OAUTH_CALLBACK_PORT。
- **密钥/Scope：** confidential client 的 secret 以 secret key 名出现在配置，实际值从环境或 Goose secret store 获取；public client 可依 PKCE 不配 secret。未配置 scopes 时从服务器 metadata 选择，文档提醒其可能宽于 extension 需求。
- **refresh/重登/撤销/状态：** 本次官方页面未找到 token 持久化、refresh、OAuth logout/远端 revoke 或 OAuth 专属状态定义。enable/disable extension 不等于 logout/revoke。
- **契合与判定：PORT DESIGN。** 借鉴 loopback callback 与“配置只存 secret key 引用”；scope 仍按 MCP challenge/最小权限设计，不能无条件照搬 metadata 所有 scope。

### Cline

来源：[Cline MCP overview / CLI wizard](https://github.com/cline/cline/blob/main/docs/mcp/mcp-overview.mdx#L235-L259)，核实日期 2026-10-11。

- **发起/状态管理：** cline mcp wizard 可列 server 与 enabled/disabled 状态；Authorize OAuth 动作可运行或重跑远程 server 的 browser authorization；也可编辑、启停和删除 server。
- **回调/存储/刷新/撤销：** 该官方页面未找到 DCR/CIMD 选择、callback 实现、token storage、refresh、logout 或 RFC 7009 说明。删除 server 不能证明 token 已本地清除或远端撤销。
- **契合与判定：PORT DESIGN。** 借鉴显式 OAuth 授权/重授权入口和 server 状态管理；协议安全仍使用 SDK，不从向导界面推断底层实现。

### Codex CLI

来源：[OpenAI MCP / Codex CLI 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)，核实日期 2026-10-11。

- **发起/回调/注册：** codex mcp login 启动授权；127.0.0.1 callback 可变端口，可覆写端口/URL；预注册 client 流程展示完整 callback。根据 AS metadata 自动选择 CIMD，否则 DCR；预设 client ID 跳过注册。code 兑换前验证返回的 iss；AS 宣告支持 issuer 时，缺 iss 也失败。优先使用 server advertised scopes。
- **存储/refresh/注销：** 文档称配置 auth=oauth 使用 stored MCP OAuth credentials；未说明本地存储后端、refresh 故障 UX、logout 或 RFC 7009。
- **状态和安全边界：** mcp list 显示服务器，TUI 用 /mcp 查看。官方文档允许没有 credential source 时匿名连接，再单独登录；这不符合本票“授权失败不能被当作无认证成功”，不得复制此回退。
- **契合与判定：PORT DESIGN。** 借鉴 callback/iss 绑定、注册选择和状态；不复制 OpenAI 托管 CIMD URL 或匿名降级语义。

### Cursor

来源：[Cursor MCP](https://cursor.com/docs/mcp)、[Cursor CLI MCP](https://cursor.com/docs/cli/mcp)，核实日期 2026-10-11。

- **发起/注册：** 对不适合 DCR 的 provider，可在 mcp.json 指定固定 OAuth client ID、可选 secret 和 scopes。文档建议敏感配置用环境变量插值；这不等于 token 安全存储。scope 缺省时文档提到从授权服务器 metadata 发现。
- **回调：** Web/Agents 使用 Cursor 固定 HTTPS callback；桌面用 http://localhost:8787/callback。CLI agent mcp login 自动处理 callback。
- **状态/refresh/注销：** agent mcp list 显示 connected/disconnected；官方 MCP/CLI 文档未披露 token storage、refresh、logout 或 RFC 7009 机制。
- **契合与判定：PORT DESIGN。** 借鉴预注册 callback、CLI 登录与状态命令；不能以配置文件中的 client secret 推断 token store 设计。

## Windows 凭据大小边界

本仓已有 [Credentials seam](../../src/agent_harness/model/provider_store.py#L67-L85)，但它为 model provider API key 设计：一个字符串对应一条 keyring credential，并未声明 OAuth TokenStorage 可直接使用。Microsoft 文档规定 Windows CredentialBlob 按字节计最大 5×512，即 2560 bytes；不能用字符数代替字节数。keyring 上游 #540 报告约 1280 个 2-byte wchar_t 的实际边界，也指出按 shard 命名会有升级后降级静默读回截断值的风险。issue 中“加密文件放 payload、Credential Manager 放密钥”只是讨论想法，不是 keyring 已实现并支持的方案。来源：[Microsoft 上限](https://learn.microsoft.com/en-us/windows/win32/api/wincred/ns-wincred-credentiala#credentialblobsize)、[keyring #540 与分片风险](https://github.com/jaraco/keyring/issues/540#issuecomment-967354742)。

七个成熟客户端官方材料均未给出 OAuth token 的 Windows 存储容量、JWT 上限或可移植的大对象 credential 编码。因此当前证据支持：

- **ADAPT**：保留凭据抽象 seam，但需验证 SDK TokenStorage 对象映射和安全存储容量；不得默认整个 OAuthToken + client_info JSON 可以放入单条 keyring entry。
- **DEFER**：没有产品官方资料支持“任意大 token 可存”或特定分片/文件方案；实现前应实测各平台后端的字节上限、异常和失败语义，不静默截断、不落明文。
- 这是新增 OAuth storage 的边界，不代表现有 provider API-key store 应在本票内改造。

## 方案依据（供主开发接票）

### 来源机制、契合点与判定

| 来源 | 机制 | 本仓契合点 | 判定 |
|---|---|---|---|
| MCP Authorization 2026-07-28 + RFC 7009/8414 | MCP 传输授权、发现、PKCE、resource、scope；RFC 7009 另定义有条件的 AS 远端 revoke。 | 以当前正式 wire contract 为准；分离标准和产品 logout UX。 | **REUSE** 标准语义；远端 revoke **ADAPT** 为服务器支持时提供，不能普遍保证。 |
| MCP Python SDK 2.1.1，MIT | OAuthClientProvider、HTTPX auth、callback/storage hooks、PKCE、discovery、CIMD/DCR、refresh、scope step-up。 | 同一 Python SDK/Streamable HTTP transport；HTTPX seam 可接 SDK auth。 | **REUSE** wire；**ADAPT** callback 与 TokenStorage。SDK [MIT LICENSE](https://github.com/modelcontextprotocol/python-sdk/blob/0921d94a74db900dccd2d534842aa7b6160542d2/LICENSE)。 |
| Claude Code、VS Code、Zed、Goose、Cline、Codex、Cursor 官方资料 | 登录入口、回调、secret 引用、status/logout UI 各不相同；多家未公开 token backend/revoke。 | 借鉴用户路径和可见状态，不能当成同一底层实现；没有复制其代码。 | **PORT DESIGN**；仅实现本项目必要的回调/状态 glue 时才 **BUILD**。 |
| Microsoft Credential Manager + keyring #540 | 单个 credential blob 有字节上限；历史分片被指出有降级静默截断风险。 | 现有 keyring seam 每条 credential 是单一字符串；OAuth cache 比 API key 大且结构不同。 | **ADAPT** seam；具体大对象存法没有一手成熟方案前 **DEFER**。 |

### 设计边界建议

- 将 OAuth 作为 HTTP MCP client 的 auth provider 接入；工具注册、Permission、Scheduler、Executor、Ledger、ToolResult/SessionEvent 保持原路径。
- 明确未登录/需授权、授权失败/用户拒绝、授权成功三种结果；失败不能靠移除 Authorization 的匿名重试“恢复”。
- 用户路径需能触发 login、查看 connected/needs-auth/failed、在 refresh 失效或 scope 增量时重新授权；持久化要覆盖 token、client registration 信息、跨进程恢复与本地清除。
- 明确 logout 文案：local sign-out = 清除本地 token/client_info；remote revoke = 可信 endpoint 存在时按 RFC 7009 请求并报告服务器结果。endpoint 不存在或请求失败时，不能显示“服务端授权已撤销”。
- 不引入自制 OAuth 流程、HTTP 头 token passthrough、匿名回退或自创 token exchange；SDK OAuthClientProvider 是 wire 优先实现。只对产品 glue 和 SDK 缺少的、有条件 remote revoke 请求评估最小补充。
- Windows 存储需检查序列化字节数和真实后端写入失败路径；字符数、JWT 长度猜测和 issue 中 workaround 都不能代替安全边界验证。

## 尚待确认

1. #879 的“撤销”是清除本地登录材料，还是要求服务端 RFC 7009 revoke？两者语义和服务器依赖不同；证据不足以替主开发改 AC。
2. OAuth-only server 的现实兼容范围需用实际服务端验收 CIMD、DCR、预注册、callback、PKCE metadata；锁定 SDK 版本本身不能证明生态互通。
3. 当前没有可从成熟客户端官方资料直接复用的跨 Windows/macOS/Linux token store 方案；Windows keyring 的 blob 大小尤其要验证。
4. 产品官方文档没披露 revoke 请求时，本报告只将其 credential clearing 视为本地语义，除非官方来源或代码能证明 RFC 7009 调用。
