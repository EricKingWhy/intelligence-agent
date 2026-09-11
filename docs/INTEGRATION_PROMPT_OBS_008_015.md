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
| OBS-015 | P2 | ⏳ 待做 | — | `ApprovalCard.tsx` catch 乐观翻转（**前端 worktree**） |
| OBS-012 | P2 | ⏳ 待做 | — | `bash` 工具实为 cmd.exe：描述诚实化 + 测试锁 |
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
