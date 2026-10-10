# #830 MM-08 D1 方案文档：附件字节改全局内容寻址对象根

- 状态：施工中（用户 2026-10-09 选定**方案 b**）
- 票据：#830（MM-08 D1）
- 分支：`omp/fix-mm08-d1-global-attachment-store`（基点 `origin/main` = `7fcd0209fec4d79a599b61e6a4761e60c20329f8`）
- 范围：问题 1（fork 子会话读不回父会话附件）；不修 D2/D3（Windows `O_BINARY` / 路径，另支并行）、
  不改远端 Provider、不做孤儿回收。

## 0. 方案依据（AGENTS §6.1）

来源均为**本地浅克隆 + `file:line` + commit**（clone：DeepSeek Harness `~/deepseek-harness`、oh-my-pi `~/workspace/cli-beauty-20261006/oh-my-pi-src`；引用 commit 即各 clone 的 HEAD，可用 `git -C <clone> rev-parse HEAD` 核验），两处均为 MIT，只 PORT DESIGN（不复制代码，不引入依赖）。

| 来源 | 版本/commit | 文件:行 | 机制（读到的事实） | 契合点 | 判定 |
| --- | --- | --- | --- | --- | --- |
| DeepSeek Harness | `5badb15009ae1756c3afe0ae0cef1faafc290ccc`（MIT，Copyright 2026 DeepSeek） | `packages/attachment/attachment-local/src/store.ts:47-54`、`file-store.ts:75-81` | 对象根是 **`DSH_HOME/attachments/v1`（每用户全局，非每会话）**；对象路径 = `objects/<sha256[:2]>/<sha256>`（文件另加 `file-objects/<sha[:2]>/<sha>`）。**没有 session 段**。 | 本项目 D2 已 declared「落盘算法移植 DSH `attachment-local`」；`<sha[:2]>` 分片形状两边一致，改根即可对齐 | **PORT DESIGN** |
| DeepSeek Harness | 同上 | `packages/api/session-controller/src/commands.ts:405-410`（谓词 `:682`） | 读授权 = **「该 id 被本会话事件流引用」**，否则 `ATTACHMENT_NOT_REFERENCED`；写入侧图片以 inline base64 到达（`prompt()` `:307-370`），因此 wire 调用方**不可能引用到自己没上传的 id** | 本项目读闸门（`web/attachments.py` GET）与它同构，本方案**不动**；写入侧我们按 id 引用，故须自建等价的所有权事实（见 §5） | **PORT DESIGN** |
| oh-my-pi | `a507b6235d82f66d53183677b68a85da26c2eb29`（MIT，Copyright 2025 Mario Zechner；clone `~/workspace/cli-beauty-20261006/oh-my-pi-src` 的 HEAD） | `packages/utils/src/dirs.ts:962-964`、`packages/coding-agent/src/session/blob-store.ts:8,40-68,180-234,295-320`、`session/session-persistence.ts:217` | 附件字节存**全局**内容寻址 blob store（`~/.omp/agent/blobs/<sha256-hex>`），会话文件里只留 `blob:sha256:<hex>` 引用；docstring 原文：*"Content-addressing makes writes idempotent and provides automatic deduplication **across sessions**"*；`put` 同长度已存在即复用（去重）、staging + rename 原子发布；`parseBlobRef` 用 64 位小写 hex 正则做**单一收口**，防 `../` 越出 blob 根 | 「事件流存引用、字节外置且跨会话去重」的第二个独立实现；正则收口与我们 `BYTE_ARTIFACT_ID_PATTERN` 同形状；本项目 PRD「Further Notes 1」已把它的 `blob:sha256:` 记为直接先例 | **PORT DESIGN** |

**为什么不是别家**：Pi / Claude Code / Codex / Gemini CLI 把 base64 内联进会话文件（PRD「Further Notes 1」已核实），
与不变量 #15「大内容优先外置」相反，不作候选；LobeChat 式无鉴权公开链接被 PRD「Out of Scope」明确排除。

**License 结论**：两处上游均 MIT，且本方案只借鉴**目录形状与授权模型**，不复制任何上游源码行。

## 1. 问题与根因（D1）

证据件：`docs/tickets/multimodal-2026-10-07/MM-08-verification-evidence.md` §5 D1。

- 现象：父会话上传并发送图片 → `POST /api/sessions/{id}/forks` → 子会话事件流**带着** `child_refs=["sha256:a7c8e890…"]`，
  但子会话读数接口 404（`attachment 'sha256:…' 不在会话 '…' 的命名空间里`），子会话模型请求里图片退化成占位符。
- 根因（代码面）：`LocalArtifactStore` 的字节对象路径是
  `<artifact_dir>/<session_id>/attachments/objects/<sha[:2]>/<sha>`（会话内命名空间），
  而 `session/fork.py::_copy_workspace` 只复制 workspace、**绝不复制附件对象**（其 docstring 本就写着
  「Artifact 是全局 store 的内容寻址 ref…绝不复制」）。
  ⇒ 子会话 store 的命名空间里没有这个对象，`load_bytes` 抛 `KeyError` → 404 / 占位符。
- 即：**读语义（全局可寻址）与写语义（会话内命名空间）自相矛盾**——fork docstring 描述的是本方案要落地的语义。

## 2. 决策

把附件字节的**存储根**从「会话目录内」上提到「artifact 根下的全局对象根」，读取按内容寻址跨会话可寻址；
**授权不改**：读仍要求「本会话事件流引用该 id」，发仍要求「本会话上传过该 id」。

不做的事（显式记录）：不改 `fork.py`（它本来就零拷贝）、不改远端 Provider、不做孤儿回收、不动读闸门语义。

## 3. 新旧路径对照表（Local Provider）

| 用途 | 旧路径 | 新路径 |
| --- | --- | --- |
| 字节对象 | `<root>/<sid>/attachments/objects/<sha[:2]>/<sha>` | `<root>/.attachments/objects/<sha[:2]>/<sha>` |
| 元数据旁挂 | `<root>/<sid>/attachments/objects/<sha[:2]>/<sha>.json` | `<root>/.attachments/objects/<sha[:2]>/<sha>.json` |
| staging | `<root>/<sid>/attachments/tmp/<uuid>` | `<root>/.attachments/tmp/<uuid>` |
| **会话上传回执**（新） | —（旧语义把它当"唯一对象"） | `<root>/<sid>/attachments/objects/<sha[:2]>/<sha>`（hardlink 指向全局对象） |
| 文本 artifact | `<root>/<sid>/<artifact_id>`（不变） | 不变 |

`<root>` = `Settings.artifact_dir`。

**为什么全局根用点号目录 `.attachments`**：`SESSION_KEY_PATTERN = [A-Za-z0-9_-]{1,128}` **不**匹配前导点，
因此名为 `attachments` 的会话**不可能**与全局根同名——`discard_local_artifacts` / `delete_local_artifacts` /
`_artifact_dir_for` / `_list_local_artifacts` 都是「`root` + session_id 拼路径」，点号目录让"全局根被某个会话的
rmtree 连带删除"在构造上不可能（同 `sandbox/registry.py:181-192` 的 `.fork-tmp` 先例）。
**为什么保留 `objects/<sha[:2]>/` 分片**：与 DSH `store.ts:47-54`、Git 对象库同形状，避免单目录上万文件。

## 4. 读写语义

写（`save_bytes`）：
1. 段落纪律不变：staging → 写满 → `os.fsync` → `os.link` 到**全局**目标 → 校验已有对象完整（去重命中时）→ `chmod 0o400` → 目录 fsync；
2. 再在**本会话**的旧路径建一条 **hardlink 回执**（同一 inode，不复制字节），并 fsync 该会话目录链。
   ⇒ 不变量：**有回执 ⇒ 有对象**（回执在对象之后建；对象可从回执独立读取）。
3. 新写入只写全局元数据旁挂（不再写会话内旁挂）。

读（两种语义，两个方法）：
- `load_bytes(id)`（读回/投影用）：**全局优先，未命中回落本会话旧路径** → 未命中才 `KeyError`。回落保证
  升级前已落盘的旧对象不被 brick。
- `load_uploaded_bytes(id)`（**发送归属**用，新窄方法）：只读本会话路径（= 回执 / 升级前的旧对象）。

  ⚠ **本票（#830 D1）当时给 `ArtifactStore` 留的默认实现 `= load_bytes` 已由 #933 M-01 删除**
  （改为 `NotImplementedError`）：它的前提是"远端 Provider 的字节日志 key 本就是
  `{session_id}/attachments/{sha}` ⇒ 两者等价"，而 #933 把远端也改成全局寻址之后这个前提不再成立
  （全局对象**必然**跨会话读得到），留着它就是把"别的会话的字节"判成"本会话上传过"。**每个 Provider
  必须各自实现**归属（Local = 会话回执 hardlink；远端 = 会话回执对象 key）。当时"行为零变化"的判断
  只对**当时的**远端 key 布局成立——原文保留以留痕，口径以本条为准。

## 5. 授权闸门（不弱化，逐条对齐 PRD）

| 边界 | 判据 | 本方案 | 回归钉 |
| --- | --- | --- | --- |
| 读字节（GET） | id 被**本会话**事件流引用（PRD D5 / DSH `ATTACHMENT_NOT_REFERENCED`） | **不动**（`web/attachments.py` 先查 `referenced_attachment_ids`） | `tests/web/test_attachments_api.py::test_other_session_id_is_404`、`test_uploaded_but_unreferenced_is_404` |
| 发送引用 | id **属于本会话**（PRD D5 原文） | 改走 `load_uploaded_bytes`（会话回执），**不放宽** | `tests/web/test_send_message_attachments.py::test_send_with_other_session_attachment_is_422` |
| 跨会话读 | 别的会话拿到 id 也不可读 | 不变（上表第一行即判据） | 同上 `test_other_session_id_is_404`（含 404 文案形状不可区分） |

「存储全局化」**不会**新增跨会话共享面：别的会话的事件流里出现该 id，只有两条路——本会话上传后发送（回执闸门）、
或 fork 血缘（种子事件逐字复制）。二者都不是"跨会话共享附件"（PRD Out of Scope 指的是 LobeChat 式无鉴权共享/公开链接，本方案不引入）。

## 6. fork 语义（零拷贝）

`fork` 之后：子会话种子事件自带 `attachments` 引用 ⇒ 读闸门放行 ⇒ `load_bytes` 全局命中 ⇒
GET 200 / 模型载荷含图片块。子会话目录**不产生任何附件字节**（`child_artifact_dir_exists=false` 不变，
只有父会话目录里的回执，子会话自己的 store 不写任何东西）。`fork.py` 零改动。

## 7. 兼容与迁移

- **不回填、不迁移**：升级不搬动任何旧对象；旧对象继续在原会话路径被读到（`load_bytes` 回落）。
- 升级后既有会话：读取 ✓（回落）、发送 ✓（旧路径即回执）、fork *之后*的新子会话读旧对象仍 404（见 §8）。
- 无需改任何调用方签名；`web/attachments.py` 读路径与 `context/builder.py` 投影路径**一行不改**（它们用的就是 `load_bytes`）。

## 8. 已知取舍与未修项（如实登记，不在本票范围）

1. ~~**远端 Provider（S3/MinIO）不修 —— 本票对 D1 的修复只覆盖 Local Provider（默认）**：字节日志 key 仍是
   `{session_id}/attachments/{sha}`，fork 后子会话读不回的问题在这两个 Provider 上**仍然存在**（即 S3/MinIO
   部署下 D1 = 未修复，仍是已知缺口）。理由：本票根因与证据都在 Local；改远端 key 是存储布局迁移
   （需要回填/双读），属独立票（PRD D2 的"加法式扩展"边界）。⇒ 关单 comment 与 PRD 层面**不许**把这点
   含糊成"fork 继承引用已修"，必须写明"仅 Local Provider"。~~
   **已由 #933 M-01 关闭（2026-10-10）**：远端 Provider 改走**同一套**全局内容寻址语义——对象落
   `<bucket>/.attachments/objects/<sha[:2]>/<sha>`，加一份每会话回执对象 `<bucket>{sid}/attachments/{sha}`
   作为归属事实（对象存储无 hardlink，回执是对象副本；发布顺序反过来为"回执先、对象后"，不变量仍是
   「有回执 ⇒ 有对象」）。旧 key 双读回落、**不回填**（与本文件 §7 对 Local 的策略一致）。`ArtifactStore`
   的 `load_uploaded_bytes` 默认实现已删（改 `NotImplementedError`）⇒ 不再存在"默认路径静默换个语义"。
   **本条原判的"属独立票"已兑现**（#933），此处保留原文以留痕，不再作为未修缺口。
2. **孤儿字节不回收**：对象全局驻留 + 内容寻址去重，会话硬删后全局对象仍在（磁盘增长）。PRD D2 已登记
   "v1 不做自动清理、孤儿附件保留"；本方案把保留面从"未发送的上传"扩到"被删会话的字节"，同属后续票
   （GC 需引用计数/宽限期，DSH 有 `gc`、oh-my-pi 有 `omp gc`，我们暂无）。
3. **旧对象 + fork 仍 404**：升级前上传的、且只在会话内存在的对象，fork 出的子会话读不回来（全局无对象、回落只看自己命名空间）。
   修它需要 fork 时搬对象或对父链解码，与"fork 零拷贝"冲突；登记为可接受缺口。
4. **会话内旁挂元数据不再写**：升级前写入的旁挂仍被读到（回落），新写入只写全局旁挂。

## 9. 测试计划（TDD，先红后绿）

| 层 | 用例 | 现在 | 修后 |
| --- | --- | --- | --- |
| HTTP（新） | 父上传+发送 → fork → 子会话 GET 附件 → 200 + 字节逐字节相等 | **404（红）** | 200 |
| HTTP（既有） | 别会话 GET → 404；已上传未引用 → 404 | 绿 | 绿（不变） |
| HTTP（既有） | 别会话 send 该 id → 422 | 绿 | 绿（回执闸门） |
| 存储（改写） | 跨会话 `load_bytes` 可读（新语义）+ `load_uploaded_bytes` 跨会话 KeyError | — | 绿 |
| 存储（新） | 旧路径对象仍可读（回落）+ 旧路径对象可发送（回执兼容） | — | 绿 |
| 存储（新） | 两会话上传同字节 → 全局对象仍只 1 份（跨会话去重） | — | 绿 |
| 存储（新） | `discard_local_artifacts` 删会话目录后，全局对象仍在（删除语义） | — | 绿 |
| 存储（改写） | 篡改检测 / 权限 0o400 / staging 半途失败 / SIGKILL / 元数据缺失回落 | 绿 | 绿（路径改全局） |
| 链路（重跑） | MM-08 AC3 驱动（真 HTTP + 真 fork） | 红 | 绿 |

`tests/attachments/test_byte_store_providers.py`（MinIO/S3 key 形状）与 `tests/session/test_retention.py` 用文本 artifact，
**不受影响**（文本路径未改）。

## 10. 三问自检

1. **用户可见行为变了吗？** 变了、且是修复目标：fork 后子会话的附件读回从 404 → 200，模型载荷占位符 → 真图。
   未被请求的行为（授权、错误码、端点形状、文本 artifact）不变。
2. **边界/失败面怎么保证不退化？** 授权三条闸门各有既有回归钉（§5 表）；发布纪律（staging/fsync/hardlink/0o400/篡改检测）
   与 SIGKILL 用例原样保留、只改路径；新增回执后**对象仍是唯一权威**，回执只是指向同一 inode 的链接。
3. **最小性？** 改 1 个 Provider 的路径构造 + 读回落 + 1 条回执；ABC 加 1 个**有默认实现**的窄方法（零 Provider 改动）；
   调用方只改 1 行（发送归属）；不动 fork / 不动读闸门 / 不改远端 / 不引入依赖。

## 11. 与 PRD 的关系（需用户裁决的文案张力）

- `docs/PRD_MULTIMODAL_IMAGE_INPUT.md` D2 原文只说"落盘算法移植 DSH `attachment-local`"、"内容寻址"，**未**规定根的位置；
  本方案把根从会话内上提到全局，属于 D2 的实现细化，**不与之冲突**。
- 「Out of Scope」有一条 **"跨 session 共享附件、公开分享链接（LobeChat 式无鉴权路由明确不做）"**。本方案**不**新增共享面
  （§5），但"字节在全局根、可跨会话去重"这一事实与该条的**字面**读法存在张力。
- 建议的 PRD 最小措辞修订（**待用户批准，本次不改 PRD**）：D2 增一句「字节对象落在 artifact 根下的**全局内容寻址对象根**
  （跨会话去重；DSH `store.ts` / oh-my-pi `blob-store.ts` 同构），鉴权仍在事件引用与会话上传回执」；
  Out of Scope 那条改为「跨 session **复用/分享附件 id** 的产品入口、公开分享链接」。
