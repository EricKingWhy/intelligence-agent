# 设计稿 — 上下文容量看板（#200，跨端）

- **Status**: 设计冻结，待实现
- **Date**: 2026-09-13
- **读者**：接下来实现 #200 的人（前端 + 后端，可能是另一个模型/agent）
- **Related**：Issue #200；ADR-0020b（context providers 运行时消费）；`docs/spec/Observable_Agent_Workspace_SDD/03_RUNTIME_EVENT_CONTRACT.md:162`（`cached_tokens?` 已声明未实现）；`docs/BENCHMARK_SYNTHESIS.md:109-116`；`docs/RESEARCH_OHMY_PI_DSH_COMPACTION.md:58,183-188`；AGENTS §6（Reuse First）、§15（主题变量双份）

---

## 1 需求（用户原话裁定，逐条都是硬要求）

| # | 裁定 |
| --- | --- |
| 1 | 新增「上下文容量」看板（形态参考图 4） |
| 2 | **必须包含全部元素**：已用总量、窗口总量、**消息 / 系统提示词 / 技能 / 其他**、**系统工具 / MCP 工具**、**平均缓存命中率**。不要"先做一半"的分期版本 |
| 3 | 窗口真相 = **全局 `max_context_tokens = 200_000`**。**不要**接通每模型 `context_window`（理由由用户给出：即使有 100M 上下文也没用，太多无关信息会干扰模型注意力；Codex 用 256k，本仓用 200k） |
| 4 | 入口**替代**原来的 `Context · N` 药丸区；合并后 `ContextProviderPicker` **直接删掉**（见 #201） |
| 5 | 记忆是自动注入的、不需要点击选择，所以入口不再承担"选记忆"的职责；模型按需检索由 ADR-0031 的 `retrieve_memory` 承担 |
| 6 | 参考 deepseek harness / ZCode 等产品，"能直接拿来用的就直接拿来用，不要手写"（§4 给出带 license 的复用清单） |

---

## 2 现状与数据缺口（**必须先补后端数据面**，否则前端只能造数）

| 看板元素 | 今天能否算出 | 证据 |
| --- | --- | --- |
| 已用总量 | ✅ 有估算 | `context/builder.py` `_estimate_tokens_cached` / `_token_estimate_total`（`:198-228`） |
| 窗口总量 | ✅ 常量 | `config.py:68` `max_context_tokens: int = 200_000` |
| 消息 | ⚠ 分类存在但**私有** | builder 内部按类估算，未暴露给任何 API/事件 |
| 系统提示词 | ⚠ 同上 | `builder.py` `_system_prompt_tokens`（`:57,104-109`） |
| 技能 | ❌ 无独立记账 | 技能目录注入的文本**折进 system prompt**（与 dsh 归档里 "Not separable here" 是同一个病） |
| 其他 | ❌ 未定义 | 需要把"上下文 provider 注入（记忆等）+ 未归类残差"定义为残差桶 |
| 系统工具 / MCP 工具 | ❌ **完全没有记账** | tool schema 的 token 从未计入任何地方（`assembly.py:212-218` 注册工具，无 token 统计） |
| 平均缓存命中率 | ❌ **未采集** | `runtime.py:118-135` `_usage_from_response` 只取 3 个键，**丢掉** `input_token_details`（含 cache hit/miss）；`03_RUNTIME_EVENT_CONTRACT.md:162` 已声明 `cached_tokens?: number` 但未实现；`tests/test_structured_logging.py:95-133` 把"恰好 3 字段"钉住了 |

> **诚实原则（贯穿本设计）**：看板里的每个数字都必须是**能算出来的**或**明确显示"未采集"**。用户明确要求不能伪造（此前我提出"无法采集时展示示例值 99.6%"被否）。**任何情况下不得用假数据填充。**

---

## 3 数据契约（后端先补，前端才能画）

### 3.1 用法采集：缓存命中（对应"平均缓存命中率"）

| 项 | 契约 |
| --- | --- |
| 采集点 | `runtime.py` 的 `_usage_from_response`（`:118-135`） |
| 新字段 | 从 provider 响应的 `usage.input_token_details.cached_tokens` 读**缓存读取 token 数**；字段缺失 → `None`，**不写 0** |
| 落点 | `model/completed` 的 `data.usage` 增加可选 `cached_tokens`；`usage_total` 增加累加项 `cache_read_tokens`（取消臂 `runtime.py:240-242` 也如实带上） |
| 口径 | **平均缓存命中率 = Σ cached_tokens ÷ Σ input_tokens**（该会话/该 run 所有模型调用求和）。**不是**逐调用命中率的算术平均（调用大小差异会让算术平均失真） |
| 无数据 | 没有任何一次调用带回 cache 字段 ⇒ 前端显示「未采集（提供商未返回缓存明细）」，**不显示 0%** |
| 舍入 | 百分比保留 1 位小数；分母为 0 时视为无数据 |
| 必改测试 | `tests/test_structured_logging.py:95-133` 的"恰好 3 字段"断言**必须**放宽为"3 个既有字段 + 可选第 4 个"。这是本票要求的行为变更，**不是**"改测试迁就代码"——实现 PR 必须显式说明这一点 |
| provider 差异 | 只有 OpenAI 兼容线的 `input_token_details` 可读；不返回 ⇒ `not_collected`（不做启发式猜测） |

### 3.2 分类记账（消息 / 系统提示词 / 技能 / 其他 + 工具两组）

**分类口径（顺序即优先级，保证不重叠、可求和）**：

| 桶 | 定义 | 来源 |
| --- | --- | --- |
| 系统提示词 | profile system prompt + 折进 system prompt 的工具指导文本 | `builder._system_prompt_tokens` |
| 技能 | 技能目录框架 + 各技能描述文本 | skills provider **实际注入**的消息成本（builder 按 provider 自称的 `name` 分账） |
| 消息 | 会话投影出的 messages（含压缩摘要投影成的 SystemMessage） | 逐条 `estimate_message_tokens` 求和 |
| 其他 | 其余 context provider 注入（记忆等）+ 运行期快照 | builder 上报的**真实注入成本**（非 skills 的 provider 之和 + 运行期快照） |
| 工具 · 系统 | core/内置工具 schema 的 token 估算 | §3.2.1 |
| 工具 · MCP | 外部（MCP/插件）工具 schema 的 token 估算 | §3.2.1 |

**不变量**：`Σ(消息 + 系统提示词 + 技能 + 其他) + Σ(工具两组) = 已用总量`（前端断言这条，见 §7 T4）。

**实现口径（与早期草案的差别，勿回退）**：`used_tokens` 与各桶都由 builder 上报的**真实成本直接求和**，**不是**"总量减各项"的残差倒推。倒推写法在总量只含投影 messages 时会令"其他"恒为 0，把记忆注入整块漏报——首版即此 bug（回归测试 `test_t4_snapshot_counts_provider_injection` 钉住）。分账依据是 provider 自称的 `name`，不是调用方按 provider 文本重算一遍（重算会复制 `select()` 的拼装逻辑并漏掉预算截断，从而估高）。

**3.2.1 工具来源分组（不靠名字前缀猜）**
在实现里用一个**显式常量集合**声明 core 工具名（`assembly.py:212-218` 的 9 个 + 内置 capability 工具名），其余（MCP / 插件）归"工具 · MCP"。要求这个集合有单测钉住——新增 core 工具却忘了登记时，测试会失败，而不是静默把系统工具算成 MCP。

**3.2.2 暴露方式：新增只读 HTTP 端点，不进事件流**
看板是 UI 查询，不是运行事实 ⇒ **不**把分类明细写进 SessionEvent（不变量 #4：Event ≠ Diagnostic Log）。

### 3.3 端点契约

`GET /api/sessions/{session_id}/context-usage`（只读，无副作用）：

```json
{
  "estimated": true,
  "window_tokens": 200000,
  "used_tokens": 48123,
  "thresholds": {"auto_compact": 0.70, "hard_guard": 0.85},
  "breakdown": {
    "messages": 21000,
    "system_prompt": 8000,
    "skills": 1500,
    "other": 623,
    "tools": {"system": 16000, "mcp": 1000}
  },
  "cache": {
    "state": "ok",
    "reported_calls": 12,
    "total_calls": 13,
    "avg_hit_rate": 0.87
  },
  "state": "ok"
}
```

| 字段 | 说明 |
| --- | --- |
| `estimated` | **恒为 true**。除 provider 返回的 usage 外一切数字都是估算；前端必须在 UI 上标注口径（副标题"估算值"） |
| `window_tokens` / `thresholds` | 直接来自 `Settings`（`config.py:68-70`）；**不得**改成每模型 `context_window`（用户裁定 3） |
| `breakdown` | §3.2 的六个数；`other` = 非 skills 的 provider 注入 + 运行期快照（真实记账，非残差倒推） |
| `cache.state` | `"ok"` \| `"partial"`（部分调用带回）\| `"not_collected"`（一次都没有） |
| `state` | `"ok"` \| `"no_data"`（会话还没有任何 build 快照） |

- 数据来源：`ContextBuilder` 最近一次 build 的快照（需暴露一个只读快照方法，**不改** build 行为）+ 该会话事件流里的 `usage`/`usage_total` 汇总。
- 无 run / 无快照 ⇒ `state="no_data"`、各数为 0、`cache.state="not_collected"`。

---

## 4 复用清单（Reuse First，逐项带 license；用户已明确授权"能拿就用"）

| 来源 | 文件 | License | 取什么 | 决策 |
| --- | --- | --- | --- | --- |
| deepseek harness | `ContextMeter.tsx` / `ContextMeter.module.css` / `context-occupancy.ts` | MIT | 占用条 + popover 结构与 CSS 思路 | **PORT DESIGN**（照搬结构与交互，不整段复制代码文本） |
| opencode | `session-context-breakdown.ts` / `session-context-tab.tsx` | MIT | 分类桶模型、`other` 残差桶、溢出重标定（小桶合并显示） | **PORT DESIGN** |
| pi 社区扩展 | 唯一带 Skills 桶的公开实现 | MIT | 分类命名（Skills bucket） | 参考命名 |
| Codex | `TokenUsageBreakdown` | Apache-2.0 | cache-aware 字段命名（`cached_input_tokens` 这类） | **REUSE** 命名 |
| Claude Code `/context` | — | proprietary | 交互形态（弹层 + 分段条 + 阈值标记） | **仅设计参考**，禁止复制代码/文案 |
| 分段条本身 | — | — | flex divs，**不需要任何库** | **BUILD**（不引 UI 库） |

**许可义务（AGENTS §6 最后一条）**：若实质复制了 MIT/Apache-2.0 的代码文本，文件头必须写来源 + license；仅借鉴结构/命名则在本设计稿与本票 PR 里注明来源即可。

---

## 5 UI 契约（impeccable 设计依据）

| 面 | 要求 |
| --- | --- |
| 入口 | 替换原 `Context · N` 药丸：显示占用百分比（小环形或细条）+ 悬停 tooltip「已用 48,123 / 200,000」。点击打开看板 |
| 面板布局 | 上：分段条（消息 / 系统提示词 / 技能 / 其他）+ 图例；中：工具两行（系统 / MCP）；下：缓存命中率一行（大字百分比 + 口径副标题） |
| 阈值可视化 | 70%（auto compact）与 85%（hard guard）在条上以标记线显示——这两个阈值对应真实的运行时行为（`config.py:69-70`），用户应能看见自己在哪 |
| 图例数值 | 每类显示 token 数 + 百分比（百分比按 `window_tokens` 算，与"已用总量"同一分母口径） |
| 溢出处理 | 小桶（< 2%）合并进"其他"的显示并按比例重标定（取自 opencode 的做法），但**图例仍列出全部桶**，不隐藏数据 |
| 空态 | `no_data` → 「暂无用量数据」；`cache.state="not_collected"` → 「未采集（提供商未返回缓存明细）」。**禁止**显示 0% 或示例值 |
| 主题 | 新增颜色/尺寸 token 必须在 `:root` **与** `:root[data-theme='light']` 双份定义（AGENTS §15） |
| 无障碍 | 分段条 `role="img"` + `aria-label` 概述各类占比；弹层与既有 picker 一致：`Esc` 关闭、焦点陷阱、键盘可达 |
| 刷新 | 打开时拉一次 + 会话有新 run 时刷新；不做实时轮询（避免无谓请求） |

---

## 6 不变量与约束

| 不变量 | 守法 |
| --- | --- |
| #21 Optional 故障不拖垮 Core | 端点失败/无数据 ⇒ 前端降级为空态，绝不影响会话 |
| #7 完整保存 ≠ 完整注入 | 看板**只读**，不改变注入内容 |
| #4 Event ≠ Diagnostic Log | 分类明细不进事件流，只经 HTTP 只读端点 |
| #22 前端不维护第二套真相 | 数据源 = builder 快照 + 事件流 usage 汇总，前端不自行推算 |
| #15 Artifact/大内容 | 与本票无关（不改） |
| 诚实 | §3 的 `estimated` / `not_collected` / 残差桶三条硬约束 |

---

## 7 测试要求

| # | 用例 | 断言 |
| --- | --- | --- |
| T1 | 采集 cache | mock 响应带 `input_token_details.cached_tokens` ⇒ `model/completed.usage.cached_tokens` 存在且相等；缺失 ⇒ 字段为 `None`/省略，**绝不写 0** |
| T2 | 命中率算法 | 两次调用（1000/800 命中）⇒ 平均 = 0.8（求和口径，不是逐次平均）；全缺 ⇒ `cache.state="not_collected"`；部分缺 ⇒ `"partial"` |
| T3 | 既有断言更新 | `test_structured_logging.py` 更新后仍断言 3 个既有字段，并允许可选第 4 个；PR 说明这是本票的行为变更 |
| T4 | 求和不变式 | `Σ(四类) + Σ(工具两组) == used_tokens`；构造有 MCP 工具的场景 ⇒ `tools.mcp > 0` |
| T5 | 端点形状 | 无数据 ⇒ `state="no_data"`；有 run ⇒ 各数与 builder 快照一致；`window_tokens == 200000`（回归：用户裁定不要每模型窗口） |
| T6 | 前端渲染 | 六个桶都渲染；空态/未采集文案正确（不出现 0%）；亮色主题下新 token 生效（双份定义）；`Esc` 可关闭 |
| T7 | 阈值标记 | 70% / 85% 标记位置按百分比换算正确（纯函数单测） |

---

## 8 分期与依赖

两段，**同一票内交付**（用户要求元素齐全，不允许砍元素的分期）：

| 段 | 内容 | 依赖 |
| --- | --- | --- |
| 后端 | §3.1 采集 + §3.2 分类快照 + §3.3 端点 | 无 |
| 前端 | §5 面板 + 入口替换 | 后端端点契约冻结；入口删除依赖 #201 的三 picker 合并 |

---

## 9 Out of scope（明确不做）

- **不做每模型 `context_window`**（用户明确否掉；全局 200k 是唯一口径）。
- 不做历史趋势/时序图（只显示当前快照）。
- 不做精确 token 计数（一律估算；只有 provider usage 是实测，且它不提供"构成"）。
- 不做压缩前后对比、不做单 run 明细下钻。
- 不引入任何图表库（分段条用 flex divs）。

---

## 10 风险与对策

| 风险 | 对策 |
| --- | --- |
| 技能桶与"其他"的分割 | 按 provider 自称的 `name` 分账（skills provider 的注入单列成"技能"，其余 provider 归"其他"），记的是**实际注入**成本；UI 副标题标注"估算"；某桶恒为 0 时如实显示 0 而**不是**填假数 |
| "其他"桶恒为 0 的漏报（首版 bug） | `used_tokens` 与各桶都由 builder 真实成本求和，禁止残差倒推；回归测试钉住（`test_t4_snapshot_counts_provider_injection`） |
| 不同 provider 的 cache 字段差异 | 只读 OpenAI 兼容的 `input_token_details`；不返回 ⇒ `not_collected`，不猜 |
| 分类估算与真实 token 有偏差 | `estimated: true` + UI 副标题；端点契约里不承诺精确 |
| 工具 schema 记账新增的估算开销 | 只在端点被调用时计算（不在每轮 build 里做），避免给热路径加成本 |
