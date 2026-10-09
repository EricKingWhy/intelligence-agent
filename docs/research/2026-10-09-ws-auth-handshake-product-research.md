# #890 `/api/ws` 握手鉴权调研（成熟产品一手来源）

调研日期：2026-10-09。核对树：本票 worktree HEAD `8c210b069bfb16eb43f86b9b81a9552691e5aa77`
（基线 `origin/main` = `53585080761171f84a045948263d044448dac554`）。
判据口径：`SPEC_ROOT/13_OPEN_SOURCE_REUSE_MATRIX.md` §1；调研纪律见
`docs/agents/reference-sources.md` §1（代码来源给 `file:line` + commit；第三方笔记先核实）。

## 结论

选 **ADAPT**：照搬"**在连接建立之前**判定凭据与来源、不通过即以协议层拒绝收场"这条机制，
用本项目已有的 `AuthSeamMiddleware` + `Settings.jwt_secret` + `require_trusted_origin`
的判据实现，不引第三方库、不加新依赖。

三家独立实现给的是**同一条形状**，且"拒"的落点都在 accept/握手之前：

| 来源 | 判定点 | 拒的落点 |
| --- | --- | --- |
| Django Channels | `OriginValidator.__call__`：把 scope 交给 application **之前** | `WebsocketDenier` 的 `connect()` → `await self.close()`（accept 前 close） |
| Phoenix | `check_origin`：WebSocket 升级请求的 plug 管线里，**建 socket 之前** | `resp(conn, :forbidden, "") |> halt()`（HTTP 403，不进 socket） |
| Socket.IO | `io.use(...)` 中间件：连接建立**之前** | `next(new Error(...))` → 客户端收 `connect_error`，连接被拒 |

本项目 HTTP 面已经是 fail-closed（配置 `jwt_secret` ⇒ 无 Bearer 401），故 WS 侧沿用
**同一份凭据判定**（同一个类里的 `_resolve_identity`），只在传输层出口上分叉：
HTTP 用 401 JSON 响应，WS 用 `websocket.close`（未 accept）——uvicorn 对握手前的 close
固定回 403。

## 浏览器凭据通道：`Sec-WebSocket-Protocol` 子协议（#890 P1-1）

独立审查指出：只认 `Authorization` 头等于把 `/api/ws` 变成"只有能塞裸 HTTP 头的客户端才
连得上"，而浏览器原生 `WebSocket` 构造器**不能**设该头 —— "反向代理 + JWT"这个官方认可
形态下的浏览器客户端因此结构性连不上（HTTP 面 200，唯独 live 通道 403）。用户裁决按
**Kubernetes 的做法**补一条浏览器可用的凭据通道：

- 来源：k8s commit `714f97d7baf4975ad3aa47735a868a81a984d1f0`
  `staging/src/k8s.io/apiserver/pkg/authentication/request/websocket/protocol.go`。
- 形状：子协议值 `base64url.bearer.authorization.<ns>.<base64url 无 padding 的 token>`；
  本项目命名空间取 `agent-harness`（k8s 用 `k8s.io`）。
- 为什么 base64url 无 padding：子协议值必须是 RFC 6455 的合法 HTTP token 字符，
  padding 的 `=` 会被浏览器直接拒发。
- 关键细节照搬：校验成功后**剥离该子协议、101 响应不回显**（防泄漏）；因浏览器请求了
  子协议就必须收到一个回显，客户端另带一个业务子协议（本项目 `agent-harness.v1`）。
- 两种来源**等价**：桌面外壳 loopback 代理注入的 `Bearer` 头与子协议通道任一通过即放行。

前端对侧在 `web/src/lib/wsStream.ts`（token 取自与 HTTP 面同一处 `auth.getToken()`）。

## 取舍（本项目为什么这样适配）

1. **认证与来源分属两态，不叠加成"与"**：
   - 配置 `jwt_secret` ⇒ 唯一凭据是 Bearer，**不看 Origin**（与 HTTP 面
     `projects.require_trusted_origin` 的 `if state.settings.jwt_secret: return` 同款）。
     理由：本机任意进程都能伪造 `Origin: http://localhost`，把它当边界是**假边界**；
     桌面形态下外壳 loopback 代理正是靠注入 Bearer 过闸（#891 追认的是代理侧的调用方校验）。
   - 未配置 `jwt_secret` ⇒ 本地信任模式，此时来源闸是**唯一**的浏览器面防线，
     与 HTTP 面同判据同常量。
2. **"同源"用 hostname 白名单而不是 Phoenix 的 `:conn` 严格相等**：本项目 HTTP 面
   既有 `_LOCAL_HOSTNAMES = {localhost, 127.0.0.1, ::1}`（ADR-0025 D1 的 (b)），
   WS 必须复用同一份——`localhost` 与 `127.0.0.1` 在开发形态互换是既成事实，
   收紧成 `:conn` 会让 Vite dev（5173 → 代理到 8000）整体失效。
3. **无 Origin = 放行**（Phoenix 口径）：第三方网页**构造不出**不带 Origin 的浏览器握手，故这条放行不构成
   drive-by 面；反过来若按 Channels 的 `None ⇒ False` 拒绝，会打断 CLI / curl /
   本机脚本这条真实使用路径。
   ⚠ **这一条是相对改动前的行为变更**，不是"零影响"：base 对**任意** Origin 的 WS 握手
   一律放行，现在跨源被拒（受影响旧用法：从 `file://` / sandboxed iframe 打开本服务，
   它们的 `Origin` 是 `null`）。票面验收 3 的准确表述是"本地信任模式仍可用；跨源浏览器
   握手由放行改为拒绝"。
4. **拒的落点**：`await send({"type": "websocket.close", ...})` 且**不**先 accept。
   实测（uvicorn 0.52.4 `protocols/websockets/websockets_impl.py:295-303`）：
   握手前的 `websocket.close` 一律以 HTTP **403** 收场、连接不建立 ⇒ 与 Phoenix 的
   403 同形；给出 `code=1008`（policy violation）是为了在 ASGI 层表达语义，
   但**不声称**它到达客户端（HTTP/1.1 403 响应里没有 close code）。
5. **出口的文案形状分叉，策略不分叉**：策略（含 `origin_is_local` 与原因文案）抽成
   `projects.check_trusted_origin` 由 HTTP / WS 共用（#890 P3-4）；两侧只在出口上分叉——
   HTTP 转 403 JSON，WS 转握手前 close。WS 侧的 `Origin` 只进日志且按 `[:120]` 截断
   （客户端可控、无长度上界），close `reason` 用固定短 ASCII（ASGI 限 ≤123 字节可打印
   ASCII，且它到不了客户端）。

## 与既有来源清单的关系（§3.1 偏差披露）

`docs/agents/reference-sources.md` §2 已有「WebSocket / ASGI transport」节，但只索引
RFC 6455 / ASGI 规范 / uvicorn 设置 / `websockets` 内存文档——**鉴权接入面没有条目**。
本次调研按 §3.1「出现新领域/新来源先补本清单」在该节补了 Channels / Phoenix / Socket.IO
三行（机制摘要指向本文）。三家的本地克隆不落盘（本机无 `D:\reference`；本次按需取
单文件正文并记 commit / tag），正文快照留在 `~/refs-890/src/`（本机临时目录，不入库）。

## License

三家均为宽松许可（Channels BSD-3-Clause、Phoenix MIT、Socket.IO MIT）。本票
**未逐字复制**任一上游代码：只取"判定点必须在 accept 之前 + 拒走 close"这条机制，
判据、常量、文案全部来自本项目既有实现（`AuthSeamMiddleware` / `_LOCAL_HOSTNAMES`），
故不新增 `THIRD_PARTY_NOTICES` 条目。
