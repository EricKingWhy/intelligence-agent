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
