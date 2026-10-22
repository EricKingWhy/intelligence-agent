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

# 检索：FTS5 词法腿，无派生索引后端

#1353 §5 删掉了 dense 语义腿：`MemoryV2Service.hybrid_search` 走 SQLite 的
`memory_v2_fts`，命中随后回 `memory_v2_records` 做身份、项目、active 状态复核。
`InMemoryMemoryV2Index` 仍作为 outbox 投影的进程内落点保留（契约不变、可重建），
但它不再是任何检索的入口。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from agent_harness.config import Settings
from agent_harness.memory.v2.capability import MemoryV2Service
from agent_harness.memory.v2.executor import MemoryJobExecutor
from agent_harness.memory.v2.index import InMemoryMemoryV2Index, MemoryV2IndexRelay
from agent_harness.memory.v2.jobs import SqliteMemoryV2JobStore
from agent_harness.memory.v2.markdown_store import MarkdownMemoryStore
from agent_harness.memory.v2.roles import (
    MEMORY_PRIMARY_ALIAS,
    MemoryModelRoles,
    resolve_configured_memory_roles,
)
from agent_harness.memory.v2.runner import ChatModelInvoker, MemoryJobRunner
from agent_harness.memory.v2.store import SqliteMemoryV2Store
from agent_harness.memory.v2.write_approval import BlockedWriteStore, StagedWriteStore
from agent_harness.observability import get_observability_sink
from agent_harness.session.store import JsonlSessionStore

logger = logging.getLogger(__name__)

#: V2 的 SQLite 库位（相对 `settings.workspace_dir`）。见模块 docstring 的决定二。
MEMORY_V2_DATABASE_NAME = "memory-v2.db"

#: markdown 权威面目录（相对 `settings.workspace_dir`）。#1353 §2。
MEMORIES_DIRECTORY_NAME = "memories"


async def build_memory_v2_service(settings: Settings) -> MemoryV2Service:
    """Build SQLite authority and its derived index independently of the formation model."""
    database = Path(settings.workspace_dir) / MEMORY_V2_DATABASE_NAME
    store = SqliteMemoryV2Store(database)
    await store.initialize()
    await store.purge_expired_tombstones()
    index = InMemoryMemoryV2Index()
    relay = MemoryV2IndexRelay(store, index)
    # #1353 §2：markdown 是权威面（`<workspace_dir>/memories/MEMORY.md` / `USER.md`），
    # SQLite 降为派生/历史层。注入后 create/update/invalidate 先落 markdown 再投影。
    # #1441 §1（F-09）：容量预算从部署配置来（上游同名字段），缺省 2200/1375；
    # 构造参数仍可覆盖，既有测试路径不变。
    markdown = MarkdownMemoryStore(
        Path(settings.workspace_dir) / MEMORIES_DIRECTORY_NAME,
        memory_char_limit=settings.memory_char_limit,
        user_char_limit=settings.user_char_limit,
    )
    markdown.load_from_disk()
    # #1353 M6：待审区与 job/record 同一个库文件（决定二）；与 markdown 同生共死。
    staged = StagedWriteStore(database, markdown)
    await staged.initialize()
    # #1461：被审批门拦下的后台写入。同一库文件（与 staged 同生共死），writer 就是本
    # service——promote 用 executor 那套 `*_in` 在它自己的事务里重放。
    service = MemoryV2Service(store, index, relay=relay, markdown=markdown, staged=staged)
    blocked = BlockedWriteStore(database, service)
    await blocked.initialize()
    # 先有 service 才能给它自己当 writer，所以接在构造之后（`blocked` 只读一次）。
    service.attach_blocked(blocked)
    return service


async def build_memory_formation(
    settings: Settings, *, sessions: JsonlSessionStore,
    workspace_index: Any | None = None,
    memory_v2: MemoryV2Service | None = None,
    roles: MemoryModelRoles | None = None,
) -> MemoryJobRunner | None:
    """装配记忆形成管线；主模型角色解析不出时返回 `None`（= 这个部署没有这条管线）。

    `sessions` 由调用方注入而**不在这里按约定新建**：执行一个 job 要按
    `(session_id, run_id)` 从**运行时空时正在写的那个**会话日志里切事件，而日志根目录
    在 web（`AppState.store`）与 CLI（`workspace_root/sessions`）各有一处构造。在这里
    再按 `Path(settings.workspace_dir)/"sessions"` 建一份，就是路径约定抄了第二遍——
    一旦两处分叉，症状是"job 永远切片为空 ⇒ 每轮降级"，没有任何东西报路径不一致。
    """
    if roles is None:
        # #1039：角色映射可指向 #203 自定义供应商（`provider:model` 命名空间）⇒ 解析时
        # 带 store。装配层通常由 wiring 显式传入 roles（同一入口解析），这一支是直调方
        # 的便捷回退，与生产路径同一语义。
        roles = resolve_configured_memory_roles(settings)
    if roles.primary is None:
        # 不打印模型名 / 端点 / 凭据（roles 模块的同一约束）；别名与**当前配置的** provider
        # 名是诊断所需（#1039：运营者可改指别的 provider，打印常量会误导排查）。
        logger.warning(
            "记忆形成未装配：模型角色 %s 解析不出 provider '%s'。配 AGENT_MODELS 里一条该 "
            "provider 的条目、或把 MODEL_PROVIDER 设为它即可启用；本进程不会形成任何 V2 记忆。",
            MEMORY_PRIMARY_ALIAS, settings.memory_primary_provider,
        )
        return None

    database = Path(settings.workspace_dir) / MEMORY_V2_DATABASE_NAME
    jobs = SqliteMemoryV2JobStore(database)
    await jobs.initialize()

    # 组合实现同时满足两个执行器端口（写者 `create_in/update_in/invalidate_in`、
    # 检索者 `search`）——一个对象，所以"写进去的"与"检索到的"不可能是两套可见性规则。
    service = memory_v2 or await build_memory_v2_service(settings)
    observability = get_observability_sink(settings)
    executor = MemoryJobExecutor(
        jobs=jobs, writer=service, searcher=service, invoker=ChatModelInvoker(),
        session_store=sessions,
        observer=(observability.memory_observation if observability.enabled else None),
        blocked=service.blocked,
    )
    # 默认值仅作为无 V2 service 调用方的兼容回退。生产 runner 在每个 job 执行时从
    # SQLite 读取可信用户的 extraction_enabled（入队资格）与 fallback_enabled（#1039
    # 降级开关）；两项设置都由 #300 的治理 API 持久化。
    runner = MemoryJobRunner(
        jobs=jobs, sessions=sessions, executor=executor, roles=roles,
        max_concurrency=settings.memory_v2_max_concurrency,
        memory_v2=service,
        workspace_index=workspace_index,
        notification_mode=settings.memory_notification_mode,
    )
    recovered = await runner.recover()
    logger.info(
        "记忆形成已装配：主角色 %s%s、并发上限 %d、启动恢复 %d 条待办",
        MEMORY_PRIMARY_ALIAS,
        "" if roles.has_fallback else "（无备用角色，R9 的备用阶梯不生效）",
        runner.max_concurrency, recovered,
    )
    return runner
