# ADR-0010: Capability Registry 与 Plugin Config

**Status**: Accepted
**Date**: 2026-09-05
**Phase**: 7 (Capability / Plugin Foundation + Skills)
**依据**: spec 08（全文）、13 §3（Capability seam PORT DESIGN）、13 §5（Registry 属 BUILD）、14 Phase 7 交付与 Gate

## Context

Phase 6 已有一条竖切 Capability seam（`MemoryCapability` Protocol → `LangMemMemoryCapability` Provider → `MemoryContextProvider` Consumer），但它是点对点接线：Web 装配代码直接 import 具体 Provider 类。Phase 7 要把这条 seam 泛化成通用机制，使"新增领域能力（如 Finance）不修改 Agent Core"成为结构保证（spec 08 §1、§8）。同时必须守住：插件不能绕过 Tool Permission / Operation Ledger（08 §9）、Optional 故障降级（08 §7）、Consumer 使用前检查 descriptor（08 §5）。

## Self-grill 决策

### Q1：Registry 的接口形状？
**决策**：命名 Registry，五个方法：`register(descriptor, provider)` / `get(name)`（缺失抛 `CapabilityError("not_found")`）/ `optional(name)`（缺失返回 None → OPTIONAL 降级路径）/ `descriptor(name)` / `available()`。重复注册同名抛错（不做静默覆盖——静默覆盖正是 08 §5 禁止的"接受但忽略"的温床）。
**理由**：08 §3"Provider 可多实例共存时使用命名 Registry"；V1 显式注册（08 §6），不做 entry-point 扫描、不做 Marketplace。
**后果**：Provider 实例化发生在注册前（factory 函数），Registry 本身零生命周期魔法。

### Q2：Descriptor 字段与校验时机？
**决策**：Pydantic 模型，字段严格取 08 §5 清单：`name / version / provider_name / capabilities[] / risk / supports_streaming / supports_recovery / supports_concurrency / config_schema`，外加两个本项目必需位：`degradation`（REQUIRED_CORE / OPTIONAL_RUNTIME / OPTIONAL_OBSERVABILITY，08 §7 三分类）与 `enabled`。提供 `supports(capability: str) -> bool`；Consumer 使用前 MUST 检查，不支持时抛 `CapabilityError("unsupported")`——显式报错，绝不静默忽略（08 §5 原文）。
**理由**：字段清单是规格原文，不自创；`degradation` 是 08 §7 分类的载体，`enabled` 是"按配置启停"（08 §9 验收 3）的载体。
**后果**：health/availability 字段 08 标注"按需"——V1 不做健康检查轮询，DEFER。

### Q3：错误词汇表？
**决策**：`CapabilityError(RuntimeError)` + 四个码：`not_found`（注册表无此能力）、`unsupported`（有此能力但 descriptor 不支持所需子能力）、`disabled`（配置显式停用）、`init_failed`（装配期失败：factory 构造失败 / 注册冲突 / config 非法）。全部显式抛出。另：`unsupported` 在 V1 是保留码——唯一 Consumer 由显式装配选定，还没有按子能力在多 Provider 间挑选的场景，Consumer 侧检查随多 Provider 选择一起落地（DEFER）。
**理由**：08 §2 要求 error vocabulary；四个码恰好覆盖 §9 验收的 3/4/5 三条失败形态。

### Q4：Plugin Config 的来源与形状？
**决策**：Settings 新增 `CAPABILITIES` 环境变量（JSON 字符串），结构 `{"<capability_name>": {"provider": "<provider_name>", "enabled": bool, "options": {...}}}`，经 Pydantic TypeAdapter 校验为 `CapabilityConfigMap`。装配函数 `wire_capabilities(registry, config, tool_registry, context_providers)` 按配置驱动 builtin factory。配置缺省 = `{}` = 只有 REQUIRED_CORE 检查，零行为变化。
**理由**：本项目配置一贯走 .env + pydantic（config.py），不引入第二套 YAML 配置文件；08 §6 的 YAML 是"示例形态"，其本质要求是"显式配置加载"——JSON env 满足同一本质且零新依赖。
**后果**：`config_schema` 字段 V1 只做声明与文档（消费方 `options` 直接透传 factory），不做 JSON Schema 运行时校验——DEFER，等第二个真实 Consumer 出现再收紧。

### Q5：builtin factory 注册哪些？
**决策**：V1 只登记两个 factory：(1) `memory` → `LangMemMemoryCapability`（需 `[memory]` extra + 配置齐，同 Phase 6 Web 装配逻辑，迁入 factory 后 `_build_runtime` 不再直接 import 具体 Provider 类——这正是 08 §9 验收 1"切换 Memory Provider 不改 Core"的落点）；(2) `demo` → `TickerCapability`（Gate 用最小 demo：一个 `tick` 工具贡献进 ToolRegistry，走统一 Executor）。Artifact 不迁——ArtifactStore ABC 已是 seam 且 Phase 5 Gate 稳定，改动收益为零（Scope Lock）。
**理由**：08 §9 验收 1 需要 memory 走 Registry 证明"换 Provider 不改 Core"；Gate"新增 demo capability 不改 Agent Loop"需要 demo 实证。

### Q6：Capability 如何贡献 Tool / ContextProvider？
**决策**：不定义强制基类。贡献走**装配侧约定**：`wire_capabilities` 内置对已知 capability 的接线（memory → MemoryContextProvider + memory_writer；demo → 其 tools）。Capability 包提供 `ContributesTools` / `ContributesContextProviders` 两个可选 Protocol，实现了的 provider 由装配函数统一收集，工具一律进 ToolRegistry（统一 Executor 路径，08 §9 验收 6）。
**理由**：08 §4 列了五种 Consumer，强制单一贡献接口会造出"万能 Provider"假抽象；V1 只有 2-3 个接线点，显式装配比反射式发现更可读（08 §6：不要一开始实现复杂机制）。
**后果**：新增 capability 的成本 = 写 factory + 在 wire_capabilities 加一段显式接线（改的是装配层，不是 Agent Loop——满足 Gate 1 的字面与精神）。

## Consequences

- `AgentRuntime`（Agent Loop）零改动——Gate 1 的结构保证。
- Web `_build_runtime` 的 Memory 装配代码改为走 registry；REQUIRED_CORE 缺失在装配期显式失败，OPTIONAL 缺失返回 None 降级（与 Phase 6 行为一致）。
- Registry 是进程内单例（挂在 AppState），无跨进程语义——DEFER。

## 补充（T5/#62，2026-09-05）：装配期 factory 失败的降级语义

`_BUILTIN_WIRING` 每项带该能力声明的降级档位：`wire_capabilities` 里 OPTIONAL capability 的 factory 抛错（外部依赖故障等）→ 记 warning 并跳过装配；REQUIRED_CORE → 向上抛。失败的能力不会出现在 Registry，Consumer 走 `optional()` 的 None 降级路径。两层分工：capability 代码内部仍显式抛 `init_failed`（Q5 不变），**装配边界**按 08 §7 决定降级还是失败。实证见 `tests/capability/test_phase7_gate.py::TestDegradation`。

## 补充（#225，2026-09-17）：降级**原因**结构化 + 路由层按原因分流

上面那条补充只解决了"降级后 Agent 还能不能跑"，没解决"**对用户怎么解释**"。装配期把
原因写进 `logger.warning`，路由层看不到，于是任何缺席都只能给一句话。

**决策**：

1. `CapabilityWiring.degradations: dict[str, str]` 登记 capability 名 → `DegradeReason` 码
   （`capability/base.py`）。四个码：`not_configured` / `disabled` / `missing_settings` / `init_failed`。
   **只登记"非缺省"的原因**：不在 CAPABILITIES 里（= `not_configured`）是缺省状态，不进表；
   查表者把"键不存在"读作 `not_configured`。一个 capability 因自身内容为空而缺席
   （如 mcp 连上了但没有任何工具）也不进表——那是该能力自己的领域判据，它自己的 `errors` 是落点。
2. 前三个码是**配置状态**（改配置能解决），`init_failed` 是**装配期出错**（改 CAPABILITIES
   解决不了——要修的是它指向的东西，或按日志排查）。这个二分是这张表存在的全部理由：
   把它塌成一个"未启用"，就是让用户去改一个本来就配好的开关。
3. `/api/memories` 的 503 因此从 `detail: string` 变成 `detail: {code, message}`：
   `code` 是 `DegradeReason` 的值（给机器），`message` 是**逐原因**写的那句话（给人）。
   前端据 `code` 分流：「记忆未启用」（配置态，不给重试）vs 错误条 + 重试（`init_failed`）。
   **判别不走中文匹配**——文案会改，码不会。老后端（`detail` 是纯字符串、无 `code`）按配置状态处理。
   码是**跨端字面量**（前端 `MEMORY_INIT_FAILED`、e2e fixture 各硬编码一份），所以后端有一条
   把值集合钉死的用例（`test_reason_codes_are_a_closed_set`）：改名会让它先红，逼人去同步前端。
4. 前端**不再自己附会**一句"这是配置状态而非故障"：那是它对后端状态的断言，而真实原因它看不到
   （用户照这句去改 CAPABILITIES 永远修不好）。缺席原因由后端逐原因说清，前端只铺原文。

**边界**：

- 这张表**不覆盖**所有缺席：`knowledge` 的"没配 collection"、`websearch` 的"没配 key"、
  `mcp` 的"连上了但零工具"里，前两者进表（`missing_settings`），最后一个**刻意不进**
  （见决策 1）。要判断"某能力为什么不在"，先看这张表，再看该能力自己的 errors。
- `DegradeReason` 与 `CapabilityError.code` 是两套词汇：前者描述**装配结果**（为什么没装上），
  后者是**错误分类**（哪个环节错了），两者都可能取到字符串 `init_failed`，不要据此互推。
  语义其实**基本相反**：`CapabilityError(code="init_failed")` 出现在配置/契约错误处且
  **响亮失败不降级**（`capability/config.py`、`factories.py`、`wiring.py` 的显式 `raise`），
  `DegradeReason.INIT_FAILED` 是外部依赖故障**已降级跳过**。
- 这条取代了 MEM5 时的旧口径「503 = 未装配（降级态）；5xx = 读取失败」——503 内部现在有
  一个"真故障"子类（`init_failed`）。旧口径的活文档（`docs/integration/FRONTEND_MEM5_INTEGRATION_PROMPT.md`）
  就地加了一行指针指向本节；历史批次/巡检记录不改写（append-only）。

**实证**：`tests/capability/test_phase7_gate.py::TestDegradation`（四种缺席各留各的码，含真 factory
的 `missing_settings` 与"存的是码不是枚举成员"）、`tests/web/test_memory_api.py`（两条 503 的码与
文案都不同 + 文案表**覆盖整个枚举**）、`web/src/lib/api.test.ts` + `web/e2e/s-memories.spec.ts`
（前端按码分流；`init_failed` 那条真的点一次重试，锁"失败不被吞"）。
