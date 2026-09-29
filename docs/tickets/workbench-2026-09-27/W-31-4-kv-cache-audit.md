# W-31.4 (#416) KV-cache / 确定性卫生审计报告

- **审计两阶段**：① 审计阶段在 `origin/codex/346-protected-facts` 分支树（head `0974d6d3`，含 #411）完成——独立审计（只读）；② 复核阶段在本票分支树（`zcode/T416-w31-4-kv-cache-audit`，head `0e639364` = main `88c258a8`〔#411 已合并〕+ 测试 `db0e34e2` + 修复 `0e639364`）对全部行号引用逐条重验（99 条构造体：90 CONFIRMED / 7 SHIFTED 已就地更正 / 2 STALE——STALE 的两条正是 ⚠️A 被修复删除的原文证据，属修复生效的直接结果）。全部 29 个被引文件 `git diff 0974d6d3 0e639364` 逐一核对：context/builder.py、session/derive.py、context/compactor.py、context/pruner.py、context/tokens.py、session/plan.py、prompt/* 等 27 个文件**零漂移**（builder.py 额外验证 `0974d6d3..88c258a8` 空 diff）；仅 assembly.py（#415 +6 行）、memory/rank.py、memory/context_provider.py（本票修复）三个文件漂移。
- **审计者**：审计阶段独立只读；复核阶段独立只读 subagent（逐 claim 实测，仓库零写入）。
- **复现命令**（审计阶段证据可按此重跑；复核阶段把 rev 换成 `0e639364`）：

```bash
git rev-parse origin/codex/346-protected-facts^{commit}
# = 0974d6d3e241c9a754c8d2713a7a4284b32b990d
git grep -n -E "datetime\.now|time\.time\(|uuid4|random\.|strftime" 0974d6d3 -- src/
git grep -n -E "os\.environ|getenv" 0974d6d3 -- src/
git show 0974d6d3:src/agent_harness/context/builder.py
```

- **前缀可见内容定义**（本报告的判据面）：每次 `ContextBuilder.build()` 发给模型的全部消息 =
  system prompt + provider 注入（skills/memory）+ 运行时快照 + 进度清单锚块 + 保护事实两条 +
  事件投影消息（含压缩摘要、dangling 合成）+ 接近硬护栏 warning。

---

## 1. 结论总表

| # | 审计对象 | 结论 | 证据（文件:符号:行；行号 = 终树 `0e639364` 实测，经复核阶段逐条重验） | 建议处置 |
|---|---|---|---|---|
| 1 | system prompt 组装（registry / persona / tool guidance） | ✅ 确定 | `prompt/registry.py:PromptRegistry.sections:109-113`（`(order, name)` 显式稳定排序，name 全局唯一 R1 ⇒ 排序键全序）；`prompt/registry.py:assemble:149-159`（固定 `"\n\n"` join，分区不混装）；`prompt/builtin.py:22-354`（全部正文为模块级静态字符串，`DEFAULT_REGISTRY = build_registry()` 永不读环境，:346-349）；`prompt/persona.py:persona_sections:84-112` / `compose_agent_prompt:115-142`（固定 part 顺序）；`prompt/tool_sections.py:join_guidance:88-98`（同样 `(order, name)` 排序）。无时间戳/随机数/环境依赖排序 | 无问题 |
| 2 | skills provider 注入 | ✅ 确定 | `skills/context_provider.py:select:29-50`（目录渲染按 `catalog()` 列表序逐行 join；截断循环 `break` 判据 = token_budget，确定）；`skills/capability.py:catalog:19-21`（返回 catalog 列表副本）；`skills/discovery.py:162`：`skill_dirs = sorted(directory.iterdir())` —— 文件系统枚举显式排序，无 os 迭代序泄漏 | 无问题 |
| 3 | 运行时快照：call-once 契约 + 内容 | ✅ 确定（内容含天粒度日期 → ⚠️C 单列） | `context/builder.py:289-298`：`self._runtime_context_provider()` 每次 build **恰好调用一次**（token 估算与注入用同一份文本）；`assembly.py:_render_runtime_context:441-462`：`cwd`/`os`/`date.today().isoformat()`（:461，**天粒度**、非秒级）；渲染为 META_USER（user-role），由 `_inject_runtime_context` 插在最后一条 HumanMessage 之前（builder.py:562-589）——易变内容贴尾部，符合 ADR-0023 D8 | 无问题（⚠️C 见 §2） |
| 4 | 进度清单锚块（`_should_inject_plan` / `_render_plan_block` / `_inject_plan_block`） | ✅ 确定 | `context/builder.py:_should_inject_plan:77-119`：docstring 明示纯函数；实现只读 `session.events`（`derive_plan` + durable seq 计数），无 wall-clock、无实例状态；`_render_plan_block:122-134`：逐字渲染 `derive_plan` 投影，标题常量 `_PLAN_BLOCK_HEADING`（:69）；`_inject_plan_block:137-153`：落点只依赖 SystemMessage 分布。`session/plan.py:derive_plan:194-214`：last-wins 整表覆盖，行序 = payload 列表序（写侧 `_parse_items:93-124` 保序且 id 唯一） | 无问题（观察项 ⚠️E 见 §2） |
| 5 | #411 保护事实：`derive_protected_facts` 输出顺序 | ✅ 确定 | `session/derive.py:derive_protected_facts:614-980`：事实在**单遍事件循环**（:727 起）中按序 `facts.append`（`add_fact:719-725`）⇒ 输出序 = 事件流序，重放两次逐字节同。`user_goal_sources`（:683,710）、`superseded_sources`（:618-629）、`cancelled_queue_ids`（:630-635）、`fact_ids`（:663）等 set **只做成员判定，从不迭代进输出**；`latest_tool_result_seq`/`latest_reconciled_seq`（:650-661）dict 后写覆盖 = 确定语义 | 无问题 |
| 6 | #411 保护事实：`serialize_protected_facts` + 两条消息 | ✅ 确定 | `session/derive.py:serialize_protected_facts:134-139`：`json.dumps(..., sort_keys=True)` —— dict 键排序；列表序 = facts 序 = 事件流序；`ProtectedFact.to_dict:116-131`：固定键字面量。`context/builder.py:_protected_facts_messages:460-484`：system 策略行 = 固定常量文案；user 数据行 = 固定标题 + records。fact_id 生成 `session/derive.py:_expected_fact_id:374-388`：固定键集 + `sort_keys=True` + sha256 前 24 位 —— 纯内容寻址 | 无问题。**数据行走 HumanMessage 是刻意的优先级设计，不是缓存缺陷，不报「应改 SystemMessage」** |
| 7 | #411 保护事实：`_inject_protected_facts` 插入下标 | ✅ 确定 | `context/builder.py:486-498`：`insertion` = 开头连续 SystemMessage 计数（纯消息结构函数）；输出 = `[pf_system, *原前缀system段, pf_human, *其余]`。同一事件流 ⇒ 同一 messages ⇒ 同一下标 ⇒ 逐字节稳定 | 无问题 |
| 8 | work_boundary 保留最新一条的 max 平票 | ✅ 确定 | `context/builder.py:252-268`：`max(..., key=source_seq, default=None)`——Python `max` 平票取**迭代序首个**，迭代序 = `all_protected_facts` = 事件流序 ⇒ 平票裁决确定 | 无问题 |
| 9 | token 估算纯度（estimate_tokens / estimate_message_tokens） | ✅ 确定 | `context/tokens.py:7-14`：两个模块级纯函数，无 `lru_cache`、无全局可变状态（全 src `git grep lru_cache -- src/` 零命中，终树复跑仍零）；`tiktoken.get_encoding("cl100k_base")` 由 tiktoken 进程内缓存，编码确定 | 无问题（首调用可能触发编码文件下载，属可用性非确定性，见 §4 诚实清单） |
| 10 | `model_dump_json` 键序稳定性 | ✅ 确定（附版本注） | `context/tokens.py:14` / `context/builder.py:628`：pydantic v2 按字段声明序序列化；同一进程内同一安装版本恒定。langchain message 模型字段序不随运行路径变化 | 无问题。跨包升级键序无冻结保证——由 lockfile 控制，不属本票 |
| 11 | event→message 投影重放一致性 | ✅ 确定 | `session/derive.py:derive_messages_with_source_ranges:1049-1298`：bracket 收集（:1071-1119）按 dict+`start_order` 列表，`retained_brackets = sorted(..., key=(start, position))`（:1148-1152）；supersede 区间 `sorted(superseded_seqs)`（:1184）；`is_shadowed`（:1194-1197）纯 seq 判定；投影循环（:1210-1259）只吃 USER_MESSAGE / MODEL_COMPLETED / TOOL_RESULT + 摘要注入。全函数无 set/dict 迭代进输出、无时钟。`session/session.py:derive_messages:575-577` 纯委托 | 无问题 |
| 12 | dangling 合成消息 | ✅ 确定 | `session/derive.py:DANGLING_TOOL_CONTENT:67`：固定常量「工具执行被中断，结果未知」；注入循环（:1261-1296）按 `msg.tool_calls` 列表序遍历（:1282-1293），`source_range=None` | 无问题 |
| 13 | pruner 决策与原位替换 | ✅ 确定（store 状态依赖 → ⚠️B 单列） | `context/pruner.py:_decide:179-232`：分组 dict 按 events 遍历序构建（Python 3.7+ 插入序），`pruned.sort(key=seq)`/`skipped.sort(key=seq)`（:224-225）显式排序；survivor=`members[-1]`（事件序最后）；`_render_skeleton:246-262`：固定 dict 字面量 + 截断 120 字符 + `json.dumps(separators=(",",":"))`（:262）——键序由字面量固定；`apply:154-177`：仅替换 `source_range` 已知的 ToolMessage，压缩摘要与 dangling（range=None）天然排除。`context/builder.py:_prune_projection:532-560`：决策整体替换语义（`self._prune_decisions[sid] = {...}` 全量重建），memo 失效取「本次 ∪ 上次」双保险（:545-555） | 无问题（⚠️B 见 §2） |
| 14 | usage_snapshot / `_last_*` 记账不反向影响投影 | ✅ 确定 | `context/builder.py:668-715`：只读实例计数器 + 重放 `_prune_decisions`，无 session.append、无 build 行为读写；`_last_protected_fact_tokens`（:275）、`_last_runtime_context_tokens`（:308）、`_last_plan_tokens`（:332,424）、`_last_provider_tokens_by_name`（:662）只在 build 内写、usage_snapshot 读；`other` 桶求和（:703-706）对 dict 迭代序不敏感（加法交换）；压缩 `reserved_tokens`（:344-349, :427-432）全部来自局部确定量 | 无问题 |
| 15 | `CompactionResult.bracket_id = str(uuid4())` 是否进前缀 | ✅ 确定（不进） | `context/compactor.py:361`：uuid 只进返回值；`context/builder.py:377-394`：只写入 COMPACTION_START / CONTEXT_COMPACTED / COMPACTION_END 三个事件 payload；`session/derive.py:998-1000`：三者 ∈ `_BRACKET_META_TYPES`，投影循环 `:1228-1230` 显式跳过；摘要消息 content = payload["summary"]（:1092-1103 提取，:1218-1224 注入）——**不含 bracket_id**。CONTEXT_COMPACTION_FAILED 同样不在投影集合 | 无问题 |
| 16 | #411 摘要形态变更：name 常量 + 双形态识别 + pruner 排除 | ✅ 确定 | 常量 `session/derive.py:COMPACTION_SUMMARY_MESSAGE_NAME = "context_compaction_summary"`（:1001）；新形态落点：compactor `:255-258`（`HumanMessage(content=summary_text, name=...)`）与投影 `:1218-1224` 同名同常量；双形态识别：`compactor.py:_is_compaction_summary:486-492`（HumanMessage 按 name ＋ SystemMessage 按 `"## 原始目标与用户约束\n"` / `"## 目标\n"` 前缀）——旧会话 SystemMessage 摘要重放时不会被当普通系统前缀（compactor 前缀扫描 `:186-189` 也显式排除）；pruner `apply:154-177` 只动 ToolMessage ⇒ 两种摘要形态都不被裁 | 无问题。**角色变更作为 #411 的一次性前缀变更登记，此后重放稳定** |
| 17 | W-04 失败注入：`context/compaction_failed` 不投影 | ✅ 确定 | `session/event.py:72`：`CONTEXT_COMPACTION_FAILED = "context/compaction_failed"`；`context/builder.py:_PROJECTING_EVENT_TYPES:46` 不含它；`derive.py` 投影循环只处理三类投影事件 ⇒ 永不投影、不 shadow（`_record_compaction_failures:500-517` 载荷有界） | 无问题 |
| 18 | 接近硬护栏 warning（META_USER 非持久化） | ✅ 确定 | 文案 `prompt/builtin.py:_CONTEXT_PRESSURE_TEXT:284`：模块级常量，PRD 逐字冻结；组装 `context/builder.py:452-457`：`DEFAULT_REGISTRY.assemble("frame:context_pressure").meta_user_text`（registry 排序确定）+ 判据 `provider_estimate ∈ [auto, hard)`（输入全确定）+ `_insert_before_last_human`（:49-62，结构函数）；不 session.append | 无问题 |
| 19 | `os.environ` / locale 是否进提示文本 | ✅ 确定（不进） | 全 src environ 命中（`instance_lock.py:87,312`、`mcp/client.py:71`、`mcp/config.py:82`、`sandbox/local.py:142,160`、`memory/v2/cutover.py:635`；终树复跑命中清单逐条一致）均为锁/MCP/sandbox 配置面，无一渲染进 prompt；prompt 体系唯一变量源 = 装配点闭包显式传值（`assembly.py:456-462`）；无 locale 相关格式化进前缀 | 无问题 |
| 20 | 事件时间戳（`SessionEvent.time`）是否进前缀 | ✅ 确定（不进） | `agent/types.py:103`：`time` 默认 `datetime.now(UTC)` 毫秒级——但投影只取 `content`/`tool_calls`/`tool_call_id`（`derive.py:1236-1259`）；PlanState、ProtectedFact 均不含时间字段；`undelivered_inputs` 用 `event.time`（:1420,1441）但不进模型可见面。**前缀内无秒级时间戳**（Manus 规则成立） | 无问题 |
| 21 | memory provider 注入（前缀头部的确定性问题） | ✅ 确定（⚠️A 已由本票修复） | `memory/context_provider.py:select:41-82`：注入位置 = `_with_providers` 的开头 SystemMessage 段之后（`builder.py:663-666`）⇒ 位于**前缀头部区域**；修复后 `memory/rank.py:rank_entries:15-38`：缺省锚 = 候选集批内最新 `created_at`（:28）、显式 `(−key, id)` tie-break（:38）；provider 路径 :51 = `rank_entries(candidates, now=_event_flow_anchor(session))`（`_event_flow_anchor` :19-27 = 会话最新 durable 事件时间）。**recency 不再依赖 wall clock** | ⚠️A 行为级修复，见 §2 |

**计数**：✅ 21 项（含 #21 由 ⚠️A 修复翻绿；其中 3 项附登记性注记）/ 4 项登记观察（⚠️B-E，见 §2）/ ❌ 0 项未处置。
**总判**：在同一事件流 + 同一进程环境 + provider 正常的前提下，`build()` 前缀逐字节可重放；#411 新增通道未引入非确定性；审计发现的唯一真正跨 build 内容可变通道（memory recency wall-clock 漂移，⚠️A）已由本票行为级修复消除（`0e639364`），修复后全仓 src 不再有影响前缀的 wall-clock 成分。

---

## 2. 非确定项 / 需注意项明细

### ⚠️A（唯一行为级发现，**已修复**）：memory 注入的 recency 排序曾依赖 wall-clock

- **审计发现（原貌保留）**：`memory/rank.py:20`（修复前 `current = now or datetime.now(UTC)`），消费方 `memory/context_provider.py:39`（修复前 `ranked = rank_entries(candidates)`，未传 `now`）。评分 = `0.7*score + 0.2*importance + 0.1/(1+age_days)`：两次 build 之间无新事件时 `score`/`importance`/`created_at` 不变，但 `age_days` 随 wall-clock 连续变小 ⇒ 每个条目的 key 都在漂移；两条 key 足够接近时**排序翻转** ⇒ `accepted` 拼接次序变化 ⇒ 注入的 SystemMessage 内容次序变化。memory 注入落在前缀**头部**（builder.py:663-666），此处任何字节变化都会击穿其后整段 KV-cache——与 Manus「前缀内禁时间敏感成分」直接相关。翻转概率低（需要近平票），但机制上存在且随会话变长（跨天）概率上升。
- **处置记录（修复 commit `0e639364`，独立 commit 满足票面验收）**：采纳审计选项 2 的设计（session 事件流锚，把 recency 变成事件流函数——最贴近 append-only 纪律），经两阶段设计评审（含 #411 适配性复核与 PR #429 删除 V1 工具层后的范围复核）后落地：
  1. **`memory/rank.py`**：`rank_entries` 缺省锚从 `datetime.now(UTC)` 改为**候选集批内最新 `created_at`**（durable per-entry 时间，:28）；新增显式 tie-break `sorted(materialized, key=lambda entry: (-key(entry), entry.id))`（:38，替代「stable-sort 保输入序」——输入序来自向量库返回序，不是稳定契约）；`_parse_created_at` helper（:41-44）上提原 key() 内联解析。docstring 明写「`now` 是确定性时间锚，禁止传 `datetime.now()`」。
  2. **`memory/context_provider.py`**：provider 路径显式传 `_event_flow_anchor(session)`（:51）= 会话最新 durable 事件时间（`session.events[-1].time`，ISO 毫秒 UTC 字符串，append-only 逐字重放）；:19-27 新增模块级纯函数。无新事件 ⇒ 锚不变 ⇒ 注入逐字节稳定；新事件 ⇒ 锚推进（变化有语义原因）。
- **为什么必须改**：这是审计 21 项中唯一需要行为级变更的非确定成分；机械就地修（选项 1：select 开头取一次 now）只消除同一次 select 内的漂移、不解决跨 build——与本票的 KV-cache 目标（前缀跨 build 稳定）不符。
- **影响面（行为级变更登记）**：① 检索排序语义从「相对 wall clock 的绝对龄」变为「相对事件流/批内锚的龄」——**历史数据零迁移**（事件 append-only，已落盘内容不重算；`MemoryEntry.created_at` 写入路径不动）；② exact-tie 条目次序可能变化（输入序本就不是契约）；③ 生产调用点仅剩 provider 一处（PR #429 已删除 V1 `memory/tools.py` 的第二个调用点，审计后置变更，恰好消解「两调用点锚来源不同」的历史顾虑）；④ **回归红→绿实证**：`tests/context/test_prefix_stability.py` 15 条在修复前树首跑 **13 passed / 2 failed**（红 = 本发现的症状钉 + 根因钉，逐条对应），修复后 **15 passed**；⑤ 公式本体（0.7/0.2/0.1 权重）不动，D1「排序只落一处」不破。
- **残余（登记不修，另立票同口径）**：`memory/v2/recall.py:101` 的 `as_of or datetime.now(UTC).date()` 是同型 wall-clock 缺省——V2 未接 provider 注入、不入前缀，不属本票面（见 §4-3）。

### ⚠️B（登记，设计使然）：pruner 读回校验依赖 artifact store 状态

- **位置**：`context/pruner.py:_readable:234-244`（`self._validation` 粘滞缓存 + `store.inspect` 远端探测）。
- **推理**：裁剪决策 = f(事件流， store 可读性)。两次 build 之间 store 可用性变化（远端故障恢复）⇒ 决策可从 skip 翻转为 prune ⇒ 骨架行替换进/出前缀。缓解：判据「False 同样缓存 + 决策粘滞」保证**单调收敛不振荡**（模块 docstring :122-128 明示）；同环境下两次连续 build 输出一致。**结论：同一事件流 + 同一 store 状态下确定；跨环境状态漂移属 fail-closed 设计，不改。**

### ⚠️C（登记，设计使然）：运行时快照的天粒度日期 + 工具回合中途的位置退化

- **位置**：`assembly.py:461`（`date.today().isoformat()`）；`context/builder.py:577-583` docstring 已如实登记「build 发生在工具回合中途时快照落在整段历史之前」的位置退化。
- **判定**：天粒度非秒级，满足「前缀内禁秒级时间戳」；跨午夜会话快照文本翻转一次，属既定取舍（ADR-0023 D8 / builder.py:199-200 注释）。不改；回归测试用固定文本的 provider 规避深夜翻转 flake（见 §3）。

### ⚠️D（登记，测试交互）：memory provider 失败路径在 build 中途 append 事件

- **位置**：`memory/context_provider.py:71-82`：`select()` 异常臂 `session.append(MEMORY_DEGRADED, ...)`（:77-81）。
- **影响**：`build()` 的「零新事件」性质只在 provider 正常时成立；memory 检索失败会让本次 build 多落一条 MEMORY_DEGRADED（不投影成消息，前缀内容不受影响）。对票面回归用例的含义：**prefix_stability 测试接确定性 fake memory capability**（不接真检索），失败注入路径不被触发，「零新事件」断言稳定。

### ⚠️E（登记观察，无行为变更）：清单锚块位置 + #411 对 `_inject_plan_block` 分支的影响

- **位置**：`context/builder.py:_inject_plan_block:137-153`。
- **两点如实登记**：
  1. 清单锚块落点在前缀**头部 SystemMessage 区域**（无摘要时）。W-29 的静默窗节奏意味着该块会周期性出现/消失 ⇒ 其出现/消失会使其后整段前缀失效。这是 PRD §4.6 选定的 ephemeral 锚块语义（决策纯度已由判据①保证），**不是本票缺陷**；若未来做缓存经济学优化（锚块贴尾），属行为级变更，另行立项。
  2. #411 把摘要改为 `HumanMessage(name=...)` 后，`_inject_plan_block` 的「插在第一条压缩摘要之前」分支（:145-149 只匹配 `SystemMessage` 且 `_is_compaction_summary`）对新形态摘要**不再命中**，实际恒走「开头连续 SystemMessage 之后」分支。位置仍是消息结构的纯函数（确定），且 system 区域恒在摘要之前 ⇒ PRD §4.6「清单 → 摘要」顺序语义保持。登记为 #411 的既成事实，无修改建议。

---

## 3. 回归落地方：`tests/context/test_prefix_stability.py`（已入库，15 条）

票面要求的一条机械回归已落地为 `tests/context/test_prefix_stability.py`（commit `db0e34e2`）：对象级读法（`model_dump_json()` 比对，不比工作树字节）；复用 `tests/context/test_pruner.py` 的范式（`make_session(tmp_path)` + 事件 append + 逐字段断言 + `[e.to_dict() for e in session.events]` 前后比对）与 `tests/context/test_builder.py` 的零模型调用断言（`model.snapshots == []`，`ScriptedModel` 来自 `tests.scripted_model`）。

**覆盖面（与票面审计四通道的对照见文件 docstring）**：

1. **前缀主干 5 条**：同事件流连续两次 build 逐消息 `model_dump_json()` 相等（零模型调用、零新事件双断言）；append-only 追加一条不改变注入决策的事件后旧段逐字节不动；跨 builder 实例重放逐字节相等（钉 `_token_memo`/`_prune_decisions`/`_system_prompt_tokens` 是缓存非事实源）；从同一 store 重建 Session 重放逐字节相等（resume 后前缀稳定的机器钉）；dangling 合成确定性。
2. **#411 四通道 5 条**：保护事实两条消息的逐字文案 + `json.dumps(sort_keys=True)` 键序 + fact_id 内容寻址 + **数据行 HumanMessage 契约钉**；`_inject_protected_facts` 插入下标结构稳定（完整期望消息序 8 条 + 两次 build 下标恒等）；provider SystemMessage 在场的下标版本（期望序 9 条）；`injected_by` 非直接输入 ⇒ 零事实反向控制；记账计数器跨 build/跨实例全等（`_last_protected_fact_tokens`/`_last_plan_tokens`/`_last_runtime_context_tokens`/`usage_snapshot`）。
3. **摘要双形态识别 1 条**：`_is_compaction_summary` 对 `HumanMessage(name=COMPACTION_SUMMARY_MESSAGE_NAME)` 与旧形态 SystemMessage 双认可 + 非摘要系统块不误判 + pruner 对两形态零改动。
4. **memory 确定性 4 条**（审计后置：接确定性 fake capability）：两次 build 注入逐字节相等；**wall-clock 推进钉**（`_ShiftableClock` 替身平移时钟，注入仍不变——修复前此钉红，即 ⚠️A 症状）；`rank_entries` 同批两时钟同序钉（修复前红，即 ⚠️A 根因）；显式 `now` 契约确定性。

**红→绿实证（行为级修复的证据链）**：修复前树首跑 **13 passed / 2 failed / 1.90s**（两条红 = ⚠️A 症状钉 + 根因钉，与审计预测逐条一致，全部 #411 通道用例首跑即绿）；修复后 **15 passed / 0.98s**；聚焦面 `tests/context/ + tests/memory/` **888 passed**；冻结树全量 **4804 passed / 2 skipped / 0 failed / 858.01s**（= 4789 基线 + 15 新用例，零新失败零 flake）；ruff 0 error。

**测试设计要点**：全部断言走 `model_dump_json()`（含 role/name/content/tool_calls 全字段）；memory 用确定性 fake capability（真检索是外部非确定面，且真 provider 失败臂会 append MEMORY_DEGRADED 破坏「零新事件」——见 ⚠️D）；`runtime_context_provider` 用固定文本 lambda（真闭包含 `date.today()`，直接用会在午夜边界偶发 flake）；预置合法压缩 bracket（COMPACTION_START → CONTEXT_COMPACTED → COMPACTION_END）让「摘要 + 恒注入清单 + 保护事实」三条注入通道同场且零模型调用。

---

## 4. 诚实清单（未覆盖 / 不确定判断）

1. **审计主体是静态路径推演**：§1 各项「重放两次逐字节相同」在审计阶段为代码路径推理；本票落地的 `test_prefix_stability.py` 把其中**机器可钉的核心断言**（两次 build / append-only / 跨实例 / 跨 Session / dangling / #411 四通道）转为运行实证（13/2 → 15/0）；未转化为用例的静态结论（如 prompt 组装、skills 枚举）维持推理口径。
2. **框架内部未审**：`model_dump_json` 键序、`tiktoken.get_encoding` 的确定性结论基于 pydantic v2 / tiktoken 的公开行为与当前 lockfile；未打开 langchain-core / pydantic 源码验证，也未钉版本。跨依赖升级时该结论需重验（票面范围外）。
3. **memory 内部未深查**：审到 `MemoryContextProvider.select` / `rank_entries` 边界为止；`langmem_capability.py` 与 `memory/v2/*` 内部检索的确定性未逐行审——`v2/recall.py:101` 的 `as_of or datetime.now(UTC).date()` 是已知同型残留（V2 未接 provider 注入、不入前缀）。若 memory V2 接入 provider 注入，需按 ⚠️A 同口径复审并另立票。
4. **ScriptedModel 语义未打开核对**：`tests/scripted_model.py` 的 `snapshots` 记账语义引自 `tests/context/test_builder.py:36` 的既有用法，未读该文件本体。
5. **fork tail 摘要只查到落盘边界**：`session/fork.py:116-119` 用 `aux:fork_tail` 生成摘要并以 USER_MESSAGE 事件落盘（一次性 LLM 产物，落盘后重放稳定）；fork.py 全文未逐行审（fork 是事件前缀重放，属 #346 审计面，不属本票前缀卫生面）。
6. **`web/` 与非 src 路径不在审计面**：前缀可见通道只在 src 内闭合（runtime.py 注释「ContextBuilder 是模型可见投影的唯一入口」+ 实测 build 返回值直进模型调用），web 层只读 usage_snapshot。
7. **修复前的翻转概率未量化**：`0.1/(1+age_days)` 的日级变化量 ~1e-4 量级，近平票翻转需要 score/importance 差也在此量级——机制存在性确定（并有红证钉），实际触发频率未测（无生产数据）；修复后该概率问题整体消失（锚为事件流/批内时间，无连续漂移量）。
8. **压缩触发路径的确定性依赖 token 估算值**：估算为近似值（cl100k_base vs 真实模型分词），同一流上确定；但「是否触发压缩」这个分支本身随 builder 配置变化——本审计只断言「同配置同流同决策」，不断言跨配置一致。

---

## 5. 审计执行记录（供复核）

- **审计阶段**（基线 `0974d6d3`）：提取并逐行读过的分支文件：`context/builder.py`、`context/tokens.py`、`context/compactor.py`、`context/pruner.py`、`context/provider.py`、`session/derive.py`（1465 行全量，分四段）、`session/plan.py`、`session/session.py`（仅 derive_messages 委托段）、`prompt/builtin.py`、`prompt/registry.py`、`prompt/section.py`、`prompt/template.py`、`prompt/persona.py`、`prompt/tool_sections.py`、`skills/context_provider.py`、`skills/capability.py`、`skills/discovery.py`（排序相关行）、`memory/context_provider.py`、`memory/rank.py`、`assembly.py`（system_prompt / runtime snapshot / provider 选择段）、`agent/runtime.py`（build 调用与 system_prompt 接线段）、`session/fork.py`（grep 定位）、`session/event.py`（常量名）。全 src 搜捕命令：`datetime.now|time.time(|uuid4|import random|random.|time.localtime|strftime` 与 `os.environ|getenv`（命中逐个归类，见 §1 #15/#19/#20 与 §2）；`git grep lru_cache` → 零命中。
- **复核阶段**（终树 `0e639364`，独立只读 subagent）：99 条 claim 构造体逐条实测 = 90 CONFIRMED / 7 SHIFTED（assembly.py ×3〔#415 +6 行：435-456→441-462、455→461、450-456→456-462〕、context_provider ×2〔select 29-58→41-82、异常臂 65-69→71-82〕、rank.py 1〔15-29→15-38〕、builtin.py 1〔22-355→22-354，尾行差一〕）/ 2 STALE（⚠️A 两处原文证据被修复删除——修复生效的直接结果）。`git diff 0974d6d3 0e639364` 对 29 个被引文件逐一核对（27 个空 diff）；三条 grep 终树复跑（datetime 族 / lru_cache 零命中 / environ 清单逐条一致）。修复后终树 grep 复核：`rank_entries` 生产调用点仅剩 `context_provider.py:51`（带事件流锚）；rank.py 无实调 `datetime.now`（:21 为 docstring 禁令文本）。
- **测试范式参照**：`tests/context/test_pruner.py`、`tests/context/test_builder.py`、`tests/conftest.py:make_session`。
