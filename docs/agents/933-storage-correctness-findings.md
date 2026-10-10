# #933 存储层正确性（M-01 / M-04 / M-16）— 修法与证据

> 票据：#933（MM 系列审计终审 `final-findings.md`：M-01 P1、M-04 P3、M-16 P3）。
> 分支：`claude-code/933-storage-correctness`，基线 `1beb8d317cb34b3839a01fae055f63aba7bddce9`。
> 本文件是**施工侧的事实落点**（推导 / 来源 / 证据读数）；机制叙述的去处见各代码文件
> docstring 与 `docs/agents/830-d1-global-attachment-store-design-proposal.md`。

## 0. 方案依据（协议 §1.3；AGENTS §6.1）

**性质判定**：M-01 是**存储布局迁移**（新增全局对象根 + 归属事实 + 双读回落），不是纯缺陷修复
⇒ 不适用 §1.3 的"纯缺陷修复"豁免，本块必须落。来源均为**本地浅克隆 + `file:line` + commit**，
两处上游均 MIT，只做 **PORT DESIGN**（不复制代码、不引入依赖：本票零新增依赖）。

| 来源 | 版本 / commit | 文件:行 | 机制（读到的事实） | 契合点 | 判定 |
| --- | --- | --- | --- | --- | --- |
| DeepSeek Harness | `5badb15009ae1756c3afe0ae0cef1faafc290ccc`（MIT） | `packages/attachment/attachment-local/src/store.ts:44-54` | 对象路径 = `join(root, 'objects', sha[:2], sha)`，`root` = `DSH_HOME/attachments/v1`（`index.ts:176`）⇒ **单一全局根、无 session 段** | 与本仓 `.attachments/objects/<sha[:2]>/<sha>` 同形状（`<sha[:2]>` 分片、内容寻址）；远端只需把根从"会话内"上提到"bucket 级" | **PORT DESIGN** |
| DSH | 同上 | `store.ts:214-229`、`309-348`、`390-394` | 发布完整性 = **流式边写边算的摘要** vs 调用方期望摘要；去重命中时 `digestFile(target)` **读回文件**再算 | M-04：自证必须读**落盘字节**；本仓顺带保留"去重命中时读回已有对象自证"（`_publish_blob` 的 `FileExistsError` 分支） | **PORT DESIGN** |
| DSH | 同上 | `packages/attachment/attachment/src/index.ts:53-265` | `AttachmentStore` ABC 无任何"默认实现换个语义"的方法：能力缺失一律显式 `reject`（`ATTACHMENT_FILES_UNSUPPORTED` / `ATTACHMENT_PROJECTION_UNSUPPORTED`） | M-01：`load_uploaded_bytes` 的 `= load_bytes` 默认实现与这条相反（**静默**换语义、且失败方向是放宽授权）⇒ 删默认、改 `NotImplementedError` | **PORT DESIGN** |
| DSH | 同上 | `attachment/src/types.ts:83-108`、`api/session-controller/src/commands.ts:354,405-410` | 归属来自**事件流**（persist-before-event；wire 调用方不可能引用自己没上传的 id；读授权 = 本会话事件流引用 `ATTACHMENT_NOT_REFERENCED`） | 本票**不动**读闸门（`web/attachments.py` 的事件引用检查）；发送侧归属仍需"本会话上传过"的事实 ⇒ 远端加**会话回执对象** | **PORT DESIGN** |
| oh-my-pi | `a507b6235d82f66d53183677b68a85da26c2eb29`（MIT） | `packages/coding-agent/src/session/blob-store.ts:1-68` | `blob:sha256:<hex>`、`BLOB_HASH_RE=/^[a-f0-9]{64}$/`、**扁平单一全局根** `<dir>/<sha256-hex>`；docstring 逐字 "Content-addressing makes writes idempotent and provides automatic deduplication **across sessions**" | 第二处独立实现佐证"内容寻址字节库只有一个全局根、归属不是存储层的事"；本仓 `BYTE_ARTIFACT_ID_PATTERN` 与它的哈希形状同构 | **PORT DESIGN** |

**⚠ 票面「成熟产品做法」段与一手证据的冲突（已上报，见 §4）**：票面写"DSH 的 S3/MinIO Provider 各自
实现同一寻址语义"，**一手核实为不成立**——DSH `packages/attachment/` 只有契约包 `attachment/` 与本地包
`attachment-local/`，全仓 grep `attachment-s3|attachmentS3|S3Attachment|attachment-remote|attachment-gcs`
**零命中**。⇒ 该句作废；本票的远端语义由"DSH 本地单根 + oh-my-pi 扁平单根 + DSH 归属模型"三条一手
证据推导，而非照抄一个不存在的实现。

**License 结论**：两处上游均 MIT；本票只借鉴**目录/键形状与授权模型**，不复制任何上游源码行，
不新增依赖。

## 1. M-01（P1）：远端 Provider 未实现归属 + 未走全局寻址

**第一性原理推导（一句话）**：ABC 的默认实现编码的是"会话命名空间即归属"这一**本地 Provider 特有**
假设，而内容寻址字节库的正确模型是"**一个全局根 + 归属另说**"（DSH / oh-my-pi 两处独立实现均单根）
⇒ 默认实现既不该存在，远端也必须真的走全局寻址、并把归属事实显式化。

**改动**：

1. `ArtifactStore.load_uploaded_bytes`：删掉 `return await self.load_bytes(...)` 默认实现，改
   `NotImplementedError`（点名具体 Provider）。
2. `S3ArtifactStore` / `MinioArtifactStore`：
   - `save_bytes` 写**两份 key** —— 本会话回执 `{sid}/attachments/<sha>` + 全局对象
     `.attachments/objects/<sha[:2]>/<sha>`（同一 body）；
   - `load_bytes` 按候选顺序读：全局优先 → 回落本会话旧 key（**不回填**，与 Local #830 D1 同策略）；
   - `load_uploaded_bytes` 只读本会话回执 ⇒ 归属不放宽、且升级前的旧对象天然仍属本会话。
3. 新增 `storage/attachment_layout.py`：三个 Provider 共用一份布局常量（见 §3）。

**发布顺序（刻意的，写在代码注释里）**：**回执先、对象后**。S3 单对象存储没有跨键事务，必须挑一个
"半途失败也无害"的顺序：回执先落 ⇒ 失败面只能是"有回执、没对象"，而回执 key **本身就是**
全局化之前的旧对象位置（在读回候选里）⇒ 那种半成品**仍读得回字节**、归属也成立；反过来（对象先、
回执后）失败面是"有对象、没回执"：**别的会话读得到、本会话发不了**——归属静默失守，最不该出现的
失败面。不变量仍是「**有回执 ⇒ 有对象**」（回执指向的字节永远真实存在）。

**TDD**：`tests/attachments/test_byte_store_providers.py` 新增 **8 条用例**（其中 7 条由
`remote_provider` fixture 参数化跑 minio/s3 两遍 ⇒ 该文件收集数 **9 → 25**：写两份 key 且顺序
正确、fork 子会话读回父会话字节、旧 key 单读回落、归属只看本会话回执（且**不发**全局请求）、
归属认升级前旧 key、字节/归属两侧畸形 id 零请求、全局对象被篡改 → `KeyError`；另 1 条
`test_minio_load_bytes_roundtrip` 为 minio 单跑基线，原名
`test_minio_save_bytes_uses_session_prefixed_key` 的用例改名 `..._writes_receipt_key_first`）。
同票 `test_local_byte_store.py` 另加 M-16 5 条（候选顺序契约 + 加一条候选的可扩展性），
`tests/attachments` 全体 **82 → 103**。红：**`8 failed, 17 passed`**（当时该文件 17 条）；
绿：**`101 passed`**（`tests/attachments` 全体，改动后复测 103）。

> 口径订正：本行初稿写"新增 6 条用例"，与 `git show 6bd66d01` 的 8 个新 `def test_` 及上述
> 实测收集数不符（修后重审 Standards P3 指出），已按实测改正。

## 2. M-04（P3）：`_publish_blob` staged 自证的恒真式

**第一性原理推导（一句话）**：自证的目的是"**落盘字节** == 预期摘要"，而改动前比的是"内存 == 内存"
（`sha256` 由同一份 `data` 派生）⇒ 改成**读回暂存文件**再算摘要（DSH `digestFile` 读回语义），
内存那份删除（被读回那份包含）。

**消融实验（前后对比，隔离副本 `~/pytest-933/ablation-m04`，`git archive 1beb8d31` 解包）**：
同一注入手法（覆盖 `os.write` 做**等长** LF→CR 改写，并 `delattr(os,'O_BINARY')` 让"平台改写"不被
`O_BINARY` 挡住）、同一 payload `b"first line\nsecond line\n"`：

| 读数 | 修前 | 修后 |
| --- | --- | --- |
| `save_bytes` | **正常返回**（恒真式通过） | 抛 `OSError: staged bytes do not match their publication digest` |
| 对象是否已发布 | **True**（坏字节发布成功） | **False** |
| 落盘字节 | `b'first line\rsecond line\r'`（≠ 原文） | —（未发布） |
| staging 残留 | — | `[]` |
| 之后 `load_bytes` | `KeyError`（hash mismatch ⇒ 用户侧 404 / 模型侧占位符） | —（坏字节从未进存储） |

阳性对照：修后正常字节照常发布并读回（`b'ok\n'`）——校验不是恒假。
回归用例（主工作树，实现无关：**不** patch `hashlib.file_digest`，换读回算法不该让它变红）：
`tests/attachments/test_local_byte_store.py::test_publish_rejects_staged_bytes_expanded_by_platform_text_mode`
/ `::test_publish_rejects_same_length_corruption` / `::test_publish_self_check_passes_for_byte_exact_staging`
—— 隔离副本上**修前 2 failed / 1 passed**，主工作树**修后 3 passed**。

## 3. M-16（P3）：读回候选不可扩展

**第一性原理推导（一句话）**：候选清单是**布局演化的兼容面**，其构造与消费必须在**读回路径**
收口同一处，否则
"加一条候选"要在对象读与旁挂元数据读两处各改一次，漏一处得到"对象读得到、旁挂读不到"的静默错配
（症状最轻：mime 退化成 `application/octet-stream`）。

**改动**：`_blob_object_candidates` 的返回类型放宽为 `tuple[Path, ...]`、就地写全每一条的来由与
扩展方式；`_read_blob_meta` 改为**从同一份候选派生**旁挂路径（`候选.with_name(name + ".json")`），
删掉本次改动产生的孤儿 `_session_blob_meta_path`（读侧不再另列清单）；布局字面量收进新的
`storage/attachment_layout.py`，Local 与两个远端 Provider 共用（含 `_RECEIPT_RELATIVE_PARTS`
由常量拼出）。
per-session 回落**显式标注为升级兼容包袱、本票不删**（删需旧附件迁移前提，见方法 docstring）。

**可扩展性判据**：`test_adding_a_candidate_layout_needs_no_consumer_change` —— 只 monkeypatch 给候选
加一条假想下一版布局，对象与**旁挂**（mime=image/webp）同时可读；改动前该用例红。

## 4. 票面偏离与需裁决项（AGENTS §9.1.1 / §3.1）

1. 票面「成熟产品做法」段的 DSH S3/MinIO 断言不成立（§0 末），已按一手证据作废；**未因此改票面范围
   或验收判据**，只是把依据换成真实存在的一手来源。
2. 票面 [M-01] 给"二选一"（覆写远端 / ABC 改 `NotImplementedError`），实施判定两者各缺一半：
   只改 ABC ⇒ fork 404 症状仍在；只覆写远端 ⇒ 必须先有远端全局根、且必须补归属事实。
   ⇒ 实施取**并集**（§1），范围与票面"三个修法 + 补 fork 回归"一致，未扩大。
3. `docs/agents/830-d1-...-design-proposal.md` §8.1 原文把"改远端 key"判为独立票 ⇒ 本票即那张票，
   该节第 1 条已就地更新为"已由 #933 M-01 关闭"（原文留痕、加删除线）；§4 的"ABC 默认实现"口径
   同步更正。两处均属**已登记缺口的兑现**，不是悄悄改口径。

## 5. 已知取舍（本票不修，如实登记）

1. **远端无可选 `delete`**：`discard_local_artifacts` 只删本地目录；远端字节对象不回收（既有语义，
   ADR-0029 D6 另票）。
2. **远端孤儿字节**：全局对象跨会话去重 + 会话硬删后仍在（与 Local 同款，#830 方案文档 §8.2 已登记，
   GC 属后续票）。
3. **升级前旧远端对象 + fork**：旧 key 只在**上传者会话**下；fork 子会话读**全局未命中**才回落，
   而它回落的候选是自己的 `{child}/attachments/<sha>` ⇒ 升级前的旧远端对象在 fork 后仍读不回
   （与 Local 的 #830 方案文档 §8.3 同款缺口，同样登记为可接受）。**升级后**新上传的对象走全局根 ⇒
   fork 正常（本票新增用例所钉）。
4. **`_read_blob_meta` 的派生路径**：由候选派生意味着"旁挂必须与对象同名 + `.json`"。这是既有约定
   （`_global_blob_meta_path` 与对象同形），但此前读侧有一份独立清单可各自演化；收口后新增布局
   必须遵守该约定（方法 docstring 已写明）。
