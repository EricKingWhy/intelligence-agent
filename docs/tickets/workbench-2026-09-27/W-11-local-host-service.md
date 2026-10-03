# W-11 · 唯一本机 Python 服务与 Desktop/TUI/Web 附着协议

**目标仓库**：`intelligence-agent-backend`（Python 服务端）。**类型/优先级**：P0 Host 契约。**依赖**：[#342](https://github.com/EricKingWhy/intelligence-agent/issues/342) 并发 resume 修复的相关验收。**范围**：现有 `src/agent_harness/web/app.py`、`src/agent_harness/cli.py`、`src/agent_harness/instance_lock.py`、服务启动/健康检查/鉴权；桌面与 TUI 仅消费协议，不各建 Agent Runtime。

## 目标行为

同一个用户数据根同时只有一处 Python 服务持有 `InstanceLock` 和 Session 写入权。桌面、TS TUI、本机 Web 分别连接它；服务未运行时任一桌面/TUI 可冷启动，已运行则只附着，不能因 `cli.py` 旧锁报错而另开第二实例。服务默认绑定 `127.0.0.1` 随机/受管端口，启动者验证进程身份、健康端点及鉴权；普通网页无法仅猜端口控制会话。本地 token/握手材料只通过限权本机通道共享，不进 URL、进度文件、Renderer 普通存储、日志或诊断输出。旧 Web 的远程部署入口若存在，保持独立明确配置，不让个人桌面服务暴露 LAN。

## 工作指令

1. 在现有 `InstanceLock` 前设计“发现并认证附着现有服务→否则竞争启动→二次检查”的竞态闭环；进程死但锁残留时先证明失效，不强抢活服务。两个客户端同时冷启动最多一个 Core。
2. 服务复用原数据根、SessionStore、ArtifactStore、model-providers 与 Windows keyring；迁移 schema 前备份并可回退。禁止以空白新目录冒充成功，特别验证 [#303](https://github.com/EricKingWhy/intelligence-agent/issues/303) 禁止删除的 SessionEvent/Artifact/workspace/凭证。
3. 给 Desktop/TUI/Web 统一 `health/version/capability`、Task 查询、事件重连和鉴权失败语义；协议字段不暴露 secret。断连本身不等于 stop，Task 生命周期由 W-12 决定。

**验收**：Windows 上并发启动桌面+TUI+Web 仍只有一个服务进程/一处写者；旧会话/模型配置/Artifact 可读；过期 token、错用户、LAN 连接失败；服务崩溃后可恢复；迁移失败备份完整且旧版能打开。用真实子进程和 loopback 套接字验证，非单纯 mock。**不做**：多人服务端、分布式锁、#342 的 CAS 修复本身。

**成熟参考/复用**：[DeepSeek Desktop](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/README.md)展示 Web 与桌面共享 Host 数据和受认证连接；其 Node Host 代码不能直接用到 Python。`PORT DESIGN` 附着/鉴权，`REUSE` 本仓 FastAPI/InstanceLock/Store。

## 方案依据（§6.1/§1.3 五字段，2026-10-04 核实）

- **来源（≥2 独立）**：
  1. DeepSeek Harness Desktop `apps/desktop/README.md`（票面自带，2026-10-04 经 GitHub API 读取原文，master@`5badb150`）：桌面与 Web **共享同一 Host 数据与受认证连接**，不各建 Host/Agent Runtime；仓库 LICENSE=MIT（2026-10-03 已核实）。
  2. Jupyter Server 官方文档（原列 reference-sources「待核实候选」，本票接票时自行核实转正，2026-10-04 WebFetch HTTP 200）：`operators/security.html`——"Since access to the Jupyter Server means access to running arbitrary code … Jupyter Server uses a token-based authentication that is **on by default**"；token 启动期生成、随 startup URL 打印到**启动用户的本机终端**（"so that you can copy/paste the URL into your browser"），可随时 `jupyter server list` 取回；携带方式 = `Authorization: token …` / URL 参数 / 登录表单，认证后 cookie 粘性。`other/full-config.html`——`ServerApp.ip` **"Default: 'localhost'"**（即默认只绑 loopback）。诚实记录：security 页本身未明示默认绑定 host（仅以 `localhost`/`127.0.0.1` 启动示例 + 「同主机任意进程可达端口即可连接」的 ZMQ 风险注记间接呈现），默认绑定值以 full-config 页为准。
- **机制摘要**：Jupyter = 单用户本机服务：默认只绑 loopback（`ServerApp.ip='localhost'`）+ token 鉴权默认启用 + token 只经本机通道（启动进程的终端输出 / 同用户的 `jupyter server list`）交付给本机用户；远程暴露需显式改配置。DSH = 桌面/TUI/Web 三方附着同一 Host 进程，连接受认证、数据根共享，客户端不各建运行时。
- **契合点**：与 W-11 目标行为逐条同形——默认 `127.0.0.1` 受管端口 ↔ `ServerApp.ip='localhost'`；token/握手材料只走限权本机通道 ↔ token 只进启动用户终端/凭据面，不进 URL/日志；「普通网页无法仅猜端口控制会话」↔ token 鉴权 on by default；启动者验证健康端点与鉴权 ↔ 客户端先认证再使用；三方附着同一服务 ↔ DSH 单 Host 模型。
- **判定**：附着/发现/冷启动竞态闭环的**语义** = PORT DESIGN（只借「默认 loopback + 鉴权默认启用 + 本机通道交付」，不复制代码；**不借** token-in-URL 便利路径——票面明令 token 不进 URL；不借 cookie 粘性会话模型）。实现 = REUSE `InstanceLock`（OS advisory 锁做竞争仲裁 + 进程内幂等计数，已覆盖「进程死锁自释放」）、`Credentials` seam（ADR-0032 §5 keyring 凭据管理器，测试可注入内存后端）、`AuthSeamMiddleware`（JWT HS256 fail-closed + exp 强制，401 形状已有测试钉住）、FastAPI/uvicorn 与既有 `/api/health`；BUILD 仅限本仓没有的面：服务端点发现状态文件、附着核验分类（ABSENT/STALE/AUTH_FAILED/ATTACHABLE）、`serve` 冷启动二次检查闭环、CLI `serve` 子命令。远程部署入口保持既有显式配置路径不变（`create_prod_app` + 显式 jwt_secret），不与个人桌面服务混线。
- **License**：DSH=MIT（本票不复制其代码）；Jupyter Server 文档为官方文档参考（Jupyter 项目 BSD，无代码复制）；keyring=MIT（既有依赖，不新增第三方依赖）。

