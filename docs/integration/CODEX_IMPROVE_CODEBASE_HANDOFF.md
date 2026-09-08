# 交接单：/improve-codebase-architecture（参考扫描结果）

> **给 Codex。** 下面是 ZCode 在 `D:\intelligence-agent-backend\src\agent_harness\`
> 做过的一次架构扫描发现，作为你的**参考输入**——不是结论，不是指令。
> 请独立执行完整的 `/improve-codebase-architecture` 全仓扫描流程，产出你自己的
> 可视化 HTML 报告和你自己的优先级判断。这份交接单的唯一作用是交叉验证：
> 如果你扫到了我没扫到的东西，说明那是真信号；如果你扫到的东西和我的重叠，
> 说明高置信；如果你没扫到我列的某些项，按你自己的流程判断，不必迁就。

---

## 我做了什么

- 在 `D:\intelligence-agent-backend\src\agent_harness\` 核心模块做了文件级和
  函数级扫描（agent/ capability/ context/ session/ tooling/ web/ storage/ model/
  multiagent/）
- 用 "deep module" 词汇找问题：god method/module、shallow wrapper、leaky
  abstraction、duplicated logic、missing seam、untangled concerns
- 基线是 main `83056c8`

## 我扫到的 9 项（仅供参考，不要求采纳）

下面每项附了文件 + 行号，方便你定位时省点时间。但**行号可能在我扫描之后漂移**
——main 还在推进——请以你扫描时的实际代码为准。

### 1. `agent/runtime.py` 的 `_drive` 方法

- 行号：395–1026（方法体）；构造器 255–348
- 我的判断：疑似 god method，混了 streaming / fallback / tracing / checkpoint /
  memory writeback 多个职责；cancel 和 exception 两条终止臂可能重复
- 你来确认：是否真的该拆？拆成什么形状？是否有我看不到的理由它必须是一个函数？

### 2. `web/app.py` 整体

- 行号：全文 ~1269 行
- 我的判断：疑似 god module（AppState + schema + middleware + 15 路由全在
  `create_app` 闭包里）
- 你来确认：是否真的过宽？还是有意为之的单文件部署？

### 3. `session/service.py` 与 web 层的依赖方向

- 行号：44–46（import）、175–202（透传属性）、335（isinstance）
- 我的判断：域服务可能反向依赖了 web 层，边界可能是装饰性的
- 你来确认：这是有意架构妥协还是漏修？

### 4. 路径转义校验重复

- 位置：`web/app.py` 371–418 和 `session/service.py` 683–717
- 我的判断：同一个 PureWindowsPath 安全校验在两层各写了一遍
- 你来确认：是否真的重复？是否有微妙差异是有意的？

### 5. `web/app.py` 的 SSE 序列化

- 位置：`_event_to_sse_dict` 460–486、`_session_event_to_sse_dict` 489–514、
  内联 generator 1186–1189
- 我的判断：三个序列化路径形状可能不一致，疑似潜伏的契约 bug
- 你来确认：这是 bug 还是有意的设计？`/messages` 和 `/sessions` 的 SSE 帧形状
  是否真的不该统一？

### 6. `capability/wiring.py` 的 shim 和 `_wire_*` 重复

- 位置：340–368（三个 ContributesTools shim）、115–449（七个 `_wire_*`）
- 我的判断：三个 shim 逐字节相同；七个 `_wire_*` 结构雷同
- 你来确认：去重是否值得？还是显式重复有利于可读性？

### 7. `AgentRuntime` 构造器

- 位置：255–348，尤其 311–329
- 我的判断：同时接受 `context_builder` 和 `system_prompt` 两个入口说同一件事；
  构造后 mutate builder 内部 list
- 你来确认：双入口是否有历史原因不能删？

### 8. `multiagent/provider.py` 的 `collect_result_fields`

- 位置：58–132
- 我的判断：用字符串匹配 parse tool wire format，知道工具名集合、嵌套 JSON
  结构、中文标记词——疑似 leaky abstraction
- 你来确认：这是否真的该让工具自声明结构化 artifact？

### 9. `storage/sqlite.py`

- 位置：35–484
- 我的判断：三个不相关的 store（OperationLedger / CheckpointStore /
  SessionMetaStore）共享一个文件
- 你来确认：影响是否真的够大值得拆？还是低优先级不碰？

---

## 我没有扫到、你可能要留意的方向

我的扫描可能有盲区。以下是**我没深入看**的地方，你可以自己判断是否需要覆盖：

- `cli.py`（我只看了它对 event 渲染的支持，没做架构扫描）
- `tests/`（测试代码本身的架构通常不在 deepening 范围，但可能有测试可观察性
  的机会——比如某个模块测试特别难写，那本身就说明接口有问题）
- `model/` 目录下的 fallback / config / catalog 逻辑（我扫了 runtime 怎么用
  它们，没扫它们自身的模块形状）
- `observability/`（我只确认了 tracing 在 runtime 里散布，没扫 observability
  包自身是否该加深）
- 前端 `web/src/`（这是 TypeScript，不在本次 Python 扫描范围，但你如果想覆盖
  全栈可以考虑）

---

## 对你的期望

1. **独立全仓扫描**——不要因为我在上面列了 9 项就跳过你自己的发现流程。你的
   扫描应该覆盖面不窄于我，优先级判断也不必和我一致。
2. **产出你自己的 HTML 报告**——`/improve-codebase-architecture` 的标准产物。
3. **和我交叉验证**——在你的报告里标注哪些和我重叠（高置信）、哪些是你独有的
   （新信号）、哪些是我列了但你认为不值得做（低优先级或误判）。
4. **不要迁就我的行号**——main 在推进，行号会漂移，以你扫描时的实际代码为准。

---

## 项目约束提醒

- AGENTS.md §8 Scope Lock：每个 deepening 是独立 ticket，不顺手重构无关代码
- AGENTS.md §9.2 Simplicity First：最小改动，不为通用堆无用抽象
- AGENTS.md §9.3 Surgical Changes：只动必须动的，匹配现有风格
- 不变量 #1–22（AGENTS.md §7）：任何重构不能破坏架构不变量
- 工作目录：`D:\intelligence-agent-backend`（feat/backend）；不要碰 main
