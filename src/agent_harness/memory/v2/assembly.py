"""#298 / MEM-V2-2 T7b：把配置面变成一条可用的记忆形成管线（装配层）。

`runner.MemoryJobRunner` 要六样东西（作业库、会话日志、执行器、模型角色、并发上限、
事件出口），本模块是**唯一**把它们拼起来的地方。三个决定收在这里。

# 决定一：闸门 = 主模型角色解析不出就不装配

`executor.run` 在 `roles.primary is None` 时**每个 job 都**降级成 `no_primary_model`，
而每次降级都会给用户发一条 `memory/degraded`（R12：终态失败必须可见）。所以"没配模型"
这件事必须在**装配期**挡住，而不是让它在运行期每轮响一次：

- 没有主角色 ⇒ 返回 `None`，这个部署没有这条管线（一条日志说清原因）；
- **只有主角色**是正常的：R9 的重试阶梯允许"没有备用"（`roles.has_fallback` 决定备用那一段
  跑不跑），所以 `fallback is None` **不是**闸门。

形状与 `capability/wiring._wire_memory` 对"记忆组件没配齐"的处理一致：装配期降级
（缺席 + 一行日志）优于运行期每次报错。

# 决定二：作业表与记录表**同一个库文件**（`<workspace_dir>/memory-v2.db`）

AC6/AC7 要的"终态与副作用一起落地"要求 job 终态、记忆记录、outbox 三者**同一个 SQLite
事务**（`jobs.SqliteMemoryV2JobStore.commit_with_outcome` 是那笔事务的唯一入口）。分库会
把这条性质变成分布式事务——而"记忆写进去了、job 还停在 FORMING"这种中间态正是恢复扫描
重新跑一遍的输入。库位沿用 V1 记忆记录的约定（`factories` 用 `memory.db`），同一目录下
并排一等文件，不与 `harness.db`（harness 自己的三 Store）混。

两个 store 各自 `initialize()`（幂等建表），先后无关；都在同一次装配里做完，所以"表还没建
就有 job 进来的"这个窗口不存在。

# 决定三：**进程级单例**，不是每轮新建

runner 持有服务循环（`_pump` task）、`Semaphore` 与脏标记，它们描述的是"**这个进程**在跑
记忆作业"。而 `assembly.build_runtime` 是**每轮**调用（web 每次 run 建一个 `AgentRuntime`）
——在那里新建 runner 等于每轮起一条服务循环、把上一条留在后台，并发上限变成"每轮 4 个"。
所以 runner 只在装配期建一次（web 的 `AppState.get_wiring` once 语义 / CLI 每进程一次），
`AgentRuntime` 每轮只是把它接在终结臂上（`memory_formation=`），见 `runtime._notify_memory_formation`。

顺序上先 `recover()` 再返回：恢复扫描只做"把库里未终结的 job 排进服务循环"，不阻塞启动
（真正的执行在后台任务里），但它必须在**有任何新 run 之前**发生——否则一个重启后残留的
job 会排在这一轮新 job 后面，而按用户串行的 `claim` 会把它挡到更久。

# 已知缺口：派生索引还没有生产驱动（如实登记，别当成已经通了）

执行器的检索（R2 的"最多十条相似 active 记忆"）走 `MemoryV2Service.search` → 索引。
索引由 `index.MemoryV2IndexRelay.flush()` 从 SQLite outbox 收敛，而**全仓只有用例调用过
`flush()`**：本票不引入它的驱动（周期循环或按 job 触发都是新机制，归属召回那一票）。
后果说清楚：接线之后，**生产**路径上那份"相似记忆"结构性地为空 ⇒ 裁决期看不到既有记忆 ⇒
模型只能一路 ADD。R2 的**字面**要求仍然成立（0 ≤ 10，每条上限亦然），受影响的只是更新/去重
的质量面。这一条已登记在 T8 的缺口清单里。
"""

from __future__ import annotations

import logging
from pathlib import Path

from agent_harness.config import Settings
from agent_harness.memory.v2.capability import MemoryV2Service
from agent_harness.memory.v2.executor import MemoryJobExecutor
from agent_harness.memory.v2.index import InMemoryMemoryV2Index
from agent_harness.memory.v2.jobs import SqliteMemoryV2JobStore
from agent_harness.memory.v2.roles import (
    MEMORY_PRIMARY_ALIAS,
    MEMORY_PRIMARY_PROVIDER,
    resolve_memory_roles,
)
from agent_harness.memory.v2.runner import ChatModelInvoker, MemoryJobRunner
from agent_harness.memory.v2.store import SqliteMemoryV2Store
from agent_harness.session.store import JsonlSessionStore

logger = logging.getLogger(__name__)

#: V2 的 SQLite 库位（相对 `settings.workspace_dir`）。见模块 docstring 的决定二。
MEMORY_V2_DATABASE_NAME = "memory-v2.db"


async def build_memory_formation(
    settings: Settings, *, sessions: JsonlSessionStore,
) -> MemoryJobRunner | None:
    """装配记忆形成管线；主模型角色解析不出时返回 `None`（= 这个部署没有这条管线）。

    `sessions` 由调用方注入而**不在这里按约定新建**：执行一个 job 要按
    `(session_id, run_id)` 从**运行时空时正在写的那个**会话日志里切事件，而日志根目录
    在 web（`AppState.store`）与 CLI（`workspace_root/sessions`）各有一处构造。在这里
    再按 `Path(settings.workspace_dir)/"sessions"` 建一份，就是路径约定抄了第二遍——
    一旦两处分叉，症状是"job 永远切片为空 ⇒ 每轮降级"，没有任何东西报路径不一致。
    """
    roles = resolve_memory_roles(settings)
    if roles.primary is None:
        # 不打印模型名 / 端点 / 凭据（roles 模块的同一约束）；别名与 provider 名是
        # PRD §5.3.3 公开固定的映射，写出来是为了这句话可执行。
        logger.warning(
            "记忆形成未装配：模型角色 %s 解析不出 provider '%s'。配 AGENT_MODELS 里一条该 "
            "provider 的条目、或把 MODEL_PROVIDER 设为它即可启用；本进程不会形成任何 V2 记忆。",
            MEMORY_PRIMARY_ALIAS, MEMORY_PRIMARY_PROVIDER,
        )
        return None

    database = Path(settings.workspace_dir) / MEMORY_V2_DATABASE_NAME
    store = SqliteMemoryV2Store(database)
    jobs = SqliteMemoryV2JobStore(database)
    await store.initialize()
    await jobs.initialize()

    # 组合实现同时满足两个执行器端口（写者 `create_in/update_in/invalidate_in`、
    # 检索者 `search`）——一个对象，所以"写进去的"与"检索到的"不可能是两套可见性规则。
    service = MemoryV2Service(store, InMemoryMemoryV2Index())
    executor = MemoryJobExecutor(
        jobs=jobs, writer=service, searcher=service, invoker=ChatModelInvoker(),
    )
    runner = MemoryJobRunner(
        jobs=jobs, sessions=sessions, executor=executor, roles=roles,
        max_concurrency=settings.memory_v2_max_concurrency,
    )
    recovered = await runner.recover()
    logger.info(
        "记忆形成已装配：主角色 %s%s、并发上限 %d、启动恢复 %d 条待办",
        MEMORY_PRIMARY_ALIAS,
        "" if roles.has_fallback else "（无备用角色，R9 的备用阶梯不生效）",
        runner.max_concurrency, recovered,
    )
    return runner
