# 集成提示词：OBS-008 … OBS-015 批次（feat/backend）

> 来源：集成 AI 的 `docs/HANDOFF_BACKEND_OBS_008_015.md`（该文件只在 `main`，本 worktree 未同步）。
> 本文件是**批次单一入口**：每完成一项就更新下表 + 对应小节，集成 AI 只看这一个文件即可。
> 约束：**本分支不 push**（§16.4）。集成由集成 AI 执行。

## 0. 可执行摘要（给集成 AI）

```bash
# 1) 合并前自检（本分支）
git -C D:\intelligence-agent-backend log --oneline -8
git -C D:\intelligence-agent-backend diff main...feat/backend --stat

# 2) 合并后门禁（集成 worktree：D:\intelligence-agent）
uv run ruff check src/ tests/
uv run pytest -q

# 3) 只针对本批次改动的聚焦回归
uv run pytest tests/sandbox/test_output_encoding.py -q      # OBS-011（22 个）
uv run pytest tests/agent/test_phase5_runtime.py -q         # OBS-011 的连带回归
```

合并顺序与冲突面：本批次**只动后端 sandbox 解码路径 + 测试**，不碰 API 形状、不碰前端。
唯一需要留意的集成点是 `LocalSubprocessSandbox` 的构造签名新增了可选参数
`fallback_encoding`（默认 `None` → 自动探测），既有调用方无需改动。

## 1. 状态总表

| 项 | 优先级 | 状态 | commit | 一句话 |
| --- | --- | --- | --- | --- |
| OBS-011 | P2 | ✅ 完成 | `d4eb17e` | 子进程输出按产出方编码解码，GBK 乱码不再固化进 JSONL |
| OBS-015 | P2 | ✅ 完成 | `cb0e008`+`4580a69`+`274afcf`（**feat/frontend**） | 审批卡 catch 不再乐观翻转；404 语义订正 + fail-safe 回归锁 |
| OBS-012 | P2 | ✅ 完成 | `d9bef3a` | bash 工具描述声明真实解释器（POSIX/容器=sh，Windows=cmd.exe），不再谎称 bash |
| OBS-016 | P2 | 🆕 待定 | — | **本轮新发现（同源缺陷，Scope 外）**：`tools/read.py:140` 超长行提示仍教模型用 POSIX 专有 `sed`/`head -c`/`tail -c` |
| OBS-013 | P2 | ⏳ 待做 | — | provider 退化重复：护栏或登记已知风险 |
| OBS-009/014 | — | ⏳ 待做 | — | TIMEOUT `retryable` 语义 + 文案矛盾 |
| OBS-008 | — | ⏳ 待做 | — | model/failed 吞 traceback |
| OBS-010 | low | ⏳ 待做 | — | `GET /api/sessions` 的 `trace_id` 恒 null |

---

## 2. OBS-011：GBK 输出被按 UTF-8 硬解 → 乱码固化进 append-only JSONL

**状态**：✅ 完成，commit `d4eb17e`（分支 `feat/backend`，未 push）。

### 根因

`LocalSubprocessSandbox.exec()` 把子进程输出**硬编码**按 `utf-8 + errors="replace"` 解码。
中文 Windows 上 cmd.exe 的报错/内建命令输出是宿主控制台代码页（本机 **cp936/GBK**），
于是被解成 U+FFFD 乱码。乱码一旦写进 append-only JSONL 就**不可逆**：回放 / eval /
Langfuse 都读它，且模型看到的工具输出与真相不一致。

### 修法

新增 `src/agent_harness/sandbox/decoding.py::StreamDecoder`，`Popen` 改 `text=False` 走字节流：

1. 先按 UTF-8 **严格**增量试探（跨 chunk 缓冲不完整序列），上限 `PROBE_LIMIT = 64 KiB`；
2. 遇到确凿非法序列 → 判定整条流为兜底编码，并**重解已缓冲的全部字节**（不丢前缀）；
   判定粘性，同一条流不反复横跳；
3. 攒满 64 KiB 合法 UTF-8 → 判定 UTF-8；
4. 兜底编码取 `ctypes.windll.kernel32.GetOEMCP()`（本机 936）。
   ⚠ **不能用 `locale.getpreferredencoding()`**：本环境 `PYTHONUTF8=1` 会让它返回
   `utf-8`，正好丢掉要的信息；
5. 保留 `text=True` 时代的通用换行归一（`\r\n` / 孤立 `\r` → `\n`，含跨 chunk 切分、
   文件末尾孤立 `\r`）——改字节流后不会自动发生，丢掉会让每条 Windows 工具输出多一个
   `\r` 进入模型上下文与 JSONL，**并改变内容哈希**（曾使 `test_phase5_runtime` 挂在
   `KeyError: Artifact ... does not exist`）。
6. `docker.py` **刻意**保持固定 UTF-8 并注明理由（容器输出源自 Linux 进程，与宿主机
   控制台代码页无关）。→ 这是有意的**不对称**，不是漏改。

### 关键不变量（改动时务必守住）

> **判定后绝不能再抛。** `_drain_stream` 用宽 `except Exception` 兜异常，
> 解码器抛错会被吞掉并**无声明地截断整条流的剩余部分**。

据此：判定后的解码器一律 `errors="replace"`；`_commit_utf8` **不接管**严格探测解码器
（它持有严格语义，之后遇杂散非法字节会抛），而是按已消费字节数把余量交给宽松解码器。

### 文件

| 文件 | 性质 |
| --- | --- |
| `src/agent_harness/sandbox/decoding.py` | 新增（`platform_fallback_encoding` + `StreamDecoder`） |
| `src/agent_harness/sandbox/local.py` | 改：`text=False`、每流一个解码器、`_drain_stream` 解码后入 cap、记录判定日志 |
| `src/agent_harness/sandbox/docker.py` | 仅注释（说明为何保持 UTF-8） |
| `tests/sandbox/test_output_encoding.py` | 新增 22 测试 |

### 验收对照（HANDOFF 的 5 条）

| 验收 | 落点 |
| --- | --- |
| 1. 先复现（GBK 字节走 sandbox 输出路径，落盘 JSONL 无 U+FFFD，当前必红） | `TestDurablePathHasNoMojibake`（红→绿：首跑 `ModuleNotFoundError`，修后 0 个 U+FFFD） |
| 2. 修后绿 | 全文件 22 passed；全量 1528 passed / 9 skipped |
| 3. 跨平台 | 兜底编码可注入（测试缝 `fallback_encoding="cp936"`），非 Windows 用 utf-8 |
| 4. 边界 | 多字节跨 chunk / 提交点切在汉字中间 / 判定后坏字节 / 末尾不完整序列 / 纯二进制 / 非法编码名 |
| 5. `docker.py` 同模式一并核 | 已核：**有意保持** UTF-8，注释说明理由 |

### 测试清单（22）

真实子进程路径（非 mock）：GBK 可读且无 U+FFFD、UTF-8 与原行为一致、纯二进制不崩、
**晚到坏字节不截断流**（70000×A + `\xff` + 尾标）、耐久面 JSONL 0 个 U+FFFD。
单元面：UTF-8 优先 / 非法序列回退 / 粘性 / 多字节跨 chunk / 提交点切在汉字中间 /
判定后坏字节保留剩余 / flush 收尾不丢尾字节 / 通用换行归一 5 例 / 非法编码名构造即报错 /
`_DRAIN_CHUNK_BYTES <= PROBE_LIMIT` 耦合锁。

### 证据强度（为什么可以信）

- **独立 code-review 复核**：P0/P1/P2 **零 finding**；4 项 P3 已全部闭环。
  复核用差分模糊测试（4000 组合法流 + 4000 组非法流与整串归一对拍，0 不一致）、
  穷举多字节跨界（`é`/`中`/`€`/`😀` 全偏移）、真实子进程跑通。
- **两次变异测试**，证明新回归锁非空洞：
  - 回退 P1 修复（判定后交回严格探测解码器）→ **3 个测试变红**；
  - 把 OEM 代码页退化成 `utf-8` → **1 个测试变红**（旧断言是空洞的，
    `codecs.lookup` 抛错而非返回 `None`，任何合法名字都能过）。

### 风险 / 未决

- **无已知数据迁移**：本次只保证**新增**输出不再乱码。**历史 JSONL 里已经固化的乱码**
  不在本次范围——按 HANDOFF 约定，若要修复必须是**独立的一次性迁移工具**
  （默认 dry-run + 备份），且需用户明确批准，**不得就地重写 JSONL**。
- `StreamDecoder.feed()` 的判定顺序依赖 `_DRAIN_CHUNK_BYTES <= PROBE_LIMIT`（已加注释 +
  测试锁）。若将来调大读取块，需同步调整，否则「已满上限的合法 UTF-8 + 同段坏字节」的
  判定会取决于坏字节落在哪一段。
- 本次**未**给 `ExecResult` 加「本次解码用的是什么编码」字段（只落了 debug 日志），
  以免扩大 API 形状。若集成 AI 希望 Web UI 能显示「本次输出解码可信度」，那是另一张票。

### 集成后建议核对

```bash
# 中文 Windows 上跑一次真实 cmd 报错，确认事件里没有 U+FFFD
uv run pytest tests/sandbox/test_output_encoding.py -q
# 内容哈希敏感的回归（新换行归一必须保持原样）
uv run pytest tests/agent/test_phase5_runtime.py -q
```

---

## 3. OBS-015：审批卡的 `catch` 乐观翻转（**feat/frontend**，非本分支）

**状态**：✅ 完成。**改动在 `feat/frontend`**，不在 `feat/backend`：
`cb0e008`（实现）+ `4580a69`（code-review 收尾：`.approval-error` CSS / tracker / 注释）
+ `274afcf`（本次收尾：404 语义订正 + 404 fail-safe 回归锁）。

### ⚠️ 重要：交接单里 OBS-015 的前提是**错的**，请勿按原文"修回"

交接单写的是「409/**404** 幂等已决 vs 其它错误保持 pending」。**404 不是幂等已决。**
后端 approve 端点的 404 有四个来源，全部**无法**与「已解析且已出队」区分
（`src/agent_harness/web/app.py:1157-1166`）：

| 后端异常 | HTTP | 含义 |
| --- | --- | --- |
| `ApprovalAlreadyResolved` | **409** | **幂等已决**（决策已生效）→ 唯一可翻卡片的错误码 |
| `SessionNotFound` | 404 | 会话不存在 |
| `ApprovalQueueMissing` | 404 | 审批队列缺失（run 已结束等） |
| `ApprovalRequestMissing` | 404 | `approval_id` 不在队列（可能是过期事件，也可能是已出队——**不可区分**） |
| `InvalidDecision` | 422 | 决策不在 `allowed_decisions` 内 |

把 404 当成功 = 决策其实**没有**生效却显示「已批准」——正是 OBS-015 本身要消灭的安全假象。
所以正确语义是：**只有 409 → 翻卡片；404/5xx/网络失败 → 保持 pending + 错误提示 + 可重试。**
「已经决」的真正兜底不靠错误码，而是 `permission/resolved` 投影事件把卡片移出待决队列。

### 本次收尾做了什么（零产品行为改动）

1. `web/src/lib/api.ts`：`postApproval` 的 docstring 原写「404 with "already resolved"
   detail = same semantics → AlreadyResolvedError」，与代码相反 → 已按上表改写。
2. `web/e2e/n-approval-card.spec.ts`：+1 用例 ×2 视口「**POST 404 → 保持『需要审批』**
   + 错误文案含 404 + 按钮仍可重试」。此前 404 路径**零覆盖**；变异验证（注入
   `if (404) throw AlreadyResolvedError`）→ 两视口变红，还原后全绿。
3. `docs/PROMPT_FRONTEND_NEXT_BATCH.md`：原文把错误前提写进任务与验收标准 → 加
   2026-09-11 订正块（保留原文 + 明确 404 不走已决）。

### 既有批次已锁定的部分（无需重复做）

- `ApprovalCard.tsx` catch：`AlreadyResolvedError` → 翻卡片；其它错误 → 保持 pending +
  `role="alert"` 提示 + 按钮重新可用（可重试）。
- e2e `n-approval-card.spec.ts`：**POST 500 → 保持 pending + 可重试（连点两次）**、
  **POST 409 → 翻「已批准」**、200 → 翻、`permission/resolved` → 移除卡片。
- 单测 `api.test.ts`：200/409/500/422 四例。
- 变异验证：还原旧「任何错误都翻卡片」→ 500 用例变红；禁用 `AlreadyResolvedError`
  分支 → 409 用例变红。

### 集成后建议核对

```bash
cd D:\intelligence-agent-frontend/web
npx tsc -b && npx vitest run && npx oxlint && npx playwright test --workers=2 && npx vite build
# 门禁基线：vitest 501 passed / oxlint 35 warnings 0 errors / playwright 118 passed
```

---

## 4. OBS-012：`bash` 工具实为 cmd.exe / sh——工具描述诚实化

**状态**：✅ 完成，commit `d9bef3a`（分支 `feat/backend`，未 push）。

### 根因

工具名 `bash` 是历史名称，**没有任何后端真的调 bash**：

| 后端 | 实际机制 | 真实解释器 |
| --- | --- | --- |
| `LocalSubprocessSandbox`（Windows） | `Popen(shell=True)` → CPython 用 `%COMSPEC%` | `cmd.exe` |
| `LocalSubprocessSandbox`（POSIX） | `Popen(shell=True)` → CPython 用 `/bin/sh` | `/bin/sh` |
| `DockerSandbox` | 硬编码 `["/bin/sh", "-lc", command]` | `/bin/sh` |

模型按工具名写 bash 语法，会被真实解释器**直接拒绝**：本机实证 cmd.exe 对 bash 语法报
「此时不应有 i。」——OBS-011 的乱码复现正是这条报错，整次工具调用作废。

规范面：`05_SANDBOX_CODING_TOOLS.md` §117 只规定 bash 工具返回 exit_code/stdout/stderr，
**对 Windows/shell 选择无任何要求** → 这是代码诚实性问题，不是规格冲突。

### 修法（不改执行语义，只让声明与真相一致）

1. `Sandbox` 新增**非抽象** property `shell_description`（**不进 ADR-0001 冻结的
   6 个抽象方法契约**——抽象化会让既有/第三方后端无法实例化）。基类保守默认 `"sh"`。
2. `LocalSubprocessSandbox` → `%COMSPEC%` 的 basename（剥离可能的引号、空值回落
   `cmd.exe`）；`DockerSandbox` → `/bin/sh`（与 exec 一致）。
3. `BashTool.description` 按 Sandbox 的声明如实写出解释器名并否认 bash；声明为 cmd 系时
   追加 cmd 专有陷阱（单引号非引用符 / `$VAR` 不展开 / `cat`·`ls` 不可用 / `2>/dev/null` 无效）。
   **不用 `os.name` 猜**：宿主是 Windows 时 Docker 容器内仍是 sh。

### 交付物

| 文件 | 性质 |
| --- | --- |
| `src/agent_harness/sandbox/base.py` | 新增非抽象 property `shell_description` |
| `src/agent_harness/sandbox/local.py` | 覆写：COMSPEC basename / `/bin/sh` |
| `src/agent_harness/sandbox/docker.py` | 覆写：`/bin/sh` |
| `src/agent_harness/tools/bash.py` | `description` 据声明合成（含 cmd 陷阱） |
| `tests/tools/test_coding_tools.py` | 新增 `TestBashToolShellHonesty`（10 例，1 例平台跳过） |
| `tests/agent/test_context_runtime.py` | `Mock(spec=Sandbox)` 显式声明 shell（假件补全） |

### 门禁与证据

- ruff clean；全量 pytest **1537 passed / 10 skipped / 39 deselected / 0 failed**。
- 变异验证：① 还原旧描述 → **3 个用例变红**；② 把 Local 硬编码成 `"cmd.exe"` →
  「取自 COMSPEC」用例变红（该用例注入**带引号的独有值** `"C:\opt\weird\mystery-shell.exe"`，
  同时锁住引号剥离——初版断言「与 COMSPEC 相等」在本机无法区分硬编码，已按独立复核意见加强）。
- 独立 code-review：**SOUND / approve，零 P0/P1/P2（本 scope 内）**。复核实证
  `shell=True` 在本机确实走 cmd.exe（`echo %OS%`→`Windows_NT`、`ver`→Windows 版本串），
  并确认改描述**不破坏任何工具 schema/提示快照**（无测试断言该文本）。

### 有意未做（附理由）

- **不真去调 bash**（不探测 Git Bash / WSL）：宿主依赖强、Alpine/Debian 精简镜像无 bash、
  且属执行/安全语义变更，超出 OBS-012 scope。
- **不改工具名 `bash`**：`"bash"` 是载荷（`profiles.py:_CODING_TOOLS`、前端
  `ToolCard`/`StepDetail` 按 `tool.name === 'bash'` 渲染、审批/权限测试、已落库 SessionEvent
  工具名）——改名是跨端破坏性变更。
- 两项 P3 保留（已在提交说明与复核中记录理由）：基类默认 `"sh"` 可能被忘记覆写的第三方
  后端误用（有 docstring 声明，且当前两个具体后端都覆写）；`"cmd" in shell.lower()` 是宽松
  子串门（`tccmd.exe` 误触发无害；COMSPEC 指向 PowerShell 时描述诚实但无语法提示）。

## 5. OBS-016（本轮新发现，**未修**，Scope 外）：`ReadTool` 的超长行提示仍教模型用 POSIX 专有命令

**位置**：`src/agent_harness/tools/read.py:140`。

单行超过 `_READ_MAX_BYTES` 时，ReadTool 往模型可见的 `content` 里追加：

```text
Use bash with 'sed -n '{start}p' <file> | head -c {_READ_MAX_BYTES}' plus 'tail -c +N' to read further segments.
```

在本机（Windows 本机 → cmd.exe）`sed` / `head -c` / `tail -c` **都不存在**，cmd.exe 会回
`'sed' is not recognized...`。这与 OBS-012 是**同一类缺陷**（模型可见文案描述了一个不存在的
解释器环境），目标场景同样会复现。

**为什么本轮未顺手修**：① 属另一个工具（ReadTool），不在 OBS-012（bash 工具描述）scope 内
（AGENTS.md §8 Scope Lock）；② 该字符串被前端 handoff 文档 `docs/HANDOFF_FRONTEND_SYNC.md:46`
当作契约引用，改它需要跨端同步。**独立复核也建议只报告不修**。

**建议的最小修法**（交给集成 AI 排期）：把该提示改成后端无关的表述（如「用 shell 工具按
字节/行切片读取后续内容」），或按 `sandbox.shell_description` 给对应平台的示例；若采用后者，
需同步 `docs/HANDOFF_FRONTEND_SYNC.md:46`。
