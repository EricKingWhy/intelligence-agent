# W-19 · Windows 本机/Docker Sandbox 显式选择和缺依赖语义

**目标仓库**：`intelligence-agent-backend` + `intelligence-agent-frontend`；backend 先提供 Sandbox 能力/选择 API，frontend 再显示选择器与缺依赖状态。**类型/优先级**：P1 Windows 使用体验。**依赖**：W-14。**范围**：backend 的 `src/agent_harness/sandbox/local.py`、`src/agent_harness/sandbox/docker.py`、`src/agent_harness/web/app.py` Session 创建 API；frontend 的 Desktop/TUI 选择器。不新增 Docker Engine 管理 Tool。

## 工作指令

创建 Task 时显示现有 LocalSandbox 与 DockerSandbox 能力、宿主读写边界、Docker 是否可用。用户明确选本机时按选定目录与 W-14 权限运行；选 Docker 但 daemon/镜像/映射不可用时任务保持阻塞、显示诊断和可选切换动作，**不得自动改成本机**。模式选择及后续切换留 SessionEvent/可审计事实；在途 Tool 或 UNKNOWN 副作用未结清时不得切换。Docker 缺失不阻止 TUI、桌面、普通本机短任务安装与启动。

**验收**：Docker 在场/不在场、启动后 daemon 消失、Docker 路径绑定失败、本机目录外写入、用户显式切换、恢复后模式一致；在失败情况下没有 Host 上的意外写入。真实 Windows 本机 Sandbox 必跑；Docker 在场时真实容器跑一次，缺席时报告“未测/缺依赖”而非通过。**不做**：Docker Desktop GUI 自动化、Host Engine/Compose 资源管理、静默安全降级。

**成熟参考/复用**：[Docker Windows 前提](https://docs.docker.com/desktop/setup/install/windows-install/)说明 Docker 不能假定每台机器可用；本仓 LocalSandbox/DockerSandbox 与 WorkspaceRegistry `REUSE`，隔离层级提示为最小 `BUILD` UI/API glue。
