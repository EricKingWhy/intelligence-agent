# ADR-0050 — local sandbox 进程树回收与配额边界：回收进单一路径、盲区如实声明、配额 DEFER

- **Status**: Accepted（#549 拆分裁决由用户批准，2026-10-03；-c 方向 (ii) 文档化 DEFER 同日拍板）
- **Date**: 2026-10-03
- **Deciders**: 用户（#549 四子票拆分 + #549-c 选 (ii)）+ 本 Agent（实现口径）
- **Related**:
  - 票面 **#549**（SB-01/02/03/04/05/07 + MCP-F5 同根因家族；audit 2026-10-03 纠偏块）
  - **ADR-0027**（权限档从来不是路径围墙——本 ADR 的 Security 前提与其同源）
  - 规格参照：`05_SANDBOX_CODING_TOOLS.md`（spec 冻结，本 ADR 记录 local 后端的
    进程边界语义与**不**做配额的产品决定，不回写规格）
  - 代码落点：`agent_harness/sandbox/local.py`（`exec` 正常返回回收 + 模块 docstring
    边界声明）、`agent_harness/sandbox/base.py`
  - 已核实来源（2026-10-03，一手）：[tini README](https://github.com/krallin/tini)
    （PID 1/subreaper 收养 reap、`-g` 才杀整组——进程组是树回收的作用域单位）、
    [Docker run 参考](https://docs.docker.com/reference/cli/docker/container/run/)
    （`--init`/`--memory`/`--cpus`/`--pids-limit`）、
    [Windows Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
    （子进程默认入 job、`TerminateJobObject`、`KILL_ON_JOB_CLOSE`、breakaway 标志）

---

## 1. Context

测试报告 SB 族（§1.5 上线前 P0 第 6/7 项 + 中低档）指出 local sandbox 的进程/资源
边界停留在「进程组 + 管道」层。2026-10-03 audit 纠偏：七类问题不能当一张原子
tracer 票验收；ADR-0027 已明确 Local 不是路径硬围墙（SB-04 的权限默认归 #358 W-14）；
`docker --init` 负责 reap 僵尸、不保证杀掉仍活跃 detached 进程；RLIMIT 是 POSIX
进程限制、不是 Windows 全树隔离。用户批准拆四张：-a 进程树回收（SB-01+SB-03）、
-b 错误映射（SB-07）、-c 资源配额（SB-05）、-d 权限默认（并入 #358）。

## 2. Decision

### D1 — 正常返回路径纳入整组回收（-a，已随 `a7d72ad8` 落地）

`exec()` 的三条退出路径里，超时/取消早已 `_kill_process_tree` 整组击杀；**正常
`wait()` 返回**此前零回收——shell 退出后其后台孙进程（`sleep 78 & echo done` 形态）
被 init 收养、永久泄漏（SB-01，POSIX xval 实锤），并攥住管道写端让 exec 晚
~2×`join(5)`≈10s 才返回（SB-03）。决定：正常返回路径补**一次幂等
`killpg(pgid, SIGKILL)`**（`_reclaim_process_group`）——正常命令树已空时是
`ProcessLookupError`（无害），有残留则整组击杀、reader 立刻 EOF。Windows 不需要
该修复：cmd 无 shell 级后台（`&` 是顺序执行，SB-01 Windows 未复现的机理），且
既有 Job Object（`KILL_ON_JOB_CLOSE`，0x2000）在句柄关闭时兜底整树终止。

### D2 — setsid/killpg 盲区如实声明，不以代码追（-a 的文档半边）

SB-02（`setsid sleep 73 …` 脱组逃逸超时击杀）是 `killpg` 作用域的**固有边界**：
进程组是本次修复的作用域单位（tini 同理：默认只信号直接子进程、`-g` 才杀整组），
组外进程本来就不可达。追平它需要 cgroup/namespace 级追踪——那是容器运行时的
职责，不是 local 后端在 Host 上重造的能力（Scope Lock：不为未来可能性造抽象）。
决定：**文档声明**盲区 + 生产部署建议强制 DockerSandbox（或配置层 warning，
随 #358 权限默认一并考虑）。本仓绝不在真实 Host 上跑 fork 炸弹/setsid 探针做
"验证"——有界探针（后台 sleep）封顶。

### D3 — 资源配额 DEFER：local 无配额是**已声明的边界**，不是缺陷（-c，用户拍板 (ii)）

local 后端**不做**内存/CPU/外联配额（SB-05）：`_CappedCapture` 只防输出 OOM，
进程内存、CPU 时间、网络外联均无上限。理由：

1. 配额的成熟解法在容器层（Docker `--memory`/`--cpus`/`--pids-limit`；
   `--pids-limit` 同时是 fork 炸弹的容器级保险），microVM/托管执行更彻底——
   在 Host 的 Python 进程里用 RLIMIT 造半吊子隔离，可靠性不如把部署边界抬高；
2. RLIMIT 是 POSIX 机制，Windows 无等价物（audit 纠偏）；Windows 原生的
   Job Objects 配额（`JobMemoryLimit`/CPU rate control）可以补进程树级上限，
   但那是未来选项（DEFER），不是本批承诺；
3. 用户拍板 (ii)：文档声明「local 无配额，生产用 DockerSandbox」，配额实现 DEFER。

声明落点：`sandbox/local.py` 模块 docstring（代码看不出的操作约束写注释）+
本 ADR。**验收语义**：任何依赖"local 沙箱能限制资源消耗"的部署结论都是误用；
资源不可信工作负载必须走 DockerSandbox。

### D4 — 与权限默认的关系

SB-04/MCP-F5（web 默认 auto-approve 放行 DANGER bash）是**权限默认**问题，
归 #358 W-14 统一裁决；本 ADR 不替它决定。ADR-0027 的结论在此重申：权限档不是
路径围墙，沙箱边界与审批轴正交——任何一侧都不构成另一侧的替代。

## 3. Consequences

- 进程树泄漏与 SB-03 延迟在 POSIX 正常路径闭合；回归探针为 POSIX-gated
  （`tests/sandbox/test_exec_hardening.py`），POSIX 绿由 CI/xval 确认。
- setsid 脱组逃逸是**已声明的能力边界**（D2），不再作为 SB-02 的未修缺陷跟踪；
  生产不可信负载走 DockerSandbox。
- local 无资源配额是**已声明的边界**（D3）；#549-c 以文档闭环，不再作为
  SB-05 的未修缺陷跟踪；配额实现挂 DEFER（未来可走 RLIMIT + Job Objects 双平台）。
- 事件/账本/恢复语义零改动：本 ADR 不触碰 SessionEvent、Operation Ledger、
  Reconcile 合同（ADR-0047/#547）。
