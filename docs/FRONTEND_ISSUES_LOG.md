# 前端问题登记簿（实时更新）

> **用途**：真实浏览器点击测试 + 日常使用中发现的任何问题，实时登记在此。
> 修 bug 时照着本文档逐条处理；修完把状态改为 `已修复` 并补上 commit。
>
> 状态词汇：`未修复` / `修复中` / `已修复（commit）` / `不改（理由）`
> 严重级：`P0 功能不可用` / `P1 功能可用但体验破损` / `P2 边角 / 打磨`

---

## 问题清单

> **本轮（第三轮 · 控制面清点）新增的后端问题 OBS-011～OBS-014 与两项覆盖缺口，正文在文末「第三轮」章节**（含根因文件行号与原始字节级证据）——它们不在下方历史清单里，勿以为遗漏。

### BUG-007 命令面板 11 条静态命令的 label 全是英文，中文查询零命中（中文 UI 里的本地化缺口）【P2 · 已修复】

**发现时间**：2026-09-11 真实浏览器逐按钮巡检（Ctrl+K）

**现象**：面板整体是中文（标题「命令面板」、占位「搜索命令或运行事件…」、页脚「↑↓ 选择 Enter 执行 Esc 关闭」），但**可搜索的条目文案全英文**：`Toggle Run Inspector` / `Jump to Latest Event` / `Copy Run ID` / `Copy Trace ID` / `Open Trace` / `Toggle Theme` / `Focus Composer` / `Switch to Compact|Balanced|Detailed|Raw`。

**实测（同一面板，逐个输入）**：

| 输入 | 命中 |
| --- | --- |
| `Theme` | `Toggle Theme`（+4 条 fuzzy 子序列命中的事件项） |
| `toggle` | `Toggle Run Inspector` / `Toggle Theme` |
| `Compact` | `Switch to Compact` |
| `Copy` | `Copy Run ID` / `Copy Trace ID` |
| `MARKER-A2` | `tool/call · bash {...MARKER-A2}`（事件搜索正常） |
| **`主题`** | **空** |
| **`复制`** | **空** |
| `zzzz` | 空（对照组：确属无匹配） |

**判读**：
- 过滤本身**没坏**——`lib/commands.ts` 的 `fuzzyScore` 是「子序列匹配 + 前缀/连续加分」的纯函数，大小写不敏感，输入 `Compact`/`copy`/`MARKER-A2` 都正确命中。`Theme` 顺带命中 4 条 `tool/call` 事件项也是**正确的 fuzzy 行为**（`t..h..e..m..e` 在 `tool/call · bash {"command":"sleep 1 && echo MARKER-A3"}` 里按序出现），不是 bug。
- 真正的缺口是**文案语言**：中文用户按母语输入（「主题」「复制」「密度」）一条都搜不到，而工具栏上的同名按钮写的正是「紧凑/均衡/详细」「切换主题」。同一功能两套语言。

**归因：前端（文案）**。不是逻辑缺陷，是本地化一致性问题。

**为什么最终判定为「缺陷」而不是「产品文案决定」**：面板的 `hint` 早就是中文（`右栏` / `定位` / `输入框`），只有 `label` 是英文——这是**本地化做了一半**，不是刻意的英文设计。工具栏同名按钮的 `aria-label` 也是中文（`切换主题` / `收起 Inspector`）。两处中文夹着一处英文，用户按母语搜索却零命中，属可修的一致性缺陷。

**修复实现（2026-09-11）**：
1. `lib/commands.ts`：`CommandItem` 增加 **不显示** 的 `keywords?: string`；`filterCommands` 的匹配 haystack 改为 `label + ' ' + keywords`。label 在前，所以**只按 label 命中的那条打分逐字不变**（追加文本不会移动 label 字符的贪心下标；已穷举 3 字符以内 query 验证：0 个既有命中失分或改分）。但**跨命令仍按分数比大小**——一条命令靠 keywords 命中且分高于另一条靠 label 命中的，就会排到前面（例：query `to` 下 `切换主题` 经 keyword `toggle theme` 高于 `切换 Run Inspector` 的 `Inspector`）。这是可接受的：keywords 的存在就是为了让中文 label 的命令仍能被英文搜到。
2. `App.tsx`：11 条静态命令的 `label` 全部改中文，与工具栏/提示口径对齐——`切换 Run Inspector` / `跳到最新事件` / `复制 Run ID` / `复制 Trace ID` / `打开 Trace` / `切换主题` / `聚焦输入框` / `切换到紧凑|均衡|详细|Raw`（密度名与工具栏四档同名）。英文原词进 `keywords`，老用户的英文肌肉记忆不作废。
3. 事件项（`tool/call · bash {...}`）**保持原样**——那是后端事件类型，本就属技术词汇。

**验证（真实浏览器，同一面板）**：

| 输入 | 修复前 | 修复后 |
| --- | --- | --- |
| `主题` | 空 | **切换主题** |
| `复制` | 空 | **复制 Run ID** |
| `密度` | — | 切换到紧凑 / 均衡 / 详细 |
| `紧凑` | — | 切换到紧凑 |
| `toggle theme` | Toggle Theme | **切换主题**（经 keywords，英文仍可搜） |
| `Copy Run` | Copy Run ID | **复制 Run ID**（经 keywords） |
| `compact` | Switch to Compact | 切换到紧凑（经 keywords） |

静态条目现状（**11 条**）：`切换 Run Inspector` / `跳到最新事件` / `复制 Run ID` / `复制 Trace ID`* / `打开 Trace`* / `切换主题` / `聚焦输入框` / `切换到紧凑` / `切换到均衡` / `切换到详细` / `切换到Raw`。打 * 的两条按契约条件出现（取决于该会话的 run 终态事件是否带 `trace_id`/`trace_url`）；实测会话 `f181c5ce` 上 **11 条全部渲染**，两条 trace 命令也都能正常工作（见 OBS-010）。事件项（约 39 条 `tool/call · bash {...}`）的 label **保持英文**，那是后端事件类型。

**回归锁**：`e2e/i-keyboard.spec.ts` 的 Copy Run ID 用例改为**英文查询 + 中文条目**——一条测试同时锁「label 已本地化」与「英文别名仍命中」；`lib/commands.test.ts` 新增 3 例（中文 label 命中 / 英文 keywords 命中 / 无 keywords 条目不回归）。

**参考 deepseek harness 的结论（用户要求「能抄就抄」）**：查了 `docs/RESEARCH_DEEPSEEK_HARNESS_WEB.md`——dsh 的命令面板/侧栏本地化不在其研究范围内（该文档聚焦会话/流式/审批/恢复），本条无可直接抄的设计。有一处相关差异记录在案：dsh 的重连是**服务端权威快照**（`history.ts:180-200`，无客户端游标），我们的契约是 `?after_seq=` 游标重放（这是本项目后端 T4 #97 定的，不是前端能选的），所以 BUG-006 按游标实现是当前契约下的正解，不适用「改成快照」。

---

### OBS-010 会话**列表**端点的 `trace_id` 恒为 null（但事件里有，命令照常工作）【观察项 · 后端·已订正】

> ⚠ **本条已订正**：初版写成「70 个会话 trace_id 全为 null → Copy Trace ID / Open Trace 永不出现」，**结论是错的**。错因是只查了会话列表端点，没查事件。实录订正如下。

**实测（2026-09-11）**：
- `GET /api/sessions`：70 个会话，**`trace_id` 全部为 null**。
- 但**事件里有**：会话 `f181c5ce` 的 `run/failed`（seq 33）携带
  `trace_id: 2482fcee980f20ad111e3d19289043b1` +
  `trace_url: https://jp.cloud.langfuse.com/project/…/traces/2482fcee…`。
- 命令面板的两条命令**确实出现且工作**（同一会话上实测）：
  | 命令 | 实测行为 |
  | --- | --- |
  | `复制 Trace ID` | 拦截 `clipboard.writeText` 拿到实参 = `2482fcee980f20ad111e3d19289043b1`（与事件逐字一致）✓ |
  | `打开 Trace` | 拦截 `window.open` 拿到 `https://jp.cloud.langfuse.com/project/…/traces/2482fcee…`，`_blank` ✓ |

**判读**：Langfuse **已接入**。前端这两条命令的可见性取自 **`conversation.trace_id` / `conversation.trace_url`**（由 `projection` 从 run 终态事件抽取，契约 2d7f87a），**不依赖会话列表**——所以列表端点 trace_id 为 null 完全不影响它们。

**真正剩下的后端小观察**：`GET /api/sessions` 的 `trace_id` 字段**从来没有值**（不是「本部署没接 Langfuse」，是该端点没回填）。当前前端不用它（命令走事件、会话行也不显示 trace），所以**无用户可见影响**；但若将来想在会话行上直接标 trace，要先把该字段回填。

**归因：后端（会话列表端点未回填 trace_id）**。**前端无需改动**。

---

### OBS-008 `model/failed: RuntimeError`（glm-5.3-flash）紧跟在成功的 tool/result 之后【观察项 · 后端/provider】

**发现时间**：2026-09-11 真实浏览器刷新一致性测试中顺带记录（会话 `1fdac9b9`）

**事件真值**（`GET /events`，seq 404–409）：

```
404 tool/call      bash {"command": "sleep 8 && echo proof-8842"}
405 tool/output_delta  stdout: "proof-8842\n"
406 model/completed  model=glm-5.3-flash usage=9521  tool_calls=[bash]
407 tool/result      {"ok": true, "exit_code": 0, "stdout": "proof-8842\n", "duration_ms": 8115.9}
408 model/failed     "model call failed: RuntimeError"
409 run/failed
```

**判读**：工具**成功**（`ok:true, exit_code=0`），随后**模型调用**抛 `RuntimeError` → run 收口为 failed。UI 显示「失败」并保留工具成功输出，是对事件真值的忠实渲染，**不是前端 bug**。

**归因**：后端/provider。与既有 OBS-001 同族（同一模型 `glm-5.3-flash` 此前返回 400 BadRequestError），本次是 `RuntimeError`——同一默认链上的模型不稳定，值得后端排查（`model call failed: RuntimeError` 的原始异常被吞成了这一句，建议把 traceback 落进日志/JSONL 以便定位）。

**前端侧无需修**：失败态、错误横幅、后续可重试都已有既有通路。

---

### OBS-009 bash 工具执行上限 10.0 秒，`sleep 10` 恰好越界 → `TIMEOUT`（且 `retryable:false`）【观察项 · 后端工具运行时】

**发现时间**：2026-09-11 BUG-006 实机取证时顺带命中（会话 `affd6084-…`，seq 6/8）

**事件真值**：
```
seq 6  tool/call   bash {"command": "sleep 10 && echo MARKER-A1"}
seq 8  tool/result {"ok":false,"message":"工具 'bash' 执行超时（上限 10.0 秒）…",
                    "error_code":"TIMEOUT","retryable":false,
                    "metadata":{"attempt":1,"max_attempts":3,"duration_ms":10002.6}}
```
模型随后自行改写成 `sleep 1 && echo MARKER-A1` 并在 seq 14/17 成功，最终 `run/completed`。

**判读**：`sleep 10` 与上限 10.0s 贴边，必然越界。**前端渲染正确**——Timeline 第 8 行显示 `tool/result 失败 TIMEOUT`，同一 run 的 pulse 仍是「已完成」，因为工具失败可被模型恢复：这是忠实且正确的语义（工具失败 ≠ run 失败）。

**归因**：后端工具运行时（`bash` 超时上限 10s）。**两点值得后端确认（非前端，未改）**：
1. 超时被标 `retryable: false` —— 与不变量 #8「Tool Retry 归 ToolExecutor」的取舍值得确认：若 TIMEOUT 一律不重试，则瞬时超时只能靠模型自己重发（本例正是如此）。
2. 上限 10s 对 `sleep`/长构建类命令偏紧，且错误文案已说明「可稍后重试」却标 `retryable:false`，二者读起来略冲突。

---

### BUG-005 页面刷新后选中的会话与全部内容丢失（回到空态）【P1 · 已修复（138b056）】

**发现时间**：2026-09-11 真实浏览器测试（用户预判要求：刷新后会话必须与刷新前一致）

**测试环境**：真实浏览器（Chrome DevTools MCP）→ `http://localhost:5173/`（feat/frontend worktree 的 vite）；后端 `http://127.0.0.1:8000`（集成版 `agent_harness.web.app`）；会话列表 **67 个真实会话**。

**复现**：点左侧会话 `1fdac9b9-1dee-4fc5-95d3-d4ab2787b004`（305 事件）→ 按 F5 刷新。

**证据（刷新前后同脚本实测）**：

| 观测量 | 刷新前 | 刷新后 |
| --- | --- | --- |
| 侧栏选中行 | `1fdac9b9-…`（class `session-item selected`） | **无**（`selected: null`） |
| `.turn` 轮次 | **8** | **0** |
| 会话正文指纹 / 长度 | `-271347586` / 4102 字符 | **0 / 0 字符** |
| 主区 | 8 轮对话 + 「已完成 · 4,339 tok」脉冲 | **「暂无对话 / 在下方提交任务」** |
| 右栏 Inspector | 「RUN INSPECTOR / 已完成 / run 2ab617e9 / 显示最近 200 / 共 305 条」 | **「未选择会话 / 从左侧选择，或开始新任务。」** |
| 地址栏 | `http://localhost:5173/` | 同（**不含任何会话标识**） |
| `localStorage` | `ahi.theme` / `ahi.traceDensity` | 同（**存活**） |

**结论**：会话列表（67 条）还在，但**选中的会话与它的全部内容在刷新后消失**，主区退回空态。持久化的只有主题/密度/API token——**会话选择没有持久化**。

**归因：前端。** 后端数据完好（刷新后 67 条列表仍由 `GET /api/sessions` 返回，事件也在 JSONL 里）；丢的是 UI 的选中状态。

**根因**（代码可证）：
1. `web/src/hooks/useSession.ts:232` —— `const [mode, setMode] = useState<SessionMode>({ kind: 'idle' })`，**初始态恒为 idle**，没有任何恢复路径。
2. 全仓 `grep localStorage` 只有三处：`lib/theme.ts`、`lib/density.ts`、`lib/auth.ts`（token）。**没有第四处保存会话选择**。
3. `grep 'location.hash|history.replaceState|URLSearchParams|useSearchParams'` 在 `web/src` **零命中**——URL 里也没有会话标识，所以刷新既恢复不了，也无法分享/收藏会话，浏览器前进后退也不生效。
4. 初始加载 effect（`App.tsx:149`）只做 `refreshSessions() / fetchModels() / fetchControlCatalogs()`，**不恢复选中**。

**影响**：任何刷新（F5、误触、崩溃恢复、切标签页后重载）都会丢掉当前正在读的会话——长会话尤其痛（用户可能已经翻了很久）。属于「功能可用但体验破损」= P1。

**修复方向**：
1. 持久化选中的 `session_id`（沿用本仓既有 localStorage 惯例，键名 `ahi.` 前缀，如 `ahi.selectedSession`），在 `selectSession` / 分叉跳 child / 新任务落定 sid 时写入；显式回到「新建会话」空态时清除。
2. 首屏在会话列表返回后恢复：存了 id 且**该 id 仍在列表中** → `selectSession(id)`；不在（已被删/不存在）→ 清键并保持空态（不造假入口）。
3. `viewing` 分支本来就会 `GET /events` 重建（`projectHistory`，不变量 #22），所以恢复只需把 mode 设对，不必新增数据通路。
4. 待验证并决定：**流式中刷新**（下面是 BUG-006 的测试）、以及刷新后滚动落点是否要跟随内容。

**未修原因**：本次先完成取证与登记（用户要求实时写入），修复在后续步骤按 `/implement` 进行。

**修复实现（2026-09-11）**：
- 新增 `web/src/lib/sessionRestore.ts`（无 React 依赖，可单测）：`SELECTED_SESSION_KEY = 'ahi.selectedSession'`、`readStoredSessionId` / `writeStoredSessionId`（localStorage 不可用时静默降级，不抛）、`maxEventSeq`（供 BUG-006 接流游标）。
- 新增 `web/src/lib/sessionRestore.test.ts`（4 例）：读写成环；`null` 删键；storage 被拒时不抛不读；`maxEventSeq` 跳过 null / 空集返回 `-1`。
- `web/src/lib/api.ts`：新增 `NotFoundError`，`getSessionEvents` 遇 404 抛它——用于「存的 id 已被删」时静默回落空态，而不是弹错误。
- `web/src/hooks/useSession.ts`：
  - `mode` 改**惰性初始化**：`useState(() => readStoredSessionId() ? {kind:'viewing', sessionId} : {kind:'idle'})`——首帧即选中，**不出现空态闪烁**（这是替换「先 idle 再 effect 里 setMode」写法的原因：后者既闪一下空态，又触发 oxlint `set-state-in-effect`）。
  - 新增持久化 effect：`idle → 删键`；`live/viewing(sid) → 写 sid`。三态 `SessionMode` 是唯一真相（不变量 #22），持久化挂在它上面，不新增第二份状态。
  - 历史装载 `.catch`：`NotFoundError → 删键 + 回 idle`（陈旧 id 自愈）。

**修复验证（同一脚本，刷新前后对照）**：
| 观测量 | 修复前（刷新后） | 修复后（刷新后，**零点击**） |
| --- | --- | --- |
| 侧栏选中行 | 无 | **`1fdac9b9-…`（与刷新前同一行）** |
| `.turn` 轮次 | 0 | **8** |
| 正文指纹 / 长度 | `0` / 0 字符 | **`-271347586` / 4102 字符（与刷新前完全一致）** |
| 主区 | 「暂无对话」空态 | 8 轮对话 + 「已完成 · 4,339 tok」脉冲 |
| 右栏 Inspector | 「未选择会话」 | 「RUN INSPECTOR / 已完成 / run 2ab617e9 / 共 305 条」 |

→ 刷新后与刷新前**逐项一致**（用户要求的「刷新后会话要和刷新前一致」达成）。

---

### BUG-006 流式（run 在途）期间刷新页面：刷新后停在静态快照，不再继续接收【P1 · 已修复（已实机取证）】

**发现时间**：2026-09-11，随 BUG-005 的设计评审一并识别（BUG-005 修好只是「刷新后还能看见已落盘的历史」，**在途 run 的后续事件**是另一个问题）。

**问题**：`viewing` 分支只 `GET /events` 取一次历史快照就收工。若刷新时该会话的 run **仍在后端跑**（ADR-0016 detached-run：订阅者断开不影响 run 继续执行），刷新后 UI 会显示一个「停在半截、看起来已完成」的静态画面，后续事件（工具输出、模型回复、run 终态）再也进不来——直到用户手动再发一条消息。这是**数据正确性**问题：用户看到的不是当前真相。

**归因：前端。** 后端能力齐备且已实测：`GET /api/sessions/{id}/stream?after_seq=N`（T4 #97）会先重放 `after_seq < seq ≤ cursor` 的耐久事件，再**无缝接上实时流**；detached-run 确认有效（实测一次中途刷新，新会话仍产出 69 条事件）。缺的是前端刷新后**主动去接**。

**修复实现（2026-09-11）**：
1. `attachLiveStream` 增加 `opts.resume` + 帧计数器 `framesSeen`。
2. 历史装载后：`hasUnterminatedRun(events)`（`lib/runState` 既有纯函数，判「最后一个 run 无终态」）为真 → `resumeLiveStream(sid, maxEventSeq(events), projected)`。游标取自**已加载事件的最大持久 seq**，正好落在后端「重放 + 续流」契约上，不重不漏。
3. 新增 `resumeLiveStream`：清旧流 → 置 `live` → `streamSession(sid, afterSeq)`；404 则交回历史重跑裁决（已知会话被删 → `NotFoundError` → 清键回 idle，见下方审查 #1）；失败只回 `viewing` + 报错，**不把已渲染的历史打回空态**。
4. **零帧收流兜底**（关键）：若 `resume` 流**一帧未收到就 EOF**，说明「看的时候 run 其实已经跑完了」（刷新与 run 收尾的竞态）——此时**静默回落 `viewing`**，绝不弹「连接断开/重连中」。判据用 `framesSeen === 0 && !terminalSeenRef.current`。
5. **防死循环**：`resumeAttemptedRef` 按 **(sid, 游标)** 记账（`nextResumeAttempt` 纯函数，首轮审查后由 `Set<string>` 改造而来）。若不做这层，上面第 4 条会在「viewing → 接流 → 空流 → viewing」上无限转（该 bug 是自查发现的，已消除）；零帧空流不带来新事件 → 游标不变 → 第二次被拦下。按游标而非只按 sid，是为了不误伤「切走再切回来接着看」——并且 `selectSession`（用户显式切会话）会 `forgetResumeAttempt` 再彻底放行一次。刷新页面即重置，记录清空。

**已验证**：刷新与 run 收尾的竞态（第 4 条）——在 run 恰好于刷新窗口内完成的会话上刷新，UI 稳定落在 9 轮 / 「已完成 · 9,251 tok」，终态文本完整，**无错误横幅、无重连横幅**（兜底按设计静默生效）。

**实机正向取证（2026-09-11，决定性）**：

测试任务：新会话发 `用 bash 工具依次执行三条命令：sleep 10 && echo MARKER-A1 / A2 / A3…`（预计在途 ~30s）。

1. 提交后 5s，run **确在途**：新会话 `affd6084-1c04-4580-b24a-150387cc0fbb`，`pulse = 思考中 · 15s`，事件仅到 `run/started`、`model/started`；`ahi.selectedSession` = 该新会话。
2. **此刻 F5 刷新**（run 未结束）。刷新后 3s 观测：
   - 侧栏选中行已恢复 = `affd6084-…`；`stored` 同值；用户消息在。
   - `reconnecting = false`、**无「连接断开/重连」横幅**、**无空态**。
   - Inspector 已渲染事件 0–6，其中 `6 tool/call bash {"command":"sleep 10 && echo MARKER-A1"}`。

3. **网络层铁证**：`reqid=2380 GET /api/sessions/affd6084-…/stream?after_seq=2 [200]`。
   说明：刷新时 `GET /events` 快照只到 **seq 2**（`run/started`），`maxEventSeq` = 2 → 前端以 `after_seq=2` 接流；**事件 3–6（text/delta、tool/call）是经由这条流重放+续送达的**，而不是历史快照。

4. **零交互前进（决定性）**：此后**不碰页面**等待 16s，事件从 **3 → 54** 条，Inspector 末条 = `53 run/completed`，`pulse = 已完成 · 15,306 tok`。静态快照不可能自增——**证明实时流已接上并跟到终态**。

5. **与后端真值逐项对账**（`GET /api/sessions/{id}/events`）：服务端 54 条、seq 0–53、**重复 0、空洞 0**；UI 显示 54 条、末条 53。→ 「无缝无重复」契约在 UI 侧成立（游标取自已加载事件的最大持久 seq，恰好落在重放区间）。

**结论**：BUG-006 修复有效。「刷新后会话与刷新前一致」不仅覆盖已落盘历史（BUG-005），也覆盖**在途 run 的后续事件**（BUG-006）。

**附注**：本次 run 用的模型是 `deepseek-v4-flash-0731`，全程正常收口（`run/completed`）。对照 OBS-008 的 `glm-5.3-flash` 失败——**佐证 OBS-008 是 provider 侧的模型不稳定，与前端无关**。

---

### BUG-001 分叉按钮 422：`turn.step_id` 不是后端要的用户消息 seq 【P0 · 已修复（8469a34）】

**发现时间**：2026-09-10 真实浏览器点击测试（会话 28eb3302，点第 2 轮的「分叉」）

**修复时间**：2026-09-10（commit `8469a34`）；回归锁与补强 2026-09-11

**现象**：点击用户消息上的「分叉」按钮 → `POST /api/sessions/{id}/forks` 返回 **422**，UI 无任何反馈（静默失败）。

**网络证据**：
- 请求体：`{"from_seq": 2}`
- 响应：`{"detail": "fork 边界 seq=2 不是父会话中的用户消息（可用边界: [1, 30, 46, 62, 199]）"}`

**根因**（`web/src/components/Conversation.tsx:290`）：
```tsx
onClick={() => onFork(turn.step_id)}
```
前端传的是 `turn.step_id`——这是投影层 `resolveStep` 为轮次分组**前端合成**的 step 值；而后端 fork 锚点要求的是**用户消息事件在 JSONL 里的 `seq`**（后端报错里列的可用边界 `[1, 30, 46, 62, 199]` 正是该会话 5 条 user/message 的 seq）。真实数据里 `user/message` 事件的 `step_id=None`，两个语义完全不同。

**修复方向**（最小方案）：
1. 投影层：`projectUserMessage` 已经拿得到 `event.seq`（user/message 的 seq 就是合法锚点），在 `Turn` 上记录 `user_message_seq: number`（per-turn 事实，与 T9 turn_index 落当轮同一模式）。
2. `Conversation.tsx:290`：`onFork(turn.user_message_seq ?? turn.step_id)` 改为只传 `user_message_seq`；没有该字段的历史 turn 显示分叉按钮但点击时给出提示，或对缺锚点的轮不渲染按钮（不造假入口）。
3. 错误反馈：`handleFork`（`web/src/App.tsx:312`）的 `catch {}` 是**空吞**——422/409 用户毫无感知。至少 `setError(...)` 显示后端 detail；409（在途 run）单独提示「等当前 run 结束再分叉」。

**实际落地**：
1. `Turn.user_message_seq: number | null`（`types.ts`）；`projectUserMessage` 仅在 `event.seq !== null` 时写入（`projection.ts`）——不伪造锚点。
2. 按钮只在 `onFork && status !== 'streaming' && user_message_seq !== null` 时渲染，点击传 `user_message_seq`；缺锚点不造假入口。
3. `forkSession`（`api.ts`）解析后端 `detail`；`handleFork` 的 catch 改为 `setForkError(\`分叉失败：${message}\`)`，渲染为 `role="alert"`。
4. 2026-09-11 code-review 补强：
   - 第 1 轮的按钮加 tooltip「本轮之前没有历史：child 会话将是空会话」——后端允许首个 user 消息当锚点，得到的 child 继承 model + 空 workspace，合法但反直觉，提前说清（交接手册 §B.3）。
   - `handleFork` 的结局（跳 child / 弹错误）落地前用 `selectedIdRef` 校验用户仍停在发起会话上，切走即丢弃（stale-write 纪律）；切会话的两条入口（会话栏选择 / 分叉跳 child）**同步**写该 ref，把「已切走但 effect 还没 flush」的窗口压到零。
   - `forkError` 带上发起它的 `sessionId`，只在与当前选中会话一致时渲染——**不是**在切会话时清空（少了 effect 与额外渲染）；代价是切回原会话仍会看到那条旧错误，符合「它确实是在那个会话上失败的」。
   - 分叉请求在途时忽略重复点击（`forkInFlightRef`）：按钮没有 pending 态，连点会在后端造出两个 child 会话。
   - 后端**未**新增端点——遵循交接手册 §B.4「建议不加」。

**回归**：
- `web/e2e/b-fork.spec.ts` B1 锁 `from_seq === 30`（user/message 的 seq，而非 turn 序号 2）；夹具刻意让 seq 与 turn 序号错开，且 `user/message` 不写 `step_id`（真实信封形状）。**变异测试**：把 `onFork(turn.user_message_seq!)` 改回 `onFork(turn.step_id)` → B1 变红；改回即绿。
- `web/src/lib/projection.test.ts`：新增两条——持久 seq 落到 `user_message_seq`（不写合成 step 号）、`seq === null` 不伪造锚点。
- `web/src/components/Conversation.test.tsx`：`TurnView — 分叉入口（BUG-001）` 6 条 SSR 契约。

---

### OBS-005 非默认 amend 档位会让模型调用 400【观察项 · 疑似后端/provider】

**发现时间**：2026-09-11 真实浏览器点击巡检

**现象**：把 Composer 控制行改成 `Agent Profile=编程` + `Reasoning Effort=深度` 后提交任务，连续 2 次都在 `seq=3` 落 `model/failed: "model call failed: BadRequestError"` → `run/failed`（会话 `ba4faa9a-…` / `70cca9c0-…`，`D:\intelligence-agent` 存储）。把两个档位都改回默认（`通用` / `标准`）后**同一任务立刻正常流式输出**。

**初步判断**：`agent_profile` / `reasoning_effort` 作为 amend 字段透传给 provider 时被拒（400）。前端只是把控制行选中的值放进 payload（`api.test.ts` 已锁 payload 形状），没有加工——**疑似后端/provider 侧对这些档位的处理**。需要后端确认这两个档位在 `senseaudio` 上是否受支持。

**前端表现（正常）**：run 失败被正确消费为「失败」run pulse + turn 渲染，无崩溃、无假成功。

---

### OBS-006 审批卡（#37）在本 UI 中不可达且无测试【观察项 · 覆盖缺口】

**发现时间**：2026-09-11 真实浏览器点击巡检（逐按钮清点）

**现象**：`ApprovalCard` 由 `Conversation` 按 `pending_approvals` 渲染，但：

1. **本 UI 无法触发**：`App.tsx` 提交任务时硬编码 `auto_approve: true`，所以从这里发起的 run 永远不会产生待审批项（只有「在别处以 `auto_approve=false` 发起的会话、再在本 UI 打开」才可能看到卡）。
2. **无测试**：`src/components/ApprovalCard.tsx` 没有单测文件，`e2e/` 也没有对应 spec——16 个 spec 里一个都没有审批场景。

**影响**：这个交互（批准/拒绝按钮 → 后端回调）目前既点不到也测不到，属未验证区域。本轮不做功能改动（超出交接手册 A–D 范围），仅登记。

---

### BUG-004 命令面板「Copy Run ID」复制的是 session id【P2 · 已修复（本次）】

**发现时间**：2026-09-11 真实浏览器点击巡检（Ctrl+K 面板逐条点击）

**现象**：命令面板执行「Copy Run ID」后，剪贴板得到的是**会话 id**，不是 run id。标签与动作不一致——两者都是 UUID，粘到日志查询/后端工单里只能用错地方才发现。

**根因**（`web/src/App.tsx` 创建命令处）：
```tsx
label: 'Copy Run ID',
hint: conversation ? conversation.session_id.slice(0, 12) : undefined,
run: () => conversation && copyText(conversation.session_id),
```
`ConversationState.run_id` 本来就存在（`types.ts`，注释写明「PRD §8.2 Inspector 头部 Run ID」），命令却抄了近处的 `session_id`。同区域的 `Copy Trace ID` / `Open Trace` 有 splice 兜底（值缺则命令移除），这条没有——`run_id` 为空时会留下一个点了没反应的假按钮。

**修复**：`run` 改为 `copyText(conversation.run_id)`；`run_id` 缺失时该命令整个不进入列表（条件展开生成，与 Trace 两条的「值缺则不出现」同一纪律）。

**回归**：`e2e/i-keyboard.spec.ts` 新增用例——夹具里 run id(`e2e-run-0002`) 与 session id(`e2e-session-0002`) 刻意不同，断言剪贴板等于 run id。**变异测试**：把 run 改回 `copyText(conversation.session_id)` → 断言变红（`Expected "e2e-run-0002" / Received "e2e-session-0002"`），改回即绿。

---

### BUG-003 工具输出面板内滚动会误触发对话「脱离跟随」【P1 · 已修复（本次）】

**发现时间**：2026-09-11 独立 code-review（Standards 轴，非用户报告）

**根因**：修 BUG 里的滚动问题时给 `.conversation-scroll` 加了 `wheel` 监听（用户上滚 → 同步脱离跟随）。但 `wheel` **冒泡**：工具输出面板 `.tool-out-body`（`max-height: 200px; overflow-y: auto`）与 reasoning 展开体本身就是独立滚动容器，在它们内部上滚同样会冒到会话容器，于是**对话根本没动**却脱离了跟随、弹出「↓ 最新」，之后所有 delta 都不再贴底。这是本次修复引入的回归（改动前的代码没有 wheel 监听）。

**修复**：`onWheel` 先沿 `e.target` 向上走到会话容器，把链上每个「可上滚的嵌套滚动容器」（`scrollHeight > clientHeight` 且 `overflow-y: auto|scroll`）的 `scrollTop` **累加**，再与本次上滚的位移比较：合计够 = 嵌套链吃下了这一下，忽略；不够 = 外层会被推动，释放跟随。累加（而不是「遇到第一个有余量的就忽略」）是必须的——浏览器按最内层→外层顺序分担，单看最内层会在内外层分担时误判「外层要动」，而「有余量就算」会在外层确实被推动时误判为不动（跟随仍为真、下一次 delta 把视口拽回，即原症状）。位移按 `deltaMode` 折算（像素 / ×16px 每行 / ×视口高每页）。算术收在 `lib/followLatest.ts` 的 `nestedChainAbsorbs` / `wheelDeltaPixels`。

另外补一条同类误报：容器已在顶部（`scrollTop <= 0`，内容没超视口或已滚到头）时上滚什么都不会动，不算「要离开底部」——`onWheel` 直接返回，否则浮标会为一次没发生的滚动弹出来。

**回归**：`lib/followLatest.test.ts` 锁累加语义、边界与 `deltaMode` 折算（含空链）；`e2e/j-scroll.spec.ts` 的浮标用例覆盖「对话容器内上滚」这一半；**按 DOM 走链收集余量那一半**（组件内 `parentElement` 循环）无自动化，属人工验证范围。

---

### BUG-002 交接手册 `HANDOFF_WORKBUDDY_FRONTEND.md` 关于 T9 的结论已过期 【P2 · 已勘误（2026-09-11）】

**发现时间**：2026-09-10 真实浏览器测试

**现象**：手册写「T9 未完成——TurnView 不渲染轮次标签」，但实际页面已真实渲染「第 1 轮」~「第 6 轮」。

**根因**：commit `cddea36`（T9 轮次标签 UI）已在当前 HEAD 里，手册是基于旧快照写的。**workbuddy 若照手册做会重复实现。**

**勘误（2026-09-11）**：手册 §1/§2 已正确写明「T9 已完成（`cddea36`）」，但 §7 注意事项第 5 条仍留着旧结论「T9 是唯一剩余工作」，**文档内部自相矛盾**——照做仍会重复实现。已删除该条并替换为指向 `SDD_TICKET_TRACKER.md` 的当前剩余项；§6 门禁基线（408/46）同时刷新为当日实跑值（494/96）并标注日期。

---

### OBS-001 续聊 run 失败：`model call failed: BadRequestError` 【观察项 · 非前端 bug】

**发现时间**：2026-09-10 测试「会话级模型切换后续聊」

**现象**：切到 `glm-5.3-flash` 后发消息 → 后端 `model/failed`（`BadRequestError`）→ `run/failed`。

**前端表现**（正常）：header 显示「失败」、第 6 轮 turn 渲染出来、无崩溃。前端行为正确。

**初步判断**：后端模型调用问题（provider `senseaudio` 对该模型返回 400），属后端/环境问题。前端已正确消费 `model/failed` + `run/failed` 终态。

**附注**：这次失败同时验证了 T7 的链路真实可用——`model/changed` 事件（seq=319，`from qwen3.8-27b → to glm-5.3-flash`）落库，续聊请求带上了 amend 档位（`model`/`agent_profile`/`reasoning_effort`）。

---

### OBS-002 preset 任务按钮只在空态显示 【观察项 · 符合设计】

三个 preset 按钮（FizzBuzz / todo.md / 目录结构）只在未选会话的空态渲染，选中会话后消失。符合「空态引导」设计，非 bug，登记备查。

---

### OBS-003 恢复反馈一度会把「补齐 run 终态」谎报成「无可修复项」【已修复（本次）】

**发现时间**：2026-09-11 独立 code-review（Standards + Spec 双轴）

**现象**：`repaired` 只统计回填的 tool/result 条数。当后端只补上了缺失的 run 终态（没有 dangling 工具）时，`repaired === 0` 且入口消失 → 提示落到 `已恢复：无可修复项（会话事件已完整）`——**日志本就不完整、恢复确实修了它**，这句话是谎报。

**修复**：`hasUnterminatedRun(events)` 判定「最后一个 run 有头无尾」；recover 前后各算一次得到 `terminalRepaired`；文案收敛到纯函数 `recoverDoneMessage()`，各结局分别表述（回填 N 条 / 补齐终态 / 两者都有 / 后端没修完可重试 / 真无可修）。两个「没修完」的原因（缺终态 / 悬空工具调用）**分开**传入——`isRecoverableRun` 是 OR，压成一个布尔会在「终态已补、只剩悬空调用」时误报「仍缺 run 终态」。

**回归**：`runState.test.ts` 八条分支单测（含「repaired>0 但仍缺终态必须附可重试提示」「只剩悬空调用不得说仍缺终态」）+ `d-recover.spec.ts` 新增「只补终态」用例（断言不出现「无可修复项」/「已完整」）。

---

### OBS-004 `run/interrupted` 的 `step_id` 缺失：后端排查线索（交接手册 §D 的回执）【已确认 · 非前端 bug】

**发现时间**：2026-09-11

**排查方法**：扫两个 worktree 的真实 JSONL（**当日快照**：71 个会话；后续会话继续累计，比率是承重结论）。

**结果**：`run/interrupted` 共 **4 条**，其中 **2 条的 `step_id` 字段整个缺失**（不是显式 null）：

| session_id | run/started seq | run/interrupted seq | step_id |
| --- | --- | --- | --- |
| `c63ce4d3-3b26-40bb-8e8c-e3af8dd33035` | 2 | 3 | 缺失（`reason: process_restart`） |
| `2f2f3187-fccc-4704-b9a4-f259f82d153d` | 2 | 3 | 缺失（`reason: process_restart`） |
| `f181c5ce-7c84-43f9-b249-efa606293268` | 2 | 15 | 3 |
| `3b35b83d-dcae-476f-8343-9912e40e77d7` | — | 34 | 1 |

**语义**：这两条里 `run/started`（seq 2）之后**没有任何带步号的事件**就中断了（seq 3 就是 `run/interrupted`）——进程在该 run 的第一个步骤开始前死亡，检测器（`detect_unterminated_runs` → `recovery/scan._mark_interrupted`）无从沿用 step_id，于是信封不带该字段。

**前端处置**：`projectRunInterrupted` 把缺失强制为 `null`；横幅文案区分「首个步骤开始前中断」与「第 N 步中断」，不再渲染「第 ? 步」。e2e `d-recover.spec.ts` 两条锁死。

**后端待办**（交接手册 §D 请求的复现 id 即上表）：`detect_unterminated_runs` 的取值路径可对照这两条确认——是「沿用最后见到的 step_id」在无可用值时留空，还是另有分支。

---

### OBS-007 中断的会话显示绿色「已完成」脉冲 + 过期不消的「上次运行…中断」横幅【P2 · 已修复（b4181ad）】

**发现时间**：2026-09-11（独立 code-review，Spec 轴）；**本轮真实浏览器复现并修复**

**现象（实测复现，会话 `c63ce4d3`）**：同一屏同时出现
- `run-pulse pulse-completed` + 文案「已完成 · 2,583 tok」（**绿色对勾**）
- `.interrupt-banner`「上次运行在首个步骤开始前中断（原因：process_restart）」

**真相（查事件后）**：该会话是 `run/started`(seq 2) → `run/interrupted`(seq 3) → **`run/started`(seq 9) → `run/completed`(seq 14)**。也就是说**中断的那次运行早已被后来一次成功的运行取代**——横幅是**过期提示**，而脉冲说的是**最新 run 的真实结局**。两者打架的根因不是脉冲错，而是**横幅不清**。

**根因（两处，缺一不可）**：
1. `projection.ts: projectRunStarted` 只置 `run_status='running'`，**从不复位 `run_interrupted`** → 该标记一旦置上就挂到会话生命结束；`App.tsx:671` 的横幅只看 `run_interrupted && !streaming` → 永久显示。
2. `projectRunInterrupted` 调 `finalizeRun(state, 'completed', …)`（冻结决策 69：中断 ≠ 失败，而 `finalizeRun` 只有 completed/failed 两档）→ 被中断且**此后再没跑过**的会话，脉冲是绿色「已完成」。

**修复实现**：
1. `projectRunStarted` 清空 `state.run_interrupted`——新 run 开始即「上次运行」已被取代，提示随之过期。语义收窄为：**`run_interrupted` = 最近一个 run 以中断收口**。
2. 新增第四种终态脉冲 `interrupted`：`RunPulseState` + `PULSE_TABLE`（`已中断` / `pulse-interrupted` / `CircleSlash`）+ `coarsenPulseState`（Inspector Overview 同步说「已中断」）+ CSS（复用既有中性语义 token `--text-secondary`/`--color-hover`/`--border-subtle`，**不新增 `:root` 变量，§15 无需补亮色覆盖**）。`deriveRunPulse` **优先看 `run_interrupted`，但连带恒真条件 `run_status === 'completed'`**（详见下方审查第 3 条）——因为 1 已保证该标记只反映最近一个 run，这两者在真实数据里必然同时成立。
3. 为什么必须两处一起改：只加第四态而不清标记，会让「中断后成功重跑」的会话反而显示「已中断」（比原来更错）；只清标记而不加第四态，被中断且未重跑的会话仍谎报绿色「已完成」。

**验证（真实浏览器 + 真实后端）**：
| 会话 | run 序列 | 修复前 | 修复后 |
| --- | --- | --- | --- |
| `c63ce4d3` | 中断 → **完成** | 绿色「已完成」+ 中断横幅（过期） | 脉冲「已完成」，**横幅消失**；Timeline 仍保留 `运行中断` 那一行（历史事实不删） |
| `f181c5ce` | 中断 → **失败** | 「失败」+ 中断横幅（过期） | 脉冲「失败」，**横幅消失**；Timeline 仍保留「第 3 步中断」 |

两例的**恢复入口都不受影响**（`canRecover` 由 `isRecoverableRun(events)` 判定，与本标记无关；两例会话语料本就没有 dangling，故按设计不出现入口）。

**覆盖边界（诚实说明）**：「`已中断` 脉冲」这一态在**当前真实语料里不可达**——两个含 `run/interrupted` 的会话都被后续 run 取代了，没有「中断且从未重跑」的真实会话。故该态由新增单测锁定（`runState.test.ts` 三例 + `projection.test.ts` 一例），未能在真机上目视确认。三例中两例经**变异验证**：把 `coarsenPulseState` 的中断映射改成「已完成」、或去掉 `run_status === 'completed'` 恒真条件，对应断言立刻变红，随后还原。

---

## 已验证正常的交互（2026-09-10 真实点击 17 项 + 2026-09-11 新增第 18–20 项）

| # | 交互 | 结果 |
| --- | --- | --- |
| 1 | 会话列表点击选择会话 | ✓ 319 事件加载，header 显示会话 ID / 状态 / tok |
| 2 | T9 轮次标签 | ✓ 「第 1 轮」~「第 6 轮」per-turn 正确渲染（`cddea36`） |
| 3 | 折叠按钮 | ✓ 变「已折叠 · 0 个工具 · 1 轮 · 5.1s」，内容收起 |
| 4 | Timeline → Chat 反向联动 | ✓ 点 `user/message 什么是rag` 行，chat 滚到目标轮并进入视口 |
| 5 | Timeline 行 → 事件详情 | ✓ StepDetail 打开，显示 seq=199 的 USER/MESSAGE（seq/time/event_id） |
| 6 | 密度四档切换 | ✓ `data-density` 即时生效，localStorage `ahi.traceDensity` 持久 |
| 7 | 主题切换 | ✓ dark ↔ light，`data-theme` 属性正确翻转 |
| 8 | Inspector 收起/展开 | ✓ 收起后面板移除，展开恢复，aria-pressed 正确 |
| 9 | 模型选择器打开/选择 | ✓ dialog + cmdk 列表（默认链 + 5 个模型），选中后按钮文案更新 |
| 10 | **T7 会话级模型切换** | ✓ 已选会话中选模型 → `POST /model` 200 `{status:changed}`，本地状态用响应 `model_id` 更新 |
| 11 | 权限模式选择器 | ✓ 三档（只读/工作区写入/完全访问），描述文案齐全 |
| 12 | Agent Profile 选择器 | ✓ 三档（通用/编程/研究审查），选择生效 |
| 13 | Reasoning Effort 选择器 | ✓ 三档（轻量/标准/深度），选择生效 |
| 14 | 思考块展开 | ✓ aria-expanded 翻转，reasoning 全文展开 |
| 15 | 续聊发送 | ✓ `POST /messages` 200，请求体带 amend 档位（model/agent_profile/reasoning_effort）|
| 16 | run 失败的 UI 呈现 | ✓ header「失败」+ turn 渲染，无崩溃（内容见 OBS-001）|
| 17 | 分叉按钮 | ✓ 已修复 `8469a34`（from_seq = user/message seq，422 detail 可见）——e2e 回归锁 `b-fork.spec.ts` |
| 18 | 恢复会话按钮反馈 | ✓ 已修复——成功落 `done` 提示（在 `canRecover` 门外）；文案区分「回填 N 条 / 补齐 run 终态 / 后端没修完（附具体原因）可重试 / 真无可修」；e2e `d-recover.spec.ts` |
| 19 | 流式中上滚被顶回 | ✓ 已修复——复用 `followLatest` 原语 + 瞬时贴底 + `overflow-anchor: none`；e2e `j-scroll.spec.ts` 用真实 `page.mouse.wheel` 锁可观察契约（上滚→浮标出现→点浮标回底）；**竞态本身（delta 到达时是否拽回）无法自动化**，证据见下方真机埋点 |
| 20 | 工具输出面板内上滚 | ✓ 已修复 BUG-003（wheel 冒泡导致的误脱离）；`e2e/j-scroll.spec.ts` 覆盖对话容器侧，嵌套容器侧人工验证 |

### 2026-09-11 全量逐按钮巡检（真实浏览器 + 真实后端，density 详细档）

| # | 交互 | 结果 |
| --- | --- | --- |
| 21 | 密度四档 | ✓ 紧凑/均衡/详细/Raw 均生效，`data-density` + localStorage `ahi.traceDensity` 同步，`.density-btn.sel` 跟随 |
| 22 | 主题切换 | ✓ dark ↔ light；亮色下截图逐项检查，文字/边框/工具卡/Inspector 对比度正常（无 §15 token 漏覆盖） |
| 23 | Inspector 收起/展开 | ✓ `.step-detail` 常驻 DOM，收起时 `.app-regions.inspector-closed` 归零宽度，展开恢复 320px |
| 24 | Workspace 模式 Chat/Split/Preview | ✓ 三者互斥选中，`aria-pressed` 跟随 |
| 25 | 模型选择器 | ✓ 5 项（默认链 + 4 模型），短目录无搜索框，选中后 trigger 文案更新 |
| 26 | 权限模式选择器 | ✓ 3 档带描述，选中回填 trigger |
| 27 | Agent Profile 选择器 | ✓ 3 档，选中回填 trigger |
| 28 | Reasoning Effort 选择器 | ✓ 3 档，选中回填 trigger |
| 29 | Context Provider 选择器 | ✓ **正确地不渲染**——后端 `/api/context-providers` 返回空列表，零伪造 |
| 30 | 空态 preset chip | ✓ 点击填入 Composer 并启用发送 |
| 31 | 发送 / ⌘+Enter | ✓ 空内容禁用；有内容可发；真实 run 流式输出（caret + run pulse 思考中/执行工具） |
| 32 | 停止按钮 | ✓ 流式中出现 `.composer-stop`、控制行禁用；点击后 run pulse → **已取消**（中性通道，非红色失败），按钮复位 |
| 33 | 思考块展开 | ✓ `aria-expanded` false→true，正文 258 字符渲染 |
| 34 | 工具卡展开/折叠 | ✓ `.act-node` 本身为按钮，detailed 档 `aria-expanded` 跟随；折叠按钮「折叠 ↔ 已折叠 · N 个工具 · M 轮 · Xs」 |
| 35 | 复制回答 | ✓ `aria-label` 复制回答→已复制，**系统剪贴板实测拿到回答正文** |
| 36 | Timeline 行 → StepDetail | ✓ 点 `model/completed` 行 → Inspector 切到事件焦点（`返回 Timeline`），返回按钮复位到 Run Inspector |
| 37 | 工具卡 Inspect | ✓ Inspector 切到该工具（focusType=bash，Input/Output/Raw 分页） |
| 38 | 委派节点 | ✓ 展开 `委派 → research_review` + 结果摘要区；复制子会话 ID（剪贴板实测）；Inspect 子会话；**打开子会话** → 主窗口切到 child |
| 39 | Ctrl+K 命令面板 | ✓ 唤起/输入过滤/Enter 执行/Esc 关闭；执行 Toggle Theme、Copy Run ID 等；**发现并修复 BUG-004** |
| 40 | 分叉按钮 | ✓ 真实点击 → 后端建 child、主窗口切到 child、child 为空会话（与首轮 tooltip 承诺一致）；分叉错误可见 |
| 41 | 恢复会话 | ✓ 真实 dangling 会话（停止 run 留下）→ 点击 → **「已恢复：回填 1 条工具结果」**，入口与 hint 消失而成功提示保留（原症状已消除） |
| 42 | 中断横幅 | ✓ 真实会话 `c63ce4d3-…`（run/interrupted 无 step_id）显示「上次运行在首个步骤开始前中断（原因：process_restart）」 |
| 43 | 浮标 ↓ 最新 | ✓ 见第 19 项（真实 wheel 上滚 → 浮标 → 点回底） |
| 未及 | 审批卡（#37） | ⚠ 本 UI 不可达（硬编码 `auto_approve: true`）且无测试——见 OBS-006 |
| 73 ★ | `复制 Trace ID` / `打开 Trace` | ✓ 两条**确实渲染且工作**（初版误判为「不可达」，已订正，见 OBS-010）。实测会话 `f181c5ce`：`复制 Trace ID` 经拦截 `clipboard.writeText` 拿到实参 `2482fcee…43b1`（与 seq 33 `run/failed` 的 `trace_id` 逐字一致）；`打开 Trace` 经拦截 `window.open` 拿到 `https://jp.cloud.langfuse.com/project/…/traces/2482fcee…`、`_blank`。⚠ 用例的可达条件：会话的 run 终态事件带 `trace_id`/`trace_url`（取自 `conversation.*`，**不依赖会话列表**） |
| 74 ★ | 中断态脉冲与横幅的一致性（OBS-007 修复后复验） | ✓ 会话 `c63ce4d3`（中断→**完成**）：脉冲「已完成」且**过期中断横幅消失**；会话 `f181c5ce`（中断→**失败**）：脉冲「失败」且**横幅消失**。两例 Timeline 仍保留 `运行中断` / `第 3 步中断` 的历史行（事实不删）；恢复入口不受影响（`canRecover` 由事件判定）。⚠「已中断」脉冲态当前真实语料不可达（无「中断且从未重跑」的会话），由单测锁定 |

**第 19 项的真机证据（2026-09-11，dev server + 真实后端）**：会话内提交 2500 行生成任务，流式中用真实 wheel 事件上滚并对 `scrollTop` 打点（临时埋点，验证后已移除）——

```
wheel 前: {following:true}
wheel:    {deltaY:-120, runActive:true}     → 同步脱离
随后 6×250ms 采样: dTop 全为 0，gap 7221→15502（内容在长），相邻 PIN 事件数 0（贴底已静默）
浮标:     streaming:true 期间恒为可见
点浮标后: gap→0、PIN 恢复（following:true）、浮标消失
流结束后: 5×350ms 采样均 pill:false / gap:0（settle 补底生效，无残留浮标）
```

**测试环境**：后端 `localhost:8000`（探测时 71 个真实会话）+ 前端 dev server `localhost:5173`，Chrome 经 CDP 驱动，点击 + 网络面板 + 埋点三重验证。

---

### 2026-09-11 第二轮逐按钮巡检 + 刷新一致性专项（真实浏览器 + 真实后端）

> 起因：用户要求「每个功能按钮都要点一遍」「检查刷新后会话内容还在不在」。本轮把工具栏/Composer/Inspector/命令面板/委派节点全部真机点过，并新增 BUG-005/006 专项。带 ★ 的是本轮新验证。

| # | 交互 | 结果 |
| --- | --- | --- |
| 44 ★ | 密度四档（复验） | ✓ 紧凑/均衡/Raw/详细 → `data-density` = compact/balanced/raw/detailed，`.density-btn.sel` 跟随 |
| 45 ★ | 主题切换（复验） | ✓ light→dark→light，`data-theme` 往返一致（无 §15 token 漏覆盖的可观察症状） |
| 46 ★ | Inspector 收起/展开（复验） | ✓ `.app-regions` ↔ `.app-regions.inspector-closed`，展开恢复原状 |
| 47 ★ | Workspace Chat/Split/Preview | ✓ 三者 `aria-pressed` 互斥（选中 true，另两个 false） |
| 48 ★ | 会话行「新建会话」 | ✓ 回到空态、Composer 就位 |
| 49 ★ | **刷新恢复（BUG-005）** | ✓ 刷新后零点击回到同一会话：选中行/轮次/正文指纹/脉冲逐项一致（详见 BUG-005 修复验证表） |
| 50 ★ | **流式中刷新（BUG-006）** | ✓ `GET /stream?after_seq=2 [200]` 接流，零交互下事件 3 → 54 条直到 `run/completed`；与后端真值 54 条 / 0 重复 / 0 空洞（详见 BUG-006 实机取证） |
| 51 ★ | 模型选择器（复验） | ✓ 5 项（默认链 + 4 模型），选中后 trigger 更新、浮层自动关闭 |
| 52 ★ | 权限模式选择器（复验） | ✓ 3 项（只读/工作区写入/完全访问），选中回填 trigger |
| 53 ★ | Agent Profile 选择器（复验） | ✓ 3 项（通用/编程/研究审查），选中回填 trigger |
| 54 ★ | Reasoning Effort 选择器（复验） | ✓ 3 项（轻量/标准/深度），选中回填 trigger |
| 55 ★ | 选择器浮层关闭语义 | ✓ 选完即关、浮层整体卸载（`totalMenusInDom: 0`）；**「多个浮层同时开着」是合成事件假象**——Radix 靠真实 `pointerdown` 判定外部点击，`.click()` 不产生它。用真实指针序列复测：全部正常 |
| 56 ★ | Inspector 五个 Tab | ✓ Timeline（事件行）/ Overview（run+session 摘要）/ Changes（「本次会话未产生文件变更。」）/ Terminal（真实命令回显 `$sleep 10 && echo MARKER-A1`）/ Artifacts（「本次会话未产生 Artifact。」）——空态文案诚实，无伪造 |
| 57 ★ | 命令面板 Ctrl+K（复验） | ✓ 50 项（11 静态命令 + 39 最近事件）；fuzzy 子序列过滤正确（`toggle`/`Compact`/`Copy`/`MARKER-A2` 均精准命中）；页脚快捷键提示在位。**新发现 BUG-007（label 全英文）** |
| 58 ★ | 工具卡展开/折叠 | ✓ `aria-expanded` true→false→true（首次测出「点不动」实为前一步折叠了整轮导致的连带现象，隔离后正常） |
| 59 ★ | 复制回答 | ✓ `aria-label` 复制回答 → 已复制 |
| 60 ★ | 工具卡 Inspect 芯片 | ✓ Inspector 切到事件焦点，出现「返回 Timeline」返回affordance |
| 61 ★ | 委派节点（真实会话 `7b84625c`） | ✓ 展开 true↔false；**复制子会话 ID** → 已复制；**Inspect 子会话** → Inspector 切到 `delegate target="research_review"`；**打开子会话** → 主窗口切到 child `2515a128`（2 轮内容），且 `ahi.selectedSession` 同步为 child |
| 62 ★ | 分叉按钮 | ✓ 真实点击 → 后端建 child `19c76d4b`（行数 69→70）、主窗口切到 child、child 为空会话 + 空态 hero；`POST /api/sessions/{parent}/forks [200]`；`ahi.selectedSession` 同步为 child |
| 63 ★ | 空态 preset chip | ✓ 3 个 chip；点击填入 Composer（「写一个 FizzBuzz 脚本并运行验证」）并启用发送 |
| 64 ★ | 发送禁用态 / 停止按钮 | ✓ 空内容禁用发送；有内容启用；run 中 `.composer-stop` 在场、发送消失、三个控制选择器禁用；点停止 → 脉冲 **「已取消」**（中性通道，非红色失败）、按钮复位、无横幅 |
| 65 ★ | API 身份令牌弹窗 | ✓ 打开：密码输入框（placeholder「粘贴 HS256 token（eyJ…）」）+ 说明文案（localStorage `ahi.apiToken`、claims 要求）；**Esc 真实按键可关**（合成 keydown 关不掉——同一类合成事件假象） |
| 66 ★ | 请求量核对 | ✓ 单次全新加载每端点恰好 2 次（React StrictMode dev 双调用，已知）；静置 3.5s 零增长——**无请求风暴** |
| 67 ★ | 中断横幅（复验，真实会话 `f181c5ce`） | ✓ 横幅「上次运行在**第 3 步**中断（原因：process_restart）」+ 文档内「第 3 步中断」标记；同一会话的工具卡另显示「工具 'bash' 执行超时（上限 10.0 秒）…可稍后重试」——**独立复现 OBS-009** |
| 68 ★ | 恢复会话按钮的**诚实自隐** | ✓ 两个含 `run/interrupted` 的真实会话（`f181c5ce` 末态 `run/failed`、`c63ce4d3` 末态 `run/completed`）都**不显示**恢复入口——因为它们的 run 都已有终态，确实**没有东西可修**。另：我本轮的「停止」实验（会话 `19c76d4b`）留下的是 `run/failed(reason=cancelled)` 干净终态，因此也不出现恢复入口。**这是正确行为不是缺按钮**：入口只在真有 dangling（tool/call 缺 result、或 run 缺终态）时才出现 |
| 69 ★ | 命令面板本地化（BUG-007 修复后复验） | ✓ 静态命令 label 全部中文（该会话 11 条俱在）；中文查询「主题/复制/密度/紧凑」命中，英文 `toggle theme`/`Copy Run`/`compact` 经 keywords 仍命中 |
| 70 ★ | Workspace Chat/Split/Preview（深挖，非仅 aria） | ✓ Chat **不渲染**占位行（零垂直占用，与源码注释一致）；Split/Preview 渲染 `.workspace-scaffold`，带「未来升级点」标签 + 各自说明文案（Split=另一视图 diff/预览，Preview=渲染 Artifact）——**是标注清楚的预留位，不是假面板**；三者切换间 Chat 主阅读面与 4 轮内容始终在位，切回 Chat 占位行消失 |
| 71 ★ | Timeline 行直接点击 → StepDetail | ✓ 点 `3 model/completed qwen3.8-flash · 2170 tok` 行 → Inspector 切到该事件（出现「返回 Timeline」）；点返回 → 复位到 Run Inspector（34 行 Timeline 全部可点） |
| 72 ★ | 命令面板**逐条执行**（不只搜到） | ✓ `切换 Run Inspector` → `app-regions` 加 `inspector-closed`；`切换主题` light→dark；`切换到紧凑` detailed→compact；`跳到最新事件` → Inspector 切到最后一条事件（`run/failed`）并出现「返回 Timeline」；`聚焦输入框` → 焦点落到 `textarea#composer-input`（与命令实现里的 id 逐字一致）。执行后已把主题/密度还原 |

**本轮未复验（依赖特定现场，沿用上轮结论）**：审批卡（OBS-006：UI 不可达）、浮标↓最新（上轮埋点验证）、恢复会话按钮的**正向**点击（上轮以真实 dangling 会话验证过成功反馈「已恢复：回填 1 条工具结果」；本轮语料里两个 interrupted 会话都无 dangling，故入口按设计不出现——见第 68 行）。

**合成事件假象清单（本轮新增，避免下次误报）**：Radix 系浮层/弹窗的外部点击关闭与 Esc 关闭依赖**真实** `pointerdown`/`keydown`；`element.click()` 与 `dispatchEvent(new KeyboardEvent(...))` 都不触发，会造成「浮层关不掉 / 多个同时开着 / Esc 无效」的假象。复测一律用真实指针序列或 CDP 按键。

---

## /code-review 第一轮（2026-09-11，BUG-005/006 未提交 diff）——6 findings 处置

审查范围：`web/src/lib/sessionRestore.ts`(+test)、`web/src/lib/api.ts`、`web/src/hooks/useSession.ts`。两轴（Standards + Spec/不变量）。**无 P0 / 无 P1**；3×P2 + 3×P3，逐条处置如下。

| # | 级别 | 结论 | 处置 |
| --- | --- | --- | --- |
| 1 | P2 | **成立**——resume-404 分支的 `writeStoredSessionId(null)` 是**死写**：紧接着 `setMode(viewing(sid))`，持久化 effect 会在同一批次把 sid 写回去，清了等于没清 | **已修**：删掉那行死写并写明理由。真正的「会话已被删」由紧随其后的历史重跑兜住（mode 变更 → `getSessionEvents` 404 → `NotFoundError` → 清键 + 回 idle，**那次落得住**） |
| 2 | P2 | **成立且重要**——`resumeAttemptedRef: Set<string>` 只按 sid 记账，会把**正当的再次恢复**也一并禁掉：A 会话接流成功 → 给 A 续聊 → 切到 B 再切回 A（期间 A 的新 run 在跑）→ 自动接流被拒 → 冻结在历史快照 | **已修**：改为按 **(sid, 游标)** 记账（`nextResumeAttempt` 纯函数）。死循环的断点仍在（零帧空流不带来新事件 → 游标不变 → 拦下），而「有新事件」时游标变大 → 允许再接。**追加加固**：`selectSession`（用户显式切会话）再调 `forgetResumeAttempt` 忘掉该 sid 的记账——消除「两次尝试之间只落了非持久帧（seq=null）→ 游标不变 → 手动切回来被误拦」这个边角；自动重入的死循环不经过 `selectSession`，断点不受影响 |
| 3 | P2 | **成立**——接流逻辑（本次改动的核心行为）无任何测试；`api.test.ts` 也没覆盖新增的 `NotFoundError` | **已修**：新增 `e2e/k-refresh-restore.spec.ts`（4 用例 × 2 视口 = 8 例）、`sessionRestore.test.ts` +5 例、`api.test.ts` +3 例 |
| 4 | P3 | 判断项——零帧回落是**全静默**的；若某天后端在 run 活着时返回 200+空 body（契约破坏），用户会永远盯着半截会话且无任何信号 | **接受的取舍**（不改）：零帧 + 干净 EOF 在**已实测的契约**下只能意味着「服务端已无在跑 run」（活着的 run 会把流开着、不会零帧 EOF）。加「延迟复查」会在回调里引入异步与新的重入面，收益不抵复杂度（§9.2）。风险已登记于此 |
| 5 | P3 | **成立（低影响）**——`scheduleReconnect` 的 404 分支也没清存储键 | **不改，补注释**：该分支 `live→viewing` 同样会重跑历史装载 → 404 → `NotFoundError` → 清键 + 回 idle，已在同一 tick 内自愈；单独加一次 `writeStoredSessionId(null)` 与 #1 同理是死写 |
| 6 | P3 | **成立**——diff 引入了一处多余空行 | **已修**：删除 |

**审查确认正确的部分**（作为覆盖凭据）：死循环断点充分（`nextResumeAttempt` 在首个 `await` 前记账；`mode.kind !== 'viewing'` 时历史 effect 早退，`setMode(live)` 不会二次装载）、StrictMode 双调用已被 `cancelled` 守卫挡住、游标数学不重不漏（`after_seq=max` 重放 `(max, cursor]`，`projection` 的 `seenSeqs` 再兜一层去重）、`framesSeen` 计数位置正确（被 mode/sid 拒的帧不计，去重帧计——「流还活着」的语义正确）、`terminalSeenRef` 每流重置、`streamGenRef` 代际守卫在 404/异常两路都复检、不变量 #22 未被破坏（复用同一 `projectHistory` 与唯一 `ConversationState`）、无 CSS/主题改动（§15 不涉及）。

**门禁（改后全量）**：`tsc -b` 0 · `vitest run` **484 passed**（476 → +8）· `oxlint` **35 warnings / 0 errors**（基线持平）· `playwright test --workers=2` **94 passed**（86 → +8）· `vite build` 0。

---

## /code-review 第二轮（对修复后的 diff 复审）——6 项一审 findings 全 RESOLVED，新发现 4 项（1×P2 + 3×P3）

### 一审 6 项的复核结论

| 一审 # | 复核 |
| --- | --- |
| 1（死写） | **RESOLVED**——404 分支只剩 `setMode(viewing)`；mode 对象是新的 → 持久化 effect 与历史 effect 双双重跑 → `/events` 404 → `NotFoundError` → 清键 + `idle`（`idle` 不会自恢复，故粘得住）。代码注释的说法成立（审阅者订正：严格说不是「同一批次」，是一次后续异步拉取，但同样粘得住） |
| 2（Set → 游标 Map） | **RESOLVED**——(a) 零帧分支直接 `setMode(viewing)` **不经过 `selectSession`**，记账因此留存，重装后再调被 `nextResumeAttempt` 拦下 → 死循环断点仍在（只有一条流请求，e2e 锁住）；(b) A→B→A 两条路都通（游标变大放行 / 显式切会话经 `forgetResumeAttempt` 放行）。ref 赋值在 effect 内，oxlint 无新增 refs 告警 |
| 3（补测试） | **PARTIALLY → RESOLVED**（见新发现 A：当时缺写入路径覆盖，已补） |
| 4（零帧静默取舍） | **RESOLVED / 站得住**——零帧 + 干净 EOF 在已实测契约下等于「无在跑 run」；「run 在缺口内收尾」这条竞态仍被覆盖（回到 viewing 会重跑历史、把终态重新渲染出来） |
| 5（重连 404 不清键） | **RESOLVED**——与 #1 同一自愈路径；若 `/events` 反而 200（会话确实还在），保留 id 才是对的。附注：首次历史重跑会多一次注定 404 的接流尝试，随即记上账稳定下来——有界，非循环 |
| 6（多余空行） | **RESOLVED**——五个改动文件无连续空行，`git diff --check` 干净 |

### 新发现 4 项及处置

| 新 # | 级别 | 内容 | 处置 |
| --- | --- | --- | --- |
| A | **P2** | **BUG-005 写入路径零自动化覆盖**——所有 e2e 都用 `addInitScript` 播种，而 `addInitScript` **每次导航（含 `page.reload()`）都会重跑**，所以它们只证明**读**路径；把持久化 effect 整段删掉，全套依然绿——恰好是本批要防的 BUG-005 回归 | **已修**：新增「空态 → 真实点击会话行 → 断言键被写下 → **不重新播种**刷新 → 仍恢复」用例（`k-refresh-restore.spec.ts`）。并做**变异验证**：临时把写入 effect 改为 no-op → 该用例在两个视口都变红（`Expected: "e2e-session-0001" / Received: null`），随后还原。这是本条 finding 的闭环证据。 |
| B | P3 | `useSession.ts:288` 注释称 ref 在 render 期赋值，与实现（effect 内）矛盾，会诱导后人「改回去」 | **已修**：改为「在 effect 里镜像」并加一句「曾据此改过一次，别改回去」 |
| C | P3 | 失败尝试在 `await` **之前**记账，会吃掉该游标的一次自动重试 | **不改 + 补注释写明理由**：这是刻意的。失败路径同样 `setMode(live)` → `setMode(viewing)`，mode 每变一次历史 effect 就重跑；若失败不记账，「失败 → 回 viewing → 重跑 → 再失败」就是**无上限重试**（每轮两条请求）。代价是失败后不自动重试（错误横幅已告知用户），用户重点一次该会话行即经 `selectSession → forgetResumeAttempt` 重新武装 |
| D | P3 | 交接文档定稿早于最后一次改动（测试数 9 vs 12、缺 `forgetResumeAttempt`） | **已修**：`FRONTEND_REFRESH_PERSIST_INTEGRATION_PROMPT.md` 已更新计数、模块导出表与「重新武装」机制 |

### 第二轮额外确认（「无 P0/P1」的依据）

`NotFoundError` 三个调用点：历史 effect 已处理；`doTruncatedRebuild` 走重连自愈；`useChildConversation` 仅读 `.message`（文案更友好，无回归）。`tsconfig` 目标 `es2023` → `class extends Error` 原型链完好，`toBeInstanceOf` 可靠。`maxEventSeq` 返回 `-1` 不会与 `hasUnterminatedRun` 同时成立（`run/started` 必带数字 seq），首帧不会被误判为 gap。`resumeAttemptedRef` 只按「本页会话内选中过的不同会话数」增长，刷新即清，非泄漏。§8 Scope Lock 与 §15 CSS 规则均未被触碰。`test-results/` 已被 `web/.gitignore` 忽略。

**第二轮后的门禁（实跑）**：tsc ✓ · vitest **487 passed**（28 文件）· oxlint **35 warnings / 0 errors** · playwright **96 passed** · vite build ✓。

---

## /code-review（BUG-007 修复，2026-09-11）——4 findings 全是 P3，无 P0/P1/P2

审阅者用**计算**而非目测验证了两条承重主张：①「label 命中的打分逐字不变」——穷举 3 字符以内全部 query × 11 条命令（3072 组），**0 个既有命中失分或改分**；②「英文仍可搜」——`toggle theme` / `compact` 在移除 keywords 后断言即失败，说明新测试真的守住了这个特性。e2e 的 BUG-004 回归锁（复制的是 **run** id 而非 session id）未被削弱。

| # | 级别 | 内容 | 处置 |
| --- | --- | --- | --- |
| 1 | P3 | 注释/文档把排序保证说过头了：「keywords 命中因下标靠后天然排在后面，不会抢位次」**跨命令不成立**——query `to` 下 `切换主题`（keyword `toggle theme`，37 分）会压过 `切换 Run Inspector`（label `Inspector`，21 分） | **已修**：订正为「同一命令内 label 优先；跨命令仍按分数比大小，keywords 命中可能排到前面，这是可接受的」。穷举验证结论写进注释（零既有命中改分） |
| 2 | P3 | 密度命令的 keywords 末尾重复了一次档位名（`switch to ${d} density 密度 ${d}`）；实测 1780 个 3 字符 query **只靠这层重复**命中（如 `aac`/`aca`），纯增噪而正当匹配一次都不受益 | **已修**：去掉尾部重复 token → `switch to ${d} density 密度`（`compact` 仍可由 `switch to compact` 命中） |
| 3 | P3 | 主题命令的**显示** hint 仍是英文（`→ Light` / `→ Dark`），与刚改中文的 label 同属「半本地化」 | **已修**：改 `→ 亮色` / `→ 暗色`（与已加的 keywords 同词）。判为 BUG-007 同一类而非范围外——它是面板里的显示文本；`Langfuse` 这类专有名词保留英文 |
| 4 | P3 | 问题登记簿自身的标题/清点不准：标题说「50 条命令 label 全是英文」（事件项 label 是后端事件类型，故意保留），正文说 11 条而清点只列 9 条 | **已修**：标题收窄为「11 条静态命令」；清点补齐 11 条并标注其中 2 条按契约条件出现（本部署实际渲染 9 条，见 OBS-010） |

**审阅者另附的过程提示**：审阅期间工作区有并发写入（我在同期更新 tracker/提示词文档，纯文档）。已在提交前重核 diff。

**门禁（修复后）**：tsc ✓ · vitest **490 passed** · oxlint **35w 0e** · playwright **96 passed** · vite build ✓。

---

## /code-review（OBS-007 修复，2026-09-11）——0 个可复现 bug（P0/P1/P2 全无），4 项 P3

审阅者独立复核了四条承重主张并**逐条给出反证或确认**：① 排序/优先级安全——不存在 `run_status === 'running'` 与 `run_interrupted` 同为真值的可达状态（`projectRunInterrupted` 必先 `finalizeRun`，而新 run 的 `run/started` 会清标记）；② 重放确定性——`projectRunStarted`/`projectRunInterrupted` 都只写纯函数状态，无时间/随机依赖；③ `run_interrupted` **没有**其他消费者（全仓 grep），改其语义不影响他处；④ `coarsenPulseState` 的穷尽性由编译器保证（实测删掉一个 case 触发 TS2366），非仅注释承诺。另确认 `CircleSlash` 是真实 lucide 导出、CSS 只复用双主题 token、3 条新测试在还原后确实变红、`git diff --check` 干净。

| # | 级别 | 内容 | 处置 |
| --- | --- | --- | --- |
| 1 | P3 | `coarsenPulseState` 的 `'已中断'` 映射**无任何测试断言**（`runState.ts`）：改成任一同类型字符串（如「已完成」）都不会有测试变红，Inspector Overview 会静默回归 | **已修**：在既有中断用例内补 `expect(deriveRunSummary(s).label).toBe('已中断')`——它与脉冲是**两条独立映射**，必须分开断言。已做变异验证（改成「已完成」→ 该用例变红） |
| 2 | P3 | 没有任何测试把 `pulse-interrupted` 这个**类名字符串**与 `app.css` 的**选择器**绑起来——类名字符串本身已被 `runState.test.ts` 锁住，但 CSS 选择器改名后无测试会发现（视觉表现当前恰好不变，因为 `.run-pulse` 基类已是同样的中性 token） | **不改 + 登记为已知覆盖缺口**：这是 JS/CSS 分界的固有限制，仓库无「测试读 CSS 文件」先例；为它引入构建期 CSS 断言属于 §9.2 之外的抽象。此处显式记录，避免被误当作已有覆盖 |
| 3 | P3 | 提前返回**同时覆盖** `failed`/`cancelled`（只要标记为真），而该分支在真实数据里不可达 → 优先级只靠「不变量成立」这一口头约定，无代码/测试约束 | **已修（显式化，非新分支）**：条件补成 `run_interrupted && run_status === 'completed'`。后端不变量保证两条件恒同时成立，故行为对全部合法日志**逐字节不变**（`deriveRunSummary` 的 8 个多 run 会话实测标签无变化）；破坏不变量时退化到「更晚的终态赢」，而非让过期标记把一次失败粉饰成中性色。新增一例锁住该退化语义 |
| 4 | P3 | (a) 后续发消息的 RTT 窗口内会短暂显示「已中断」（提前返回不看 `streaming`）；(b) `CircleSlash` 与邻位方形图标（`SquareCheckBig`/`SquareX`）风格不齐 | **不改（审阅者亦不建议改代码）**：(a) 该瞬时态比修复前的绿色「已完成」更接近真相，且窗口仅一个 RTT；(b) 纯审美，圆形已在图标族内（`CircleDashed` 空闲态）。换图标属无收益改动（§9.3） |

**审阅者结论**：本次修复**与规格一致**（冻结决策 69「中断 ≠ 失败」得到前端一致表达）、**无副作用**（`canRecover` 走 `isRecoverableRun(events)`，与本标记无关）、**范围克制**（§8 未被触碰）。

**门禁（修复后实跑）**：tsc ✓ · vitest **494 passed**（28 文件，+1 例）· oxlint **35 warnings / 0 errors** · playwright **96 passed** · vite build ✓。

---

## 真机验证（2026-09-11 追加）：子会话刷新一致性 + 新增回归锁

**起因**：交接提示词 §3.1 声称「刷新后恢复选中会话（**含分叉 child、委派 child、`打开子会话` 的目标**）」，但该断言当时**只有推断、没有证据**——`k-refresh-restore.spec.ts` 当时 5 个用例全用普通会话 id。用户的核心诉求正是「刷新后必须和刷新前一致」，故补做真机验证。

**方法**：真实浏览器（CDP 驱动）+ 真实后端，对每个子会话取「刷新前 / 刷新后」的正文指纹（bodyLen + 全文哈希 + 轮数 + 脉冲）逐字节对比，**零点击**。语料取自真实 workspace：委派 child `2515a128`（父 `7b84625c`）、分叉 child `1fdac9b9`（410 事件，父 `0c4fdcd0`）。

| 场景 | 入口 | 刷新前 | 刷新后 | 结论 |
| --- | --- | --- | --- | --- |
| 委派 child `2515a128` | 会话列表**点击该行** | hash `818904662` / 4710 字符 / 2 轮 / 脉冲「已完成 · 4,635 tok」 | **逐字节相同** | ✓ 一致 |
| 委派 child `2515a128` | 父会话委派节点的**「打开子会话」按钮** | 同上（切到 child 且键写为 `2515a128`） | **逐字节相同** | ✓ 一致（两条入口收敛到同一状态） |
| 分叉 child `1fdac9b9` | 会话列表点击该行 | hash `-815616722` / 12,887 字符 / 8 轮 / 脉冲「失败 · 28,357 tok」/ Inspector「显示最近 200 / 共 410 条」 | **逐字节相同** | ✓ 一致（大会话同样成立） |

**边界（有意为之，非缺陷）**：右栏的委派**钻取**（`Inspect 子会话` → `.detail-run-id` = child id）在刷新后会退回默认的 run 焦点（刷新后 `.detail-run-id` = 当前 run id）。这与既有冻结决策一致——Inspector 是**视图状态**（`inspectorOpen` 的注释即写明「仅本会话内，不持久化」，DSH 语义），与「会话内容」不是一回事：会话选中与正文全部恢复，只有「你上次在右栏盯着哪个子面板」不恢复。**记录于此，避免后人误判为刷新不一致的 bug。**

**新增回归锁**：`web/e2e/k-refresh-restore.spec.ts` 增 1 例（×2 视口）——「子会话 id 与普通会话同一持久化/恢复路径：非首行真实点击 → 写键 → 刷新恢复」。设计要点（每条都对应一次踩坑或审查意见）：
- 走**真实点击的写入路径**，不用 `addInitScript` 播种（播种每次导航都重跑，只能证明读路径——本轮首版就踩了这个坑：**变异后依然全绿**，见下）；
- 点**非首行**，且 id 是首行 id 的**严格前缀**（`SID` vs `SID-child`）→ 刷新断言真的能区分「恢复了错会话」；
- 事件按请求的 session id 返回**不同正文**（父「父会话正文。」/ child「子会话结论。」），并断言父正文**不出现**——否则 `toContainText` 在恢复错会话时也会通过，等于没锁内容一致性。

**变异验证（两次都要，第一次暴露了测试缺陷）**：
1. 首版（播种式）在「写入按 `-child` 过滤」的变异下**依然全绿** → 证明它只覆盖读路径，遂改真实点击写入路径；
2. 改后同一变异 → 两个视口都红：`Expected: "e2e-session-0001-child" / Received: null`，随后还原。

**本轮审查（对新 e2e 用例）**：**0 个 P0/P1/P2**，5 项 P3——全部指向「注释/标题的说法超出用例实际断言」（合成 fixture 无真委派/分叉事件却写得像自动化复现了真机结果；`toContainText` 无鉴别力；`toBeNull()` 前置断言在全新上下文里恒真）。**5 项已全部处置**：标题与注释改为准确表述并明确「真机验证记在登记簿」、事件按 id 区分、移除恒真断言的误导性说法、行定位改用 `title` 属性前缀（稳定身份，不依赖业务文案）。

**门禁**：tsc ✓ · vitest **494 passed** · oxlint **35w 0e** · playwright **98 passed**（+2）· vite build ✓。

---

## 第三轮：控制面清点（源码 45 个 `<button>` 逐个对照登记簿）

**起因**：前两轮的巡检表是**人工列举**的，无法证明「每个按钮都点过」——这正是用户的硬要求（「每个功能按钮你必须都要点击一下…每个都要点一遍」）。故本轮改用**可核对的方法**：从源码枚举全部交互控件，再逐项对照登记簿。

**方法**：扫 `web/src/**/*.tsx`（排除测试）取全部 `<button>` 标签 → **45 个**，分布在 17 个文件（StepDetail 10、TopBar 6、ToolCard 4、Conversation 4、DelegationNode 3、App 3、SessionList 2、ReasoningBlock 2、Composer 2、ApprovalCard 2、markdown/ModelPicker/JsonTree/CopyButton/ControlPicker/ContextProviderPicker/CommandPalette 各 1）。另有 `onClick` 非 `<button>` 元素 **0 个**（即按钮即全部点击面）。再逐个在登记簿里找覆盖证据。

**结果：11 项此前从未被点过**（前两轮的 74 行表确实漏了）。本轮逐项真机验证后，9 项正常、1 项不可达（已补 e2e）、1 项实际不可达（登记为覆盖缺口）。

### 本轮真机验证通过（9 项 + 附带确认）

| 控件 | 真机证据 |
| --- | --- |
| **Inspector 5 个 tab**（Timeline/Overview/Changes/Terminal/Artifacts） | 各渲染不同内容：Overview = RUN/TOOLS/TRACE/MODEL/CHECKPOINT（状态失败、15 轮、28,357 tok、7 工具 2 失败、410 事件、含 `glm-5.3-flash → glm-4.5-air` 切换史）；Changes = 文件写入前后 diff；Terminal = 5 行 shell 输出；Artifacts = 诚实的「本次会话未产生 Artifact。」；Timeline = 200 行 |
| **加载更早 210 条**（`.timeline-earlier`） | 点击后时间线 **200 → 410 行**（等于该会话真实事件总数），按钮消失，首行变为 `0 session/started`——**不重不漏** |
| **时间线行点击**（`.timeline-row` ×200） | 点 seq 406 `model/completed`：主区滚动 `0 → 5205`，出现 1 个跳转脉冲——反向联动（Inspector → 主区定位）成立 |
| **终端行**（`.detail-terminal-row` ×5） | 点击后 Inspector 头部切为 `bash`、出现返回键、io-tabs 出现 |
| **io-tabs ×4**（Overview/Input/Output/Raw） | 四档渲染各不相同：正文长度 136 / 122 / 276 / 921 字符；JSON 行 0 / 3 / 6 / 29 |
| **JSON 树展开**（`.json-row[aria-expanded]`，5 个可展开） | `args: {1 key}`：`aria-expanded` false → true，行数 29 → 31 |
| **返回父会话 Run 视图**（`.child-back-btn`） | 钻取态点它：从 child `2515a128` 退回父 run `2c2ad2e6`，返回键消失 |
| **代码块换行**（`.md-code-wrap-btn`） | label `自动换行 → 不换行`，class 加 `md-code-wrap`，`aria-label` 同步为「代码不换行」 |
| **停止（工具运行中点）** | 脉冲 → 中性「已取消」，无重连横幅；且**全语料 0 个 dangling tool_call**——取消会把工具收口，**不会**留下虚假的「恢复」入口（配合 `isRecoverableRun` 设计正确） |
| （附带）工具终态输出未丢失 | 卡片展开渲染 `.act-detail-result`（含 `stdout: "LINE-1 \nLINE-2 \nLINE-3 \n"`），Inspector 亦有——流式尾窗关闭**不是**输出丢失 |

### 覆盖缺口 1：`auth-banner-close`——本轮已补 e2e

**不可达原因**：后端仅在配置 `jwt_secret` 时校验令牌并要求 401（`src/agent_harness/web/app.py:594`，未配置则 fail-open），本地开发不设该密钥 → 横幅永不出现。重启后端加密钥属改动后端状态、且超出前端范围，故按**后端已冻结的契约形状**（`lib/auth.ts` 文档：匿名 → 401 `{"detail":"Missing identity token"}`）补 e2e。

**新增** `web/e2e/l-auth-banner.spec.ts`（×2 视口）：401 → 横幅出现（含文案断言）→ 点「关闭提示」→ 横幅消失 → 且**不再自行复现**（把 401 翻成 200 后再断言，否则「关闭后复现」会变成假阴性）。**变异验证**：把 `onClick={() => setAuthRequired(false)}` 改为空实现 → 两个视口都红（`Expected: hidden / Received: visible`，14 次轮询都可见），随后还原。

### 覆盖缺口 2：`tool-out-wrap-btn` / `tool-out-jump`——登记为**实际不可达**（非缺陷）

**机制**（源码 + 事件双侧取证）：输出尾窗的渲染条件是 `tool.output.length > 0 && (status === 'running' || !tool.result)`（`ToolCard.tsx:136`）。而本后端 cmd.exe **缓冲输出**，整段输出以**单个**终态 `tool/output_delta` 到达，`tool/result` 紧随其后——窗口只存在毫秒级。实证：会话 `01fa7167` 的 `echo LINE-1 & ping…` 三个 echo 在 6.2s 内跑完，`tool/output_delta` **仅 1 条**，其 `stdout` 一次性为 `"LINE-1 \nLINE-2 \nLINE-3 \n"`。叠加 bash 工具 **10.0s 硬超时**（见 OBS-014）与模型不确定性（本轮 6 次尝试中出现 1 次模型不改写命令、1 次完全拒绝调用工具、1 次 3.5 分钟退化循环），该窗口是移动靶。

**本轮实际观察到**：窗口确实渲染过（3 次），默认态为 `tool-out-body tool-out-wrap` + 按钮文案「不换行」，内容为真实输出（`LINE-1 LINE-2 LINE-3`）。**未观察到**：换行点击的切换效果、`↓ 最新` 的出现与点击（它还需要「流式中用户上滚」这一叠加条件）。

**测试侧现状（勿误认为已覆盖）**：`ToolCard.test.tsx` 只断言窗口的**存在条件**（`tool-out-stream` 的有/无），**没有**换行或跳转的点击用例；`j-scroll.spec.ts:53` 只断言非流式态**不出现** `↓ 最新`。两处点击均无覆盖，也未能在真机点击——如实登记，不当作已完成。

### 本轮新发现的**后端**问题（含根因文件行号，供后端修复）

#### OBS-011 子进程输出按 UTF-8 解码，而 cmd.exe 输出 GBK → **乱码被持久化进 JSONL**（P2）

**现象**：工具输出在 UI 与**落盘事件**中都成乱码。两次实证：`��ʱ��Ӧ�� i��`（cmd 的「此时不应有 i。」）、`���� Ping 127.0.0.1 …`。

**铁证（区分前后端的关键）**：读 `25fe14b0…/events.jsonl` 的**原始字节**，delta 为
`\xef\xbf\xbd\xef\xbf\xbd\xca\xb1\xef\xbf\xbd…` ——
即 **U+FFFD（`\xef\xbf\xbd`）与"侥幸合法的 UTF-8 双字节"混杂**：`\xca\xb1` 被解成 U+02B1（ʱ）、`\xd3\xa6` 被解成 U+04E6（Ӧ）。GBK 的「时」「应」两字节恰好构成合法 UTF-8 序列 → 剩下非法处变 U+FFFD。文件本身是**合法 UTF-8**、含 **24 个 U+FFFD**。

**归因：后端，且写入发生在事件落盘之前**——前端只是忠实渲染磁盘上的内容。

**根因**：`src/agent_harness/sandbox/local.py:166-167` 的 `encoding="utf-8", errors="replace"`（源码注释已自承认「Windows 中文系统默认 GBK…用 errors=replace 保证不崩」——代价是把乱码固化进了 append-only 事件日志）。同一模式另见 `sandbox/docker.py:140-141`。

**为什么值得修**：JSONL 是本项目可观测性的**单一事实源**（回放 / eval / Langfuse 都读它）。乱码一旦落盘即不可逆，且会让「模型看到的工具输出」与「真相」不一致。

#### OBS-012 `bash` 工具在 Windows 上不是 bash（P2）

**现象**：`for i in $(seq 1 10); do …; done` 41ms 内以 `exit_code=1` 失败，stderr 为 cmd.exe 的「此时不应有 i。」。

**根因**：`sandbox/local.py:161` `shell=True` → Windows 下走 `COMSPEC`（cmd.exe）。

**旁证（同会话 `1fdac9b9`）**：`echo "Current user: $(whoami)"` 的输出里 `$(whoami)` 被**原样回显**（`"Current user: $(whoami)"`）——cmd.exe 不做命令替换；而 `ls -la` 却能工作（PATH 里有 Unix 工具）。模型据此在**同一任务里反复试错**（该会话 4 次 `model/fallback`、7 次工具调用、2 次工具失败）。

**影响**：工具名与语义不符会让模型（与人类）按 bash 语法写命令并莫名失败，直接拉高 token 与失败率。

#### OBS-013 模型退化重复循环 + 频繁 fallback（P2 · provider）

**现象**：会话 `7d5a6f24` 一轮生成 **2,868 个 `text/delta`、共 186,507 字符**，内容为 `"Let me run the command."` 的无限重复，**始终没有发出工具调用**，持续 3.5 分钟后由我点「停止」收口（`model/failed: model call cancelled`）。全语料另有多次 `model/fallback: deepseek-v4-flash-0731 → glm-4.5-air · InternalServerError`。

**归因：provider/后端**。前端表现正确（脉冲「思考中」，文本持续流入，停止可用）。但值得后端评估：该 provider 是否存在重复惩罚/最大生成长度护栏，以及「长时间无工具调用的大段自重复」能否作为可观测信号提前暴露。

#### OBS-014 bash 工具 10.0s 硬超时且 `retryable:false`（与 OBS-009 同族，此处补实证）

`sleep 30 && echo resume-test-done`、`ping -n 45 127.0.0.1` 均以 `TIMEOUT`（`retryable:false`）失败；`ping -n 4` / `sleep 9` 正常。即单条命令**上限 10.0s**，长任务必须由模型自行切分。结合 OBS-013 会出现「想跑长命令 → 超时 → 反复重试/退化」的组合失效。

**本轮门禁**：tsc ✓ · vitest **494 passed** · oxlint **35 warnings / 0 errors** · playwright **100 passed**（+2）· vite build ✓。

### 本轮审查（对新增的 `l-auth-banner.spec.ts`）：0 个 P0/P1，1 项 **P2** + 4 项 P3——全部已处置

审阅者做了三件我没有做的核验：① 用**实测的网络捕获**确认「横幅出现后到关闭前没有任何 `/api/sessions` 请求」；② 用 `playwright-core@1.63.0` 源码确认「后注册路由优先」；③ **逐字检查了我注释里的覆盖声明**。

| # | 级别 | 内容 | 处置 |
| --- | --- | --- | --- |
| 1 | **P2** | **注释谎报覆盖**：我写「`lib/api.test.ts` 测 401 的分类（`UnauthorizedError`）」，实际该文件**零个 401 引用**——全 `src` 测试树里都搜不到 401/Unauthorized。即 **401→`UnauthorizedError` 这条缝当时根本没有单测** | **已修（把谎报变成事实，而非删掉句子）**：在 `api.test.ts` 新增一组 3 例——401 → 抛 `UnauthorizedError`（且**不是** `NotFoundError`）、401 → `onUnauthorized` 收到后端 detail、**body 非 JSON 时回退文案**（覆盖 `readErrorDetail` 的失败路径）。注释改为准确的两层分工 |
| 2 | P3 | `denied` 开关与 `waitForTimeout(400)` 是**惰性构件**：实测「横幅出现后再无 `/api/sessions` 请求」，故 200 分支永不执行、「关闭后不复现」是**空断言**；且它把真实行为**说反了**——关闭并非永久忽略（`App.tsx:148` 每次广播都会 `setAuthRequired(true)`） | **已修并改成测真实行为**：删掉开关与空等；改为走应用内真实路径「配置令牌」→ `onTokenChange` 先清横幅再 `refreshSessions()` → 后端仍 401 → **断言横幅重新出现**。**变异验证**：删掉那句 `refreshSessions()` → 两视口都红（`Expected: visible`），随后还原 |
| 3 | P3 | 契约形状的 body 只作输入、从未断言被传播；`toContainText('身份令牌')` 命中的是**静态文案**，`readErrorDetail` 坏了也照样通过 | **已修**：detail 的传播与回退文案由上面新增的 3 个单测覆盖；e2e 注释写明「文案是静态的，传播由单测覆盖」，不再暗示 e2e 覆盖了它 |
| 4 | P3 | `toBeHidden()` 在**组件树崩溃卸载**时也会通过（`main.tsx` 无 error boundary），故「点了没反应」与「点崩了」区分不开 | **已修**：关闭后补断言顶栏「API 身份令牌设置」按钮仍可见（页面还在，真的只是横幅关了） |
| 5 | P3 | `getByRole('button', { name: '关闭提示' })` 的 `name` 是归一化子串匹配，当前唯一但不够表态 | **已修**：加 `exact: true` |

**审阅者结论**：**approve with a P2 comment fix**——该用例确实锁住了此前零覆盖的关闭按钮（空 `onClick` 变异会让它红），路由优先级与其余 gate 均确认无误；要求订正注释里的覆盖声明，并把惰性的粘性构件简化或改成测真实行为（两者均已照做）。它另确认：未改动任何生产代码、无其它 spec 与之重复、`oxlint` 仍是 **35 warnings / 0 errors**。

**审查后的最终门禁**：tsc ✓ · vitest **497 passed**（+3）· oxlint **35 warnings / 0 errors** · playwright **100 passed** · vite build ✓。

### 修正：文本比对审计本身不可靠（本轮自查，务必以真机点击为准）

上文的「11 项未覆盖」是**文本关键词比对**的产物，复查发现该方法**两个方向都会错**：

- **假阳性（判成已覆盖，实际没点过）**：`保存` 命中的是第 160 行「没有第四处**保存**会话选择」、`清除` 命中的是第 167 行「回到空态时**清除**」——两处都是无关散文，于是**令牌弹窗的「保存」「清除」两个按钮实际从未被点过**，却被判为 OK。同理 `Inspect` 一词的泛命中掩盖了 `act-inspect-chip`。
- **假阴性（判成未覆盖，实际有 e2e 或真机证据）**：`滚动到最新`（第 19 行明确写着 e2e `j-scroll.spec.ts` 用真实 `page.mouse.wheel` 锁「上滚→浮标出现→点浮标回底」）、`恢复会话`（第 41 行：真实 dangling 会话点击 →「已恢复：回填 1 条工具结果」）其实都有覆盖。

**结论：按钮级覆盖率不能用关键词比对裁决。** 故改为**逐个真机点击**，见下表。

### 本轮真机点击的完整清单（新增覆盖，均已确认「点了有反应且符合预期」）

| 控件 | 真机观察到的反应 |
| --- | --- |
| Inspector tab ×5 | Overview/Changes/Terminal/Artifacts/Timeline 各渲染**不同**内容（见上文详表） |
| 加载更早 210 条 | 200 → **410** 行，按钮消失，首行 `0 session/started` |
| 时间线行 ×200 | 主区滚动 `0 → 5205` + 出现 1 个跳转脉冲 |
| 终端行 ×5 | Inspector 头切 `bash`、出现返回键、io-tabs 出现 |
| io-tab ×4 | Overview/Input/Output/Raw 正文长度 136/122/276/921，JSON 行 0/3/6/29 |
| JSON 树行 | `aria-expanded` false → true，行数 29 → 31 |
| 返回父会话 Run 视图 | 从 child `2515a128` 退回父 run `2c2ad2e6` |
| 代码块换行 | 文案 `自动换行 ↔ 不换行`，class 加/去 `md-code-wrap`，`aria-label` 同步 |
| 空态示例 chip ×3 | 点「创建 todo.md，写入三条今日计划」→ Composer 文本被填入该任务 |
| **推理块展开**（`.reasoning-header` ×5） | `aria-expanded` false → true（**此前无任何覆盖**） |
| **Inspect chip**（`.act-inspect-chip` ×3） | 点击 → Inspector 聚焦 `bash` 工具 + 出现返回键（**此前无覆盖**） |
| **Inspector 工具行**（`.detail-tool-row` ×7） | 点 `bash` 行 → Inspector 聚焦该工具（**此前无覆盖**） |
| **令牌保存**（`.auth-panel-save`） | 填 dummy token → 点保存 → `localStorage ahi.apiToken` = 该串（**此前无覆盖**） |
| **令牌清除**（`.auth-panel-clear`） | 点清除 → `ahi.apiToken` 变 `null`（**已还原，无残留**）（**此前无覆盖**） |
| Inspector 展开/收起 | `aria-label` `收起 Inspector ↔ 展开 Inspector` 互换（配合第 8/23 行：`.app-regions.inspector-closed` 归零宽度） |
| 停止（工具运行中） | 脉冲 → 中性「已取消」；**全语料 0 dangling tool_call**——不留虚假恢复入口 |

### 全部 45 个 `<button>` 的最终状态

**逐个文件核对 45 个标签**（StepDetail 10、TopBar 6、ToolCard 4、Conversation 4、DelegationNode 3、App 3、SessionList 2、ReasoningBlock 2、Composer 2、ApprovalCard 2、其余 7 个文件各 1）：

| 状态 | 数量 | 项 |
| --- | --- | --- |
| 真机点击验证通过 | **38** | **前两轮已验**：`detail-back-btn`（返回 Timeline）、`density-btn`（密度四档）、身份令牌图标、主题切换、`act-node`（ToolCard 与 DelegationNode 两处展开）、`fork-btn`、`turn-collapse-btn`、`workspace-mode`、`recover-btn`、`follow-pill`、新建会话、会话行、发送、停止、模型选择、CopyButton、ControlPicker（权限模式）、`palette-item`。**本轮新验**：Inspector `detail-tab` ×2 标签（5 个实例）、`timeline-earlier`、`timeline-row`、`detail-terminal-row`、`io-tab` ×2 标签（4 个实例）、`json-row`、`child-back-btn`、`md-code-wrap-btn`、`example-chip`、`reasoning-header`、`act-inspect-chip`、`detail-tool-row`、`auth-panel-save`、`auth-panel-clear`、Inspector 展开/收起 |
| 不可达 · 已补 e2e | 1 | `auth-banner-close`（本地未配 `jwt_secret`；新增 `l-auth-banner.spec.ts` + 变异验证） |
| 不可达 · 按设计 | 3 | 审批卡「批准」「拒绝」（`auto_approve` 硬编码，OBS-006）；`ContextProviderPicker`（本部署后端目录为空 → 正确地不渲染，第 29 行；组件本身由 `picker-search-visibility.spec.ts` 用长目录 fixture 覆盖） |
| 不可达 · 瞬态窗口 | 3 | `tool-out-wrap-btn`、`tool-out-jump`（见缺口 2）、`reasoning-jump`（同一族：需「流式中 + 用户上滚」才渲染，`ReasoningBlock.tsx:255` 的 `suspended &&`；其底层 `followLatest` 原语已由第 19 行的 `j-scroll.spec.ts` 用真实滚轮锁住） |

**合计 45 = 38 + 1 + 3 + 3** ✓ —— 每个 `<button>` 都有明确去向：**38 个真机点过**，其余 7 个各有**书面理由**（e2e / 产品设计 / 瞬态窗口），不再有「不知道点没点过」的项。

**附带证据：控制台干净**。走完上述全部点击后，页面控制台（含最近 3 次导航的保留消息）只有 **2 条 error**，且都是**本轮变异验证自身的残留**——临时改坏 `App.tsx` 再还原时，Vite HMR 报了一次 500 与一次「Failed to reload /src/App.tsx」。**没有**任何一条来自被点控件（Inspector tab / io-tab / JSON 展开 / 令牌保存 / 推理展开 / 委派钻取等）的应用级错误。
