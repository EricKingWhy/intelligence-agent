# MM-08 跨端与恢复验证 · 证据包（#830）

> **票型**：验证票（P1 Gate）。**只验证，不新造机制**；发现的缺陷以票面 + 证据回报，不在本票顺手修。
> **规格**：`docs/tickets/multimodal-2026-10-07/MM-08-cross-surface-recovery-gate.md`（Parent #821）。
> **本文件** = 证据包的落盘形态（Markdown；运行时原始读数在 `docs/evidence/` 与下文的运行目录里）。

## 0. 执行信息

| 项 | 值 |
| --- | --- |
| 执行器 | omp 18.8.5（经 `~/workspace/system/omp` 包装脚本） |
| 模型路线 | lighthouse `deepseek-ai/DeepSeek-V4.1-Flash`（主用） |
| 降级情况 | **零降级**：全程未触发 kunyou `deepseek-v4.1-flash` 备用线（未出现 401/429/5xx/空响应/工具乱码）。子代理 `task`/`scout` 三条均因渠道无候选（`422 model not found: deepseek-v4.1-flash`）不可用 → 主 agent 独跑，**这不是降级**，是委派不可用 |
| 运行位置 | ① 本机 Linux 云电脑（轻量复验）；② 云端 Docker 沙箱 `shell/omp-830-mm08`（重活，id `sbx_001m4gawxh16gcey076fg7envb0`） |
| 分支 | `omp/830-mm08-cross-client-verify`（worktree `~/workspace/intelligence-agent-wt/mm08-cross-client-verify/`） |
| 基线 | `origin/main` = `9c5bc1b4044b899e705b5d2df527d4d6dfe03df1` |
| 脚手架提交 A″ | `b795d41cf9b6a180fa78244f34ce90af13272864`（3 文件：`scripts/mm08_stub_server.py`、`scripts/verify_830_mm08.py`、`.github/workflows/mm08-windows-verify.yml`；三者都是**新增**，产品代码零改动） |
| 台账提交 B1 | `1fc9bc54b3540b291df5a51ff3fc08c730b99354`（`docs/review_ledger.d/830-mm08-cross-client-verify.tsv`，range `origin/main..A″`） |
| 本轮修复提交（AC6-VISION / push 触发 / P3-P4） | `09245f03`；其机械归属行 `73e89f22` |
| 本轮 GA 修复提交（子进程 UTF-8） | `fa99acc1`；其机械归属行 `a52ab2bf` |
| GA 真触发读数 | §9 两轮：首轮 [#37941349749](https://github.com/EricKingWhy/intelligence-agent/actions/runs/37941349749)（暴露探针编码 bug）、次轮 [#37941646937](https://github.com/EricKingWhy/intelligence-agent/actions/runs/37941646937)（暴露 D2/D3） |
| 门禁读数 | `docs/gate/1fc9bc54b3540b291df5a51ff3fc08c730b99354.json` = Gate-0 **6/6 PASS**（tip=B1, tree=`944ff04657ea`, 13.0s） |
| 本轮门禁读数 | `docs/gate/7f2114d69cf1058239b92033c42a003ec00ab40f.json`（初版终树 6/6）+ **同步合并后** `docs/gate/c6a74115e8d7258b9b24757686fa730558a09046.json`（6/6，`tree=741ab3e79538`，30.2s——本仓惯例：读数提交本身是最后一个提交） |
| 同步合并 | `c8ee6c71`（`origin/main` `7fcd0209` / #909 → 本线；零冲突、`--cc` 空表、三方树互不相等；归属行 `docs/review_ledger.d/830-sync-merge-5bb62440-c8ee6c71.tsv`） |
| 门禁可用性 | 本轮末段在本机补了 `web` 依赖（`cd web && pnpm install --frozen-lockfile`，pnpm 12.9.1 / node 24.20.0）后，**本机裸全量 Gate-0 即 6/6**（此前因缺 `web/node_modules` 是 4/6——oxlint/tsc 两条车道**阻塞**而非失败）；云沙箱同读数见 §10.1 |

## 1. 五项启动检查表

| # | 实际路径 | 章节 | 证据 |
| --- | --- | --- | --- |
| 1 | `~/workspace/intelligence-agent/AGENTS.md` | 全文 §1-§16（545 行） | 分段读完至 EOF |
| 2 | `~/workspace/system/agent-workflow-prompt.md` | 全文（第零~第五步 + 诚实红线） | 一次读完 |
| 3 | `~/workspace/intelligence-agent/CLAUDE.md` | 全文（确认是指向 AGENTS.md 的指针） | 一次读完 |
| 4 | Issue #830 全文 + Parent #821 的 PRD 相关节 | #830 全部 AC；#821 Problem/Solution/Seams/D1-D12/Testing/Out of Scope | `gh issue view 830` / `gh issue view 821` |
| 5 | 本票派工单 + 协调员简报（含 Windows GA addendum） | 全文 | 由协调员传入，逐行读 |
| 补读 | `goal/.../docs/spec/03_SESSION_EVENT_MODEL.md` §3.1/§7/§8、`06_CONTEXT_ARTIFACT_MEMORY.md` §5/§8/§9 | Artifact Ref 复用语义 / 摘要保留 refs | 实读 |

## 2. blocked_by 核实

命令：`git merge-base --is-ancestor <merge-commit> origin/main`（5/5 退出码 0，全部已是 `origin/main` 祖先）。

| 票 | merge commit | 结果 |
| --- | --- | --- |
| [#824 MM-03](https://github.com/EricKingWhy/intelligence-agent/issues/824) | `c5910dd181de18c6b6962b4e7cd5791b2b9e4fae` | ANCESTOR OK |
| [#825 MM-04](https://github.com/EricKingWhy/intelligence-agent/issues/825) | `002385691e025550a39f9d574b2d8765250bebaa` | ANCESTOR OK |
| [#826 MM-05](https://github.com/EricKingWhy/intelligence-agent/issues/826) | `53585080761171f84a045948263d044448dac554` | ANCESTOR OK |
| [#827 MM-06](https://github.com/EricKingWhy/intelligence-agent/issues/827) | `587a37b416d7eb405ba7217bec98ae016d253e87` | ANCESTOR OK |
| [#828 MM-07](https://github.com/EricKingWhy/intelligence-agent/issues/828) | `6c7c8a137a773ebc5c927650e0939d15eb6d1750` | ANCESTOR OK |

## 3. 验证方法（为什么这算「真跑」）

- **真服务**：真 `uvicorn` + 真 HTTP + 真 JSONL 落盘 + 真附件字节落盘（内容寻址）。
- **唯一替身**：聊天模型 provider。打桩点恰好是 PRD 指定的那条缝——`agent_harness.assembly.create_chat_model` 与 `agent_harness.model.provider.create_chat_model`（后者是压缩摘要的取用点，调用时查询）。传输/存储/事件/装配/压缩/分叉/授权全是真码。
- **三个真实客户端**：
  1. Web：真 `HTTP` + `POST /api/sessions/{id}/attachments`（附件路由）；
  2. TUI：**真 TUI 客户端代码** `tui/src/api.ts::ApiClient`（node 直接 import `.ts` 跑真 fetch，不是复刻一份客户端）；
  3. CLI：**真 CLI 入口** `agent_harness.cli:main` 跑成独立子进程（真 argparse / 真 exit code）。
- **可观测面**：HTTP 状态码、`events.jsonl` 事实、落盘字节 `sha256`、以及**发给模型 provider 的请求体**（替身模型把请求写进 sink，用于断言"图片块 vs 占位符降级"）。
- **确定性**：替身模型 + 固定 PNG 字节 + 无随机时序 ⇒ 同一驱动在两套独立环境（本机 / 沙箱）复现同一读数字形。

跑法：

```bash
export no_proxy=localhost,127.0.0.1 NO_PROXY=localhost,127.0.0.1
export PATH="$HOME/workspace/intelligence-agent/.venv/bin:$PATH" PYTHONPATH="$PWD/src"
python scripts/verify_830_mm08.py --work /home/hatch/pytest-830/mm08-r3   # 全量（AC1-AC7）
python scripts/verify_830_mm08.py --only ac6-cli --work <dir>              # 单条（Windows GA 复用同一断言）
```

退出码语义：`0` = 无 FAIL（`NOT_RUN` 不算失败，但必须在票面登记）。

## 4. 八条 AC 逐条证据

**判据汇总（本机最终跑 `/home/hatch/pytest-830/mm08-r3`，墙钟 61.3s；`counts={'PASS': 8, 'FAIL': 1, 'NOT_RUN': 0}`）**

> 本表是 **AC6-VISION 补跑后**的读数（`NOT_RUN` 归零）：原 `NOT_RUN` 项已由 lighthouse `deepseek-ai/DeepSeek-V4.1-Flash` 真跑转 **PASS**（见 AC6 节）。`FAIL` 仍是产品真缺陷 **D1**（AC3），按验证票口径只登记不修。沙箱（无常视觉凭证）的同批读数仍是 7 PASS/1 FAIL/1 NOT_RUN（§10.1），两处不矛盾——差的正是这一条 keyed 的 AC6-VISION。

| AC | 票面要求 | 探针 | 读数 |
| --- | --- | --- | --- |
| AC1 | A 端附图 → B 端看到同一张图与同一事件历史 | Web 上传+引用，TUI 客户端读事件/读字节 | **PASS** |
| AC2 | kill/resume 后附件与引用不丢 | `kill -9` + 重启 + `resume` | **PASS** |
| AC3 | fork 会话能看到 fork 边界之前的图 | fork 后 child 读回 + 装配面 | **FAIL（真缺陷 D1）** |
| AC4 | compaction 后摘要留 refs、旧图可找回、事实不被删 | 真压缩 → 摘要/JSONL/回读 | **PASS** |
| AC5 | 事件流可定位附件引用且**无 base64** | 大图（512KB）后扫 JSONL | **PASS** |
| AC6 | 真机走查三端 | Web→真实视觉模型（**真跑 PASS**）/ TUI Windows Terminal（见 §9）/ CLI `--image` | AC6-TUI **PASS**、AC6-CLI **PASS**、AC6-VISION **PASS** |
| AC7 | 跨 session 读附件 404 授权断言在真实服务复验 | 真服务上打 4 种状态 | **PASS** |
| AC8 | 缺陷以票面+证据回报，不扩大范围 | 本文件 §5（D1/D2/D3）即其证据 | **PASS（过程项）** |

**原始读数（入库、字节可核）**：`docs/evidence/830-mm08-cross-client-local.json`（本机最终驱动全量 `evidence.json`，即上表逐条字段的来源，`counts={'PASS':8,'FAIL':1,'NOT_RUN':0}`、`elapsed_s=61.33`）、`docs/evidence/830-mm08-cross-client-local-verify.txt`（同轮 `verify.log`：逐条 `[PASS]/[FAIL]` 行 + 墙钟，审查 P4-2 要求墙钟可核）、`docs/evidence/830-mm08-cross-client-sandbox-verifier.txt`（沙箱驱动 stdout，**无常视觉凭证** ⇒ 7 PASS/1 FAIL/1 NOT_RUN；那份早于 AC6-VISION 接线，故与本地读数差这一条）。沙箱那次的 `evidence.json` 随沙箱销毁，但同批 `~/logs` 已整体取回本机（`/home/hatch/pytest-830/box-logs/logs.tgz`，391 KB）。

### AC1 — 跨端一致（PASS）

- 命令：`python scripts/verify_830_mm08.py --work /home/hatch/pytest-830/mm08-r3`（退出码 1，因 AC3 FAIL）
- `session_id=ffdd76e0-22fd-4c5d-967d-1b14096c8a16`；`attachment_id = sha256:0936d7fc…d1cae6`
- Web 侧：`POST /attachments` 200，`GET /attachments/<ref>/content` 200，读回 sha256 == 期望 sha256
- TUI 侧（真 `tui/src/api.ts`）：`getEvents` 17 条 → `tui_event_refs == ["sha256:0936d7fc…"]`、读回 `tui_read_status=200` 且 sha256 相同、`node_exit=0`
- **模型面**：替身收到的请求里图片块 = `{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,…","detail":"auto"}}`（`provider_request_image_block_count=1`）——即"同一张图"不是靠前端缓存，而是服务端事件 + 装配链一致。
- **诚实补充（观察项 O2）**：TUI 目前**没有**"回读历史附件字节并显示"的代码路径（`grep '`/content`' tui/src` 零命中），它只渲染 pending 图片。本 AC 的"B 端看到同一张图"在字节层是由 `ApiClient` 直接 `fetch` 受控读回端点证明的，**不是** TUI UI 显示链证明的。这条按发现登记，不算 PASS 的掩盖项。

### AC2 — crash / 重启 / resume（PASS）

- 重启前 `event_count=17`、`events.jsonl` 5689 字节、`event_ids` 集合不变、字节完全一致（`jsonl_bytes_stable=true`）
- `kill -9` 后：附件回读仍 200、sha256 不变
- **在途 run 被打断**：`POST /messages` 在途时杀进程（客户端拿到 `RemoteProtocolError`）→ 该用户消息**已落盘**（`in_flight_user_message_persisted=true`, `seq=18`），附件字节仍可取回
- `POST /resume`（body `{"task":"崩溃后继续"}`）→ 200，SSE 首帧为 `user/message(seq=24)`，`new_terminal_run_after_resume=true`，`event_count_after_resume=31`，回读仍 200/同 sha256
- **注**：`ResumeRequest`（`web/app.py:757`，`extra="forbid"`）没有 `attachments` 字段，所以 resume 只能带 `task` 不能重挂图 —— 这不影响本 AC（旧图引用本就在历史里），如实记录。

### AC3 — fork 边界复用（**FAIL，真缺陷 D1**）

- `parent_session_id=2f322e70-…`，带图用户轮 `seq=10`（`image_event_id=d12f4576-…`）
- fork：`POST /api/sessions/{id}/forks {"from_seq": 26}` → child `4ce291cb-…`，29 条事件
- child **继承了引用**：`child_refs=["sha256:a7c8e890…"]`、`child_image_event_present_by_id=true`
- child **取不回字节**：`GET /api/sessions/{child}/attachments/sha256:a7c8e890…/content` → **404**，body：
  `attachment 'sha256:a7c8e890…' 不在会话 '4ce291cb-…' 的命名空间里（不存在，或属于别的会话）`
- **装配面同样退化**：child 再跑一轮时，替身收到的请求里 `child_model_request_image_blocks=[]` 且出现 `"(image omitted: model does not support images)"` 占位符（`child_model_request_has_placeholder=true`）
- 边界对照：`from_seq=image_seq` 的 fork 自身不带该 ref（`boundary_refs=[]`）——即"图正好在边界"时 child 不继承引用这一侧是**正确**的；缺陷只在"边界之前已继承的引用解不开"。
- 现场：`child_artifact_dir_exists=false`，父对象在 `…/art/2f322e70-…/attachments/objects/` ⇒ 字节只存在**父 session 命名空间**下。

**根因**：`LocalArtifactStore` 按 `session_id` 分目录（`<artifact_dir>/<session_id>/attachments/objects/<sha[:2]>/<sha>`），而 fork 只复制 **workspace**（`src/agent_harness/session/fork.py::_copy_workspace`），不复制/不链接附件对象。于是 child 继承了指向"别的会话命名空间"的 ref ⇒ 受控读回 404 ⇒ `_load_image_payloads` 失败 ⇒ `multimodal._translate_block` 把块降级成占位符。

### AC4 — compaction 保留 refs（PASS）

- `POST /context/compact` → 200（`compact_attempts=2`，见观察项 O1），`{"source_seq_start":2,"source_seq_end":31,"tokens_before":1631,"tokens_after":636,"compacted_turn_count":4}`
- 摘要（`context/compacted`，schema `eight_section`）**含 ref**：`## 精确标识清单` 段列出 `sha256:1829930fe7…`；`summary_has_base64=false`
- 摘要请求的原始 transcript（发往 provider 的 sink）里**只出现 file_id、无 base64**（`summary_transcript_has_file_id=true`, `summary_transcript_has_base64=false`）
- **事实不被删**：压缩前的带图 `user/message`（`seq=10`）仍在，且 `pre_compaction_user_message_unmodified=true`（该事件的 `attachments[0].attachment_id` 仍是原 ref）；`jsonl_grew_only=true`（13210 → 15329 字节，只增不减）
- 压缩后旧图仍可取回：200 且 sha256 == 期望
- **探针自纠**：初版探针取 `user_rows[0]`（= 建会话那条任务消息，无 `attachments`），导致 `unmodified` 退化成恒假；已改为按 `ref in refs_of([row])` 选中**带图的那条**，并把 `pre_compaction_user_message_unmodified` 纳入 PASS 判据。

### AC5 — 事件流无 base64、大图不爆流（PASS）

- 512 KB 大图（`big_image_bytes=524345`；尺寸 320×240 PNG）上传引用后：
  - `events.jsonl` 5697 → 8410 字节（`+2713`，增长比 **0.005174**，即千分之五）
  - 事件数 17 → 25（`+8`，无逐块事件爆炸）
  - `jsonl_base64_run_count=0`（用 base64 字符长串正则扫全文件）
  - 附件元数据只记录 `{"attachment_id","media_type","bytes","width","height"}`；字节数 = 磁盘对象字节数（524345）
- 模型面收到的是 `data:` URL **仅存在于发往 provider 的载荷里**，不落 JSONL。

### AC6 — 真机走查（TUI PASS / CLI PASS / 视觉模型 PASS，真跑）

- **AC6-CLI（PASS）**：真 CLI 子进程（`python -m agent_harness.cli … --image …`）
  - 成功路径：`exit=0`，事件带引用 `sha256:0936d7fc…`，附件对象落在 CLI 会话命名空间
  - 失败路径三条全部 `exit=1` 且**零附件**：文件不存在、非图片字节（`bad.txt`）、扩展名与字节不符（`mismatched.png`）——stderr 分别是 `--image 文件不存在：…` / `不支持或无法识别的图片字节。`
- **AC6-TUI（PASS）**：真 `tui/src/api.ts::ApiClient` 上传 8×6 PNG（68 字节）→ 事件带引用 → 回读 200 / 68 字节 / `node_exit=0`
- **AC6-VISION（PASS，真跑）**：**原 NOT_RUN 作废**（#830 用户裁决：lighthouse `deepseek-ai/DeepSeek-V4.1-Flash` 本就是视觉模型，用现有线路、无需新凭证）。服务侧接线**全部走配置/环境变量**（`MM08_REAL_MODEL=1` + `MM08_MODEL_BASE_URL`/`MM08_MODEL_API_KEY`/`MM08_AGENT_MODELS`，key 从 `LIGHTHOUSE_API_KEY` 环境变量读入，**不写死**），**不打桩、不 mock**：
  - **做法**：Web 粘贴路径（`POST /attachments` 上传真实截图 → `POST /messages` 带附件引用）→ 真 provider（`https://lighthouse.dphn.ai/run/text-v/v1`，`deepseek-ai/DeepSeek-V4.1-Flash`）→ 读回答。
  - **判据**：截图（`screenshot_png()`，白底 + 绿横条 + 蓝圆 + 56px 粗体文字）里嵌了一条**只可能来自读图**的校验码 `830-KX74`；回答必须含它。答错即 FAIL。
  - **四件套证据**：① 运行 ID = `session_id=c62305a2-484a-4b83-ad8d-0750cc9b64a1`；② 命令 = `python scripts/verify_830_mm08.py --work /home/hatch/pytest-830/mm08-r3`（单跑同构入口：`--only ac6-vision`）；③ 退出码 = **1**（整轮退出码，含 D1 FAIL；AC6-VISION 自身 PASS）；④ 事件 JSONL 片段：
    ```json
    [
      {"seq":11,"type":"user/message","data":{"content":"读这张截图：找到「CODE:」后面的校验码（大写字母/数字/连字符），只回该校验码本身。",
        "attachments":[{"attachment_id":"sha256:1cfc8e6b…20bf9","media_type":"image/png","bytes":16263,"width":720,"height":260,"kind":"image"}]}},
      {"seq":16,"type":"model/completed","data":{"content":"830-KX74","model":"deepseek-ai/DeepSeek-V4.1-Flash",
        "usage":{"prompt_tokens":5137,"completion_tokens":50,"total_tokens":5187,"cached_tokens":4480},"finish_reason":"stop"}}
    ]
    ```
  - **读数**：`send_status=200`、`readback_status=200` 且 `readback_sha256 == attachment_sha256 == 1cfc8e6b…20bf9`（16 263 字节截图）、`answer="830-KX74"`、`answer_has_expected_code=true`、单条墙钟 `elapsed_s=10.55`。**回答内容即证据**：模型正确读出了图里的校验码。
  - **诚实边界**：这是**同代码路径的 API 层真跑**（Web 前端粘贴走的就是这两个端点），不是浏览器 GUI 点击；`mimo` preset 仍非本项线路，本项走的是用户指定的视觉线路。

### AC7 — 授权断言在真实服务复验（PASS）

同一把字节、四种读法（都在真服务上）：

| 场景 | 状态 | body |
| --- | --- | --- |
| 属主 session 读自己的附件 | **200** | 字节 + sha256 一致 |
| 跨 session 读（别的会话的附件） | **404** | `attachment '<ref>' 不在会话 '<other>' 的命名空间里（不存在，或属于别的会话）` |
| 不存在的 id（全 0 sha256） | **404** | 同上模板（把 `<ref>` 换掉后**逐字节相同**：`cross_body_identical_to_absent=true`） |
| 同会话但**未被任何事件引用**的上传物 | **404** | 同上模板（`cross_body_identical_to_unreferenced=true`） |
| 形状非法（`sha256:nothex`） | **422** | `attachment_id 必须形如 sha256:<64 位小写十六进制>：'sha256:nothex'` |

- 「不可区分」的判法是**模板级**：把各次请求里的 id 换成 `<id>` 后比较全文（否则 404 body 回显请求 id 会误判成泄漏）。404 模板回显请求 id 属请求侧已知信息，不是越权信息。
- 另外：同一把字节重复上传得到**同一个 id**（`reupload_same_bytes_yields_same_id=true`），且"只上传未被引用"不给读权限——即授权判据是**事件引用**而非"字节在不在盘上"。
- 对照成熟实现：DeepSeek Harness 的授权闸门同样是**纯事件引用谓词**（`packages/api/session-controller/src/commands.ts:405` → `:410 {reason:'ATTACHMENT_NOT_REFERENCED'}`，谓词在 `:682`），本仓行为与之同构。

### AC8 — 缺陷回报口径（PASS，过程项）

缺陷只登记不修，落在 §5，带现象/复现/证据/严重度/目标仓库；本票代码面**不含**任何产品代码改动（`git diff --stat origin/main..A″` 只有 3 个新文件：验证脚手架 2 个 + 近似验证 workflow 1 个）。

## 5. 缺陷清单

### D1 — fork 出的会话继承了附件引用，但取不回字节、图片被降级成占位符（AC3）

| 项 | 内容 |
| --- | --- |
| 现象 | fork 边界的**之前**已有图，child 继承了该 `attachment_id`，但受控读回 404；再对话时该图被降级成 `(image omitted: model does not support images)` |
| 复现 | `python scripts/verify_830_mm08.py --work <dir>` → AC3（也可手工：建会话 → 传图 → 引用 → fork from_seq=最后一条用户轮 → child 读 `/attachments/<ref>/content`） |
| 证据 | §4 AC3 全部字段；`child_read_status=404`、`child_artifact_dir_exists=false`、`child_model_request_image_blocks=[]`、`child_model_request_has_placeholder=true` |
| 严重度 | **P1**（票面目标之一"fork 出的会话仍能看到 fork 边界之前的图"不成立；且退化是**静默**的——事件流看着正常，只有模型侧看不到图） |
| 目标仓库 | intelligence-agent（后端；`session/fork.py` 的 workspace 复制面 + `LocalArtifactStore` 的 session 分目录约定） |
| 归属 | **NEEDS-USER-DECISION**：本票是验证票，只登记不修；修法涉及"fork 是否复制/链接附件对象"或"附件存储是否改为跨会话内容寻址"这条产品语义选择 |

**为什么这是缺陷而不是"设计如此"（成熟产品对照，≥2 家一手来源）**

| 来源 | 许可证 / 版本 / commit | 事实 | 对照结论 |
| --- | --- | --- | --- |
| **DeepSeek Harness**（`@deepseek-ai/dsh-root`） | MIT（Copyright (c) 2026 DeepSeek）· v0.2.1-alpha.1 · `5badb15009ae1756c3afe0ae0cef1faafc290ccc`（2026-10-03） | 归一化图片路径 `join(root,'objects',sha256[:2],sha256)`，其中 `root` 是 **`DSH_HOME/attachments/v1`（全局根，非 per-session）**（`packages/attachment/attachment-local/src/store.ts:47,51`；文件对象 `packages/attachment/attachment-local/src/file-store.ts:75,77`）。架构笔记（`.agents/notes/archived/architecture/2026-09-02-durable-image-offload.md`，2026-09-10 归档=已实现）明写：**"Resume, fork, and replay reproduce the surface from the log"** | DSH 的 fork/resume 能解开边界前的引用，是因为**字节在全局内容寻址根下、授权只看事件引用**。本仓 `LocalArtifactStore` 把 `session_id` 编进了路径 ⇒ 引用越界即解不开。**我们的 session 分目录是偏离** |
| **Cline** | Apache-2.0 · `cd80a20e96481f5f5d413789f6847accf846487b`（2026-10-06） | 压缩"持久化该 sidecar 而**不替换正典 transcript**，因此活动会话与其后的 resume 使用压缩后的工作上下文，而**已保存的消息保持完好**"（`apps/vscode/src/sdk/sdk-compaction.ts:8-11`；协调器 `apps/vscode/src/sdk/sdk-compaction-coordinator.ts:1-20`）。CLI 附件走 `ImagePasteAttachment{dataUrl,source}`（`apps/cli/src/utils/image-attachments.ts:45,49`、`apps/cli/src/tui/utils/image-paste.ts`） | 借来的是**判据**：跨生命周期断言必须落在"原记录仍在且未被改写"上 ⇒ 本票 AC4 直接照此写（`jsonl_grew_only` + 压缩前带图事件 `unmodified`） |

**修法方向（供决策，不在本票实施）**：(a) fork 时把被引用附件在 child 命名空间**落一份**（或硬链接/软链接）；(b) 附件存储改为**跨会话内容寻址**（DSH 式全局根）+ 授权仍按事件引用（本仓已有该闸门）；(c) child 读回时**回落到父命名空间**（需定义父链与权限边界，最容易出错）。三者都动产品语义。

### D2 — Windows：`LocalArtifactStore` 用**文本模式** `os.open` 发布二进制对象 ⇒ 含 `0x0A` 的图片字节被撑成 CRLF，读回哈希不符（GA 近似验证新发现）

| 项 | 内容 |
| --- | --- |
| 现象 | Windows 上 CLI `--image` 的**字节入站**必坏：`POST`/落盘看起来成功、事件引用也对，但**读回**该对象时 `KeyError: Blob artifact '<sha>' content hash mismatch (file modified or corrupted out-of-band)`；装配面随之打出 `WARNING 附件字节读取失败（sha256:…）——投影降级为占位符`（模型看不到图，**静默退化**） |
| 复现 | GA [#37941646937](https://github.com/EricKingWhy/intelligence-agent/actions/runs/37941646937) job `cli` 步骤 5：`uv run pytest tests/test_cli_image.py -q` → `2 failed, 14 passed, 1 skipped`（`[shot.png]` / `[shot.bin]` 两例都红）；本地 Linux 同一命令全绿 ⇒ **Windows 专属** |
| 证据 | ① 失败断言在 `tests/test_cli_image.py:201` 的 `await selection.store.load_bytes(attachment_id)`，异常抛在 `src/agent_harness/storage/local_artifact.py:260`；② 报错的 sha `71ceed261bd98059556ecf0e11ff21ceb281c793987dc5697ba3cec90211f769` **逐字等于** `sha256(png_bytes(4,3))`（本地实测复算一致）；③ 该载荷 68 字节、含 **2 个 `0x0A`**（PNG 签名 `\x89PNG\r\n\x1a\n`）；④ `_publish_blob`（`local_artifact.py:307`）用 `os.open(tmp, os.O_CREAT\|os.O_EXCL\|os.O_WRONLY, 0o600)`——**没有** `O_BINARY`；CPython 3.13 `Modules/posixmodule.c::os_open_impl` 在 `MS_WINDOWS` 下只补 `O_NOINHERIT`，flags 原样交给 CRT `_wopen`，而 CRT 默认**文本模式** ⇒ 写盘时 `0x0A → 0x0D 0x0A`。⑤ 本仓**自己**就有两处按同一理由加的 `getattr(os, "O_BINARY", 0)`（`context/project_instructions.py:388`、`skills/inspection.py:329`，`tests/context/test_project_instructions.py:229` 还断言了它）——即"os.open 在 Windows 要带 O_BINARY"是本仓已知纪律，唯独字节对象发布这一处漏了 |
| 严重度 | **P1**（数据损坏 + 静默降级：Windows 上一切含 `0x0A` 的附件字节都会被写坏；上传成功、事件正常，只有模型侧看不到图。JPG/WebP/GIF 同样中招——只要字节里有 `0x0A`） |
| 目标仓库 | intelligence-agent（后端；`src/agent_harness/storage/local_artifact.py::_publish_blob` 一行修复：`flags |= getattr(os, "O_BINARY", 0)`） |
| 归属 | **NEEDS-USER-DECISION**：本票是验证票，只登记不修。注意它的发现路径是"#830 的 Windows 近似验证"——**没有这次 push 触发的 runner，这条在 Linux 上永远看不见** |
| 诚实边界 | "CRLF 撑开"这条**机理**由 CPython 源码 + 本仓既有 O_BINARY 纪律推出（[INFERENCE] 级，未在 runner 上直接 dump 落盘字节）；但"读回哈希不符 + 载荷含 0x0A + 该处没带 O_BINARY + 仅 Windows 复现"是**实测事实**。补一条直接证据最省事的办法：在 runner 上 `python -c "import pathlib;print(pathlib.Path(rb'<对象路径>').read_bytes().count(b'\r\n'))"` |

### D3 — `tui` 全量套件在 Windows 上不可移植（6 例红；测试/实现的 POSIX 假设，非 Windows 语义错）

| 项 | 内容 |
| --- | --- |
| 现象 | GA `tui` job 步骤 5 `npm test` → **201 例 / 195 pass / 6 fail**（`npm test 退出码=1`）。Linux 上同一命令是 199/2（那 2 条正是要在 Windows 上过的，见上节） |
| 复现 | GA [#37941646937](https://github.com/EricKingWhy/intelligence-agent/actions/runs/37941646937) job `tui` 步骤 5；本地 `cd tui && npm test` 不复现（Linux 全过那 6 条） |
| 证据（6 例逐条根因） | ① `tui/test/app-images.test.ts:210`（AC2 括号粘贴）、② `:233`（AC4 `@path`）、③ `:481`（P3 上传窗口）、④ `:508`（N1 预检窗口）——四例同因：`const FIXTURE_PNG = new URL("./fixtures/shot.png", import.meta.url).pathname`；`URL.pathname` 在 Windows 上给 `/D:/a/.../shot.png`（**前导斜杠 + 正斜杠**），`statSync` 解不出来 ⇒ 图被判"读不到" ⇒ `pendingImages` 为空（`0 !== 1` / `0 !== 2` / `1 !== 2`）。修法：`fileURLToPath(new URL(...))`。⑤ `tui/test/image-paste.test.ts:60`（`file://` URL）：`resolvePastedImagePath(text, platform)` 的 `file://` 分支直接调 `fileURLToPath(raw)`——**吃运行时平台、不吃注入的 `platform` 参数**；Windows 上它对 `file:///tmp/shot.png` 抛错 ⇒ 返回 `undefined`，而断言写的是 Linux 期望 `/tmp/shot.png`。⑥ `tui/test/image-view.test.ts:83`（AC6 OSC 8）：占位文本断言含 `file:///home/u/shots/shot.png`，实现用运行时 `pathToFileURL` ⇒ Windows 上是 `file:///D:/…` |
| 严重度 | **P2**（不影响 Windows 上的产品语义——`AC6-W1`/`AC6-W2` 的 Windows 判定 17/17 全过、`tsc` 也过；但**全套件红**会让"Windows 上跑 tui 全量"这条车道永远红，遮住将来真正的 Windows 回归。属"测试面不可移植"而非"产品坏"） |
| 目标仓库 | intelligence-agent（`tui/test/*` 三处 + `tui/src/lib/image-paste.ts` 的 `platform` 传参一致性） |
| 归属 | **NEEDS-USER-DECISION**：本票只登记不修（改测试会动 #827 的判别力，超出验证票范围） |

## 6. 观察项（登记但**不算缺陷**）

- **O1 压缩 409**：`POST /context/compact` 在 run 终结收尾窗口会被 `is_busy` 判忙 → 409。驱动按真客户端行为重试（1s×最多 30 次），实测 `compact_attempts=2`。**设计内**，非缺陷。
- **O2 TUI 无历史图回读路径**：见 AC1 注。属 MM-06 范围的产品决策（TUI 是否显示历史图），本票只如实登记。
- **O3 沙箱 node 不支持 TS**：沙箱（Ubuntu 26.04 apt）的 `node v22.22.1` 编译时**未带** TypeScript 支持，`--experimental-transform-types` 直接抛 `ERR_NO_TYPESCRIPT`。换官方 nodejs.org 构建（同 v22.22.1）后正常。这是**沙箱环境差异**，非仓库问题；但记录在此，避免后人误判"TUI 探针在 CI 上挂了"。
- **O4 resume 不带 attachments**：见 AC2 注（`ResumeRequest` 无该字段）。
- **O5 fork 与「在写 progress 临时文件」的复制竞态（环境敏感，非 D1）**：本轮复跑中一次运行的 AC3 在 `POST /api/sessions/{id}/forks` 上抛 **500**，`srv.log` 的根因是 `shutil.Error: [... progress.meta.json._<rand>.tmp ...] No such file or directory`——`session/fork.py::_copy_workspace` 直接 `copytree` 工作区，与后台 progress 写入器留下的**临时文件**在同一时刻擦写 ⇒ scandir/copy 窗口失手。它**不是** D1（D1 是 fork 成功但 child 读图 404），只是同一条链路上一类**时序敏感**的稳健性问题；同批重跑即复现 D1 的结构化读数（`child_read_status=404`）。P3-2 的逐 AC 异常兜底保证这类异常只把该条 AC 记 FAIL（traceback 进 notes），不再丢整份 `evidence.json`。建议：fork 复制时忽略 `*.tmp`/写入中的文件，或在 fork 前后取一致快照。**本票只登记不修**。

## 7. 由本票登记的两条事项

1. **~~真实视觉模型凭证~~ → 已解除**（2026-10-09 用户裁决）：lighthouse `deepseek-ai/DeepSeek-V4.1-Flash` **本就是视觉模型**，用现有线路跑、**无需新凭证**。AC6-VISION 已由 NOT_RUN 转 **PASS**（§4 AC6 的四件套证据）；key 经 `LIGHTHOUSE_API_KEY` 环境变量注入，**不写死**、不改产品代码。
2. **Windows Terminal 真机**（本机是 Linux 云电脑）：走 GitHub Actions `windows-latest` 近似验证（置 `WT_SESSION` 走同代码路径）。**近似 ≠ 真机 GUI**，证据里必须如实标注；见 §9。

## 8. 云端沙箱

| 项 | 值 |
| --- | --- |
| 沙箱 | `shell/omp-830-mm08`（id `sbx_001m4gawxh16gcey076fg7envb0`） |
| 资源 | Ubuntu 26.04.1，2 vCPU，~3.9 GB RAM |
| 用途 | **DENY 姿态**：只跑验证命令 + 取回日志。**不**在箱内登录/启动任何 Agent，**不**在箱内改仓库代码，**不**做发布/安装/网络外呼业务动作（pypi/npm 拉依赖除外） |
| 账户级策略 | 只读核查过 `sbx --cloud policy ls` = `Default: deny-all / No allow/deny rules`；**未执行** `policy init`（会改账户级默认值并可能影响别人正在跑的沙箱）⇒ 本票的 DENY 是**构造性声明**（shell 沙箱 + 无 Agent 启动 + 无发布/安装动作；只有 pypi/npm 拉依赖） |
| 工具链差异 | apt node 无 TS 支持（O3）；`pnpm` 缺失需自装；`uv`/`python`/`git` 齐备 |

## 9. Windows 近似验证（`windows-latest`）

**交付物**：`.github/workflows/mm08-windows-verify.yml`（`on: workflow_dispatch` **+ 本分支 `push`**〔`branches: [omp/830-mm08-cross-client-verify]`；`paths` = 本 workflow 文件 + `scripts/verify_830_mm08.py` + `scripts/mm08_stub_server.py`——后两者是它在 runner 上真正消费的脚本，纳进来是为了让**证据脚本自身的修复**能在同一 runner 上复跑；docs-only 提交不触发，不烧 runner 分钟〕，两个 job：`tui` / `cli`，`runs-on: windows-latest`，action 全部 pin 到 commit SHA，`permissions: contents: read` + `actions: write`）。加 `push` 的唯一目的是**绕过 `workflow_dispatch` 要求 workflow 在默认分支**这条限制（本票不 push main）而真触发一次 Windows runner；设计上只做**同一代码路径的近似**。**本节的步骤清单与 workflow 文件逐条一致，两处互相引用——改一处必须同步改另一处**（本票审查 P3-1 就是二者漂移产生的）：
- Job `tui`（`working-directory: tui`，Node 22）：
  1. `npm ci --no-audit --no-fund`；
  2. **类型检查 `npm run check`（= `tsc --noEmit`，`include` 覆盖 `src/**` 与 `test/**`）**——win32 分支（`tui/src/lib/host.ts` 的 `%APPDATA%`/盘符/反斜杠路径）的类型面在这里取证（gate0 的 tsc 车道只扫 `web/`，`--experimental-transform-types` 只剥类型不做检查 ⇒ tui 类型面此前无车道覆盖）；日志 `01-tsc.log`；
  3. **AC6-W1**：置 `WT_SESSION` 的 `.ts` 断言脚本（`node --experimental-transform-types wt-session-placeholder.check.ts`）——`getCapabilities().images === null`、`renderDraftImage().kind === "text"`、文本**不含**图片协议转义（`\x1b_G` / `\x1b]1337;File=` / `\x1bP`）、保留 Windows 原始长路径、并按宿主 `isAbsolute` 判定 OSC 8 链接（另有绝对路径正向锚）；日志 `02-wt-session.log`；
  4. **AC6-W2**：`node --experimental-transform-types --test --test-reporter=spec test/host.test.ts`（Windows 路径约定；两条断言在 Linux 上必红）；日志 `03-windows-paths.log`；
  5. **全量用例 `npm test`**（= `node --experimental-transform-types --test --test-reporter=dot … test/*.test.ts`，含 AC6 占位/负向转义断言与 AC7 缩略图断言）；日志 `04-tui-full.log`；
  6. `if: always()` 上传 `mm08-tui` artifact（前置还有 `00-env.log`：node/npm/OS/`WT_SESSION` 未置位基线）。
- Job `cli`（`runs-on: windows-latest`，Python 3.13 + uv，`if: ${{ !inputs.tui_only }}`）：`uv sync --locked --python 3.13` → `scripts/verify_830_mm08.py --only ac6-cli`（**与本地同一套断言**，不另写一份）→ `uv run pytest tests/test_cli_image.py -q`（16 例，Windows `tmp_path` 下重跑）→ `if: always()` 上传 `mm08-windows-cli` artifact。
- 不写任何 secret；每条步骤都 `Tee-Object` 落 `$RUNNER_TEMP\mm08-tui` / `mm08-cli*`，`if: always()` 上传为 artifact。

**本机（Linux）已能给出的同源读数**：`tui` 套件 201 条中 199 通过，**2 条失败且都只可能在 Windows 上通过**——`tui/test/host.test.ts:215`（`路径约定：数据根与凭据文件是同一层`）与 `:225`（`python 解析`）：两者断言的是 Windows 路径形状（`path.join` 在 Linux 下产出 `/`）。**这两条在 Windows runner 上确已通过**（`AC6-W2` 步骤 17/17 pass）；但 Windows 上**另**暴露 6 条 POSIX 假设的失败（D3）——即"Linux 上只红这 2 条"**不能**反推"Windows 上就全绿"。`cd tui && npm run check`（= workflow 步骤 2 同命令）**exit 0**（Linux 上类型面即过；Windows 上同命令也 exit 0，win32 分支的类型面已由 runner 取证）。`tui/test/image-view.test.ts:68` 覆盖 `WT_SESSION`（`images:null`）⇒ 文本占位 + 负例转义序列 + 不得内嵌 base64；`:90` 覆盖「无 OSC 8 能力」时的文本路径。

**GA 触发结果 —— `on: push` 真触发两轮（run 链接 / 通过项 / 失败项 / 退出码）**

> 老读数（`workflow_dispatch` 路线）保留在下方小节，作为"为什么改成 push 触发"的决策依据。

| 项 | 第 1 轮 | 第 2 轮 |
| --- | --- | --- |
| run | [#37941349749](https://github.com/EricKingWhy/intelligence-agent/actions/runs/37941349749) | [#37941646937](https://github.com/EricKingWhy/intelligence-agent/actions/runs/37941646937) |
| head | `73e89f22` | `a52ab2bf` |
| 结论 | `failure`（两 job 均红） | `failure`（两 job 均红，**失败面已收敛到下面两条真发现**） |
| Job `tui` | 步骤 1-4 全 success（含 `npm run check` 与 AC6-W1/W2）；步骤 5 `npm test` **exit 1** | 同上；步骤 5 `npm test` **exit 1**（201 例 / 195 pass / 6 fail，见 D3） |
| Job `cli` | 步骤 4 `AC6-W3` **exit 1**：`UnicodeDecodeError: 'charmap' codec … byte 0x90` | 步骤 4 `AC6-W3` **exit 0**（`[PASS] AC6-CLI`，`counts={'PASS':1,'FAIL':0,'NOT_RUN':0}`）；步骤 5 `pytest tests/test_cli_image.py -q` **exit 1**（2 failed / 14 passed / 1 skipped，见 D2） |

**第 1 轮失败的是探针自己，不是产品**：CLI job 的 `AC6-W3` 死在 `subprocess` 的 reader 线程——子进程（真 CLI）按 UTF-8 写中文 stderr，父进程（驱动）按 runner 的 locale 默认编码 `cp1252` 读。已在提交 `fa99acc1` 修掉（两处 `subprocess.run` 显式 `encoding="utf-8", errors="replace"`，并给 CLI 子进程固定 `PYTHONIOENCODING=utf-8`），第 2 轮该步 **PASS**。顺带把 workflow 的 `paths` 扩到它真正消费的两个脚本，使证据脚本的修复能在同一 runner 上复跑。

**第 2 轮剩下的两个红都是真发现**（按验证票口径只登记不修，见 §5 的 D2 / D3）：
- **D2（产品真缺陷，P1）**：`AC6-W3` 的仓库自带用例在 Windows 上 2 例红，根因是 `LocalArtifactStore._publish_blob` 用**文本模式** `os.open` 写二进制对象 ⇒ 含 `0x0A` 的图片字节被撑成 CRLF，读回哈希不符。
- **D3（测试可移植性，P2）**：`tui` 全量 201 例里 **6 例红**，全是"测试/实现假设 POSIX 路径或运行时平台"（`.pathname`、硬编码 `/tmp`、`fileURLToPath` 不吃注入的 platform），不是 Windows 语义错。

**诚实边界（必须原样保留）**：这是**同一代码路径的 headless 近似验证**——runner 上置 `WT_SESSION` 走 `tui/src/lib/host.ts` 的真判定分支，命令与断言取自仓库真文件；它**不是**真机 GUI 验证（没有 Windows Terminal 的渲染器、没有真粘贴事件、没有真控制台），也**不覆盖**真机才有的输入法/剪贴板/终端渲染差异。

**老读数（`workflow_dispatch` 实测 404，保留为决策依据）**

- 分支已推：`omp/830-mm08-cross-client-verify`；workflow 文件**确实在分支上**（`gh api .../contents/.github/workflows/mm08-windows-verify.yml?ref=<branch>` → `sha=578aa72bea16783e4b9e0127232d2b78e2c1c8ae`）。
- 但 `gh workflow run mm08-windows-verify.yml --ref <branch>` → **HTTP 404 “not found on the default branch”**；REST `POST /actions/workflows/mm08-windows-verify.yml/dispatches` → 同样 404。`gh api .../actions/workflows` 列出的 6 个 workflow 里**没有它**。
- 结论：GitHub 的 `workflow_dispatch` 要求 workflow 文件存在于**默认分支**（本仓 = `main`）。本票边界是"不 push main / 不开 PR"⇒ **GA 近似验证无法触发**，AC6 的 Windows 部分按缺口登记（`NOT_RUN`），workflow 文件按交付物留在分支上，落到 `main` 后即可手工触发。
- 先例（可给用户决策当参照）：同仓 `#885` 的 `windows-installer-smoke.yml` 已在 `main` 上（`gh api .../contents/.github/workflows/windows-installer-smoke.yml` 可读到全文；引入提交 `d82b3ab8` "ci(#885): Windows installer smoke 跑在 GitHub Actions windows-latest"），并且**只挂 `workflow_dispatch`**——即"手工触发的 Windows 工作流可以常驻 main，不当常驻 CI"这条口径在本仓**已有先例**。

## 10. 门禁与套件读数

### 10.1 云端沙箱（Ubuntu 26.04.1 / 2 vCPU / ~3.9 GB；node 用官方 nodejs.org 构建，原因见 O3）

| 步骤 | 命令 | 退出码 | 读数 |
| --- | --- | --- | --- |
| 验证驱动（AC1-AC7） | `python scripts/verify_830_mm08.py --work ~/m8b` | 1（= AC3 FAIL） | `counts={'PASS': 7, 'FAIL': 1, 'NOT_RUN': 1}`，墙钟 20.4s；**判据与本机同形**（差的一份 `NOT_RUN` = AC6-VISION：沙箱无常视觉凭证，且那份脚本早于 AC6-VISION 接线；本机 keyed 视图为 8 PASS/1 FAIL/0 NOT_RUN，见 §4） |
| 验证驱动（单条） | `python scripts/verify_830_mm08.py --only ac6-cli --work ~/m8c` | 0 | 1 PASS |
| `tests/agent` … `tests/tools`（逐目录分块，含 `websearch` 38 passed、`workspace` 118 passed + 2 skipped） | `python -m pytest tests/<dir> -q --basetemp=~/bt-*` | 全 0 | 与基线一致，无新增红（第一轮分块 + 第二轮补跑，共 26 个目录） |
| `tests/memory` + `tests/evaluation` | 同上（**装齐 extras 后**） | 0 | **1198 passed**（先前 1 红 + 1 collection error 是 `uv sync --locked` 未装 `--all-extras` 的**环境假红**：缺 `langgraph` / `langfuse`） |
| 根级 `tests/test_*.py`（41 文件） | 同上 | 0 | **716 passed, 2 skipped**（两轮独立复现同一读数） |
| `tests/web` + `tests/transport` | 同上 | 0 | **859 passed**（185s） |
| `tests/tooling` | 同上 | 0 | **378 passed**（含 `test_review_coverage_immutable_ref.py`——它要求真台账干净，**所以必须在台账行提交之后跑**；A 树上它是 1 failed = 台账缺行，预期） |
| `tui` 全量 | `npm ci` + `npm test`（TAP 取计数） | 1 | **201 tests / 199 pass / 2 fail**，与本机逐字一致；两条失败 = `host.test.ts` 的 Windows 路径断言（Linux 上必红、Windows 上必须 0） |
| `tui` 类型 | `npm run check`（= `tsc --noEmit`，与 workflow 步骤 2 同命令） | 0 | 干净 |
| `web` 全量 | `pnpm install --frozen-lockfile` + `vitest run` | 0 | **112 files / 1575 tests 全过**，61.3s |
| Gate-0 裸全量（tip=脚手架提交 A″） | `python scripts/gate0.py` | 1 | **5/6**：diff-check / ruff / oxlint / tsc / guards PASS，**coverage FAIL**（A″ 未审查且未声明——台账行还没提交，预期） |
| Gate-0 裸全量（tip = 台账提交 B1 `1fc9bc54`） | 同上 | **0** | **6/6 PASS**，`tree=944ff04657ea`，墙钟 13.0s；读数落盘 `docs/gate/1fc9bc54b3540b291df5a51ff3fc08c730b99354.json` |

**注意（沙箱坑位，后人别踩）**：① 施工期宿主机重启过一次（`uptime` 归零、`ps -eo pid=,comm=` 在重启窗口里看不到用户进程），但 `setsid nohup` 起的后台长跑**没有死**——它一路把 summary 写到 `---- DONE ----`；**判存活请看日志/`summary.txt` 增长，别只看 `ps`**。② apt 的 `node` 编译时未带 TS 支持（O3），跑 TUI/驱动前必须把官方 node 放到 PATH 最前，否则 2 个 TUI 用例会以 `ERR_UNSUPPORTED_TYPESCRIPT_SYNTAX` 假红；③ `pnpm` 缺失需自装（本箱后来有 `/usr/local/bin/pnpm` 10.32.1）；④ `vitest` 5 已无 `--reporter=basic`（要用默认 reporter）；⑤ 日志目录 `~/logs` 里同时存在两轮任务的同名族文件（`52-web-vitest.log` 属第一轮、`56-web-vitest.log` 属第二轮）——**引用读数时必须按命令核对文件，不要按目录里最新的那个拿**。

### 10.2 本机（Linux 云电脑）

| 步骤 | 命令 | 读数 |
| --- | --- | --- |
| 验证驱动 | `python scripts/verify_830_mm08.py --work /home/hatch/pytest-830/mm08-r3` | `counts={'PASS': 8, 'FAIL': 1, 'NOT_RUN': 0}`，墙钟 61.3s；`evidence.json` 即 §4 的原始读数（入库 `docs/evidence/830-mm08-cross-client-local.json` + 同轮 `verify.log` → `…-verify.txt`） |
| 守卫（verification.map + 执行位） | `python -m pytest tests/test_verification_map.py tests/test_exec_bit_matches_shebang.py -q` | 32 passed |
| 台账覆盖（真判据：无 flag） | `python scripts/check_review_coverage.py` | exit 0 |
| Gate-0 裸全量 | `python scripts/gate0.py` | **4/6**：本机**没有** `web/node_modules` ⇒ oxlint/tsc 两条车道**阻塞**（不是失败面），其余 4 条 PASS；6/6 以沙箱读数为准（§10.1） |
| `tui` 全量 | `cd tui && npm ci && npm test` | 201 tests / 199 pass / 2 fail（同 §10.1 的两条 Windows 路径断言） |
| 验证驱动（单条，编码修复后复跑） | `python scripts/verify_830_mm08.py --only ac6-cli --work /home/hatch/pytest-830/mm08-cli-recheck` | `counts={'PASS': 1, 'FAIL': 0, 'NOT_RUN': 0}`，exit 0——证明 `fa99acc1` 的 `encoding="utf-8"` 改动在本机**无回归** |
| `tui` WT_SESSION 断言脚本（workflow 步骤 AC6-W1） | 从 workflow 里原样抽出 `.ts` 后 `WT_SESSION=mm08-local node --experimental-transform-types wt-check.ts` | **exit 0**；日志 `capabilities.images=null hyperlinks=true`、`kind=text`、原始 Windows 长路径未被截断、`link=false isAbsolute=false host=linux`、绝对路径正向锚 `absLink=ok path=/tmp/mm08/shot.png` |

## 11. 待用户决策事项

| # | 事项 | 选项 |
| --- | --- | --- |
| U1 | **D1（AC3 缺陷）修不修、怎么修** | (a) fork 复制/链接附件对象；(b) 附件改全局内容寻址（DSH 式）；(c) child 读回回落父命名空间。本票只登记 |
| U2 | ~~**AC6-VISION 凭证**~~ | **已闭环**（2026-10-09 用户裁决）：lighthouse `deepseek-ai/DeepSeek-V4.1-Flash` 即视觉线路，无需新凭证 ⇒ AC6-VISION 真跑 **PASS**（§4 AC6） |
| U3 | **Windows 真机走查** | 真机（有人有 Windows Terminal 的机器）走一次；或接受 §9 的 GA 近似并明确标注 |
| U4 | ~~**GA 触发路径**~~ | **已解决**（2026-10-09）：`workflow_dispatch` 要求 workflow 在**默认分支**（本票不 push main ⇒ `gh workflow run`/REST dispatch 双双 404）。改走 **`on: push`（仅本分支 `omp/830-mm08-cross-client-verify`；`paths` 盯本 workflow 文件 + 两个证据脚本）** 真触发一次 `windows-latest`。**实测结果见 §9「GA 触发结果」**。已保留 `workflow_dispatch` 供日后合入 main 后手工跑 |
| U5 | push/PR/merge 边界 | 本票只推了特性分支 `omp/830-mm08-cross-client-verify`（为触发 GA，用户 2026-10-09 批准）；未开 PR、未合 main |
| U6 | **D2 / D3 修不修、怎么修**（GA 近似验证新发现） | (a) D2 一行修（`flags \|= getattr(os, "O_BINARY", 0)`）+ 补一条 Windows 落盘字节直读的回归用例，D3 另开票改 6 条测试；(b) 全留观察项不修（Windows 附件字节仍是坏的）；(c) 只修 D2。本票只登记 |

## 12. 三问自检

1. **"跑过了吗？"** — 跑过。三套真实入口（HTTP / 真 TUI 客户端码 / 真 CLI 子进程）在真服务上跑，读数是退出码 + JSONL + 落盘 sha256 + provider 请求体。
2. **"跑的是我要的那份代码吗？"** — 是。`origin/main` 基线 `9c5bc1b4`；脚手架提交 `A″` 只新增 3 个文件（验证脚本 ×2 + workflow ×1），无产品代码改动；沙箱与 worktree 各自 `git rev-parse HEAD` 固定并写进日志首行。
3. **"读数有没有被我自己污染？"** — 唯一替身是模型 provider（打桩点即 PRD 指定的缝）；其余真跑。已发现并修掉两处**探针自身**的假读数（AC3/AC4 选错事件），修后 AC4 的 `unmodified` 从恒假变成真断言、AC3 的 `image_user_seq` 从 2 变成真实的 10。其余观察项（409、TUI 无历史图路径、沙箱 node 无 TS）均按"观察项 ≠ 缺陷"分开登记。**追加**：push 触发的两轮 GA 先抓出**探针自己**的编码 bug（已修 `fa99acc1`），再抓出 D2/D3 两条**真发现**——两轮都留了 run 链接与逐 step 退出码，可复核。

## 13. V3.1-lite 逐节合规

| 节 | 要求 | 本证据包 |
| --- | --- | --- |
| 执行信息 | 执行器 / 模型路线 / 降级声明 | §0（含"零降级"与子代理不可用的区分） |
| 启动检查表 | 五项 + 实际路径 + 章节 | §1 |
| 依赖核实 | blocked_by 机读核实 | §2（5/5 祖先） |
| 真实入口 | 真服务 + 真客户端，非单测 | §3 + §4（三入口） |
| 逐 AC 证据 | 命令 / 退出码 / 片段 | §4（每条含判据与读数；原始 JSON 在运行目录 `evidence.json`） |
| 缺陷回报 | 现象/复现/证据/严重度/目标仓库 | §5（D1 fork 读图 / D2 Windows 二进制发布被文本模式写坏 / D3 tui 套件不可移植） |
| 缺口 | 如实登记，不 mock | §7（两条，①AC6-VISION 已闭环为 PASS）+ §4 AC6 |
| 沙箱 | id / 资源 / 姿态 / 收尾 | §8 |
| 门禁 | 读数落盘 | §10 |
| 待决策 | 交回用户的判断项 | §11 |
| 审查 | 双轴独立审查 | **不在本票范围**（协调员阶段四） |

## 14. 落盘来源与可核性（#935 M-02 补录）

> 本节由 **#935**（MM 系列审计终审 · M-02）补录。**§0–§13 是 #830 执行时的证据包原文**（与分支 `omp/830-mm08-cross-client-verify` 上的版本内容一致），本节只补「这份证据从哪来、每条读数在哪可字节级核对」，**不改动任何读数**。#935 的症状是：本文件此前只落在 #830 特性分支、未合入 main ⇒ 在仓库内不可定位；本节 + 同批落盘的原始读数/驱动/工作流即为修复。

### 14.1 恢复来源（git 对象，可核）

- 分支 `omp/830-mm08-cross-client-verify`，tip `5c7cf23ad17af3d6326536b36a3c30c50d1317fa`（`git rev-parse` 实取，#830 终态）。
- #830 开工基线 `origin/main` = `9c5bc1b4044b899e705b5d2df527d4d6dfe03df1`。
- 本包正文、原始读数、驱动脚本、Windows 工作流均按该 tip 的 git 对象恢复（逐字节取自 git 对象，非重排）。

### 14.2 原始读数与工具（sha256，逐字可核）

逐字核法：`sha256sum <文件>`（完整值取前 8 + 后 7 位展示）。

| 文件 | 角色 | sha256 |
| --- | --- | --- |
| `docs/evidence/830-mm08-cross-client-local.json` | §4 逐条 AC 读数的机器原文（本机驱动全量 `evidence.json`） | `774bb4d4…375f474` |
| `docs/evidence/830-mm08-cross-client-local-verify.txt` | 同轮逐条 `[PASS]/[FAIL]` 行 + 墙钟 | `df0614bf…bb1d520` |
| `docs/evidence/830-mm08-cross-client-sandbox-verifier.txt` | 沙箱同形驱动 stdout（7 PASS/1 FAIL/1 NOT_RUN） | `4e95e7cf…be235f52` |
| `scripts/verify_830_mm08.py` | 验证驱动（真服务 + 真三端；§3 跑法） | `a1589c7e…810406b8` |
| `scripts/mm08_stub_server.py` | 唯一替身：聊天模型 provider 桩 | `9d14fb94…a5d6d0eb` |
| `.github/workflows/mm08-windows-verify.yml` | AC6-Windows 近似验证工作流（§9 的 run 来源） | `6ca5d8ff…3b935c57` |

### 14.3 逐条 AC 来源映射

散件目录 = `~/workspace/system/dispatch/`；`local.json` = 上表 `docs/evidence/830-mm08-cross-client-local.json`。

| AC | 原始读数出处（机器可核） | 散件佐证 |
| --- | --- | --- |
| AC1 | `local.json` `checks[id=AC1].evidence`（`web_send_status=200`、`tui_read_status=200`、`tui_node_exit=0`、`provider_request_image_block_count=1`）；`local-verify.txt:5` `[PASS] AC1` | `830-omp-run.log:25`（counts 汇总）；驱动与散件 `verify_830_mm08.py` 同文件 |
| AC2 | `checks[id=AC2]`（`jsonl_bytes_stable=true`、`in_flight_user_message_persisted=true`、`resume_status=200`、`event_count_after_resume=31`）；`local-verify.txt:6` | 同上 |
| AC3 | `checks[id=AC3].status="FAIL"`（`child_read_status=404`、`child_model_request_has_placeholder=true`、`child_artifact_dir_exists=false`）；`local-verify.txt:7-8` | 同上 |
| AC4 | `checks[id=AC4].evidence.compact_body.{tokens_before=1631,tokens_after=636}`、`checks[id=AC4].evidence.{summary_contains_ref=true,summary_has_base64=false,jsonl_grew_only=true}`；`local-verify.txt:9` | 同上 |
| AC5 | `checks[id=AC5]`（`big_image_bytes=524345`、`jsonl_growth_ratio=0.005174`、`jsonl_base64_run_count=0`）；`local-verify.txt:10` | 同上 |
| AC6-TUI | `checks[id=AC6-TUI]`（`read_status=200`、`read_bytes=68`、`node_exit=0`）；`local-verify.txt:11` | 同上 |
| AC6-CLI | `checks[id=AC6-CLI]`（`success_exit=0`、`missing_exit=1`、`non_image_exit=1`、`mismatched_ext_exit=1`）；`local-verify.txt:12` | 同上 |
| AC6-VISION | `checks[id=AC6-VISION]`（`session_id=c62305a2…`、`answer="830-KX74"`、`answer_has_expected_code=true`、`elapsed_s=10.55`）；`local-verify.txt:13` | 同上 |
| AC6-Windows（近似） | §9 的 GA run `#37941349749` / `#37941646937`（GitHub 公开可核）；工作流字节 sha256 见 14.2 | `830-d2d3-review-brief-correctness.md:41,61`、`830-d2d3-review-standards.md:79,130`、`830-d2d3-review2-correctness.md:16,72`（复核 run id 与其退出码/计数） |
| AC7 | `checks[id=AC7]`（`cross_session_status=404`、`malformed_status=422`、`cross_body_identical_to_absent=true`）；`local-verify.txt:14` | 同上 |
| AC8 | 过程项 = 本文件 §5 缺陷清单（D1/D2/D3） | `fix-mm08-d2-d3-dispatch.md`（D2/D3 另开修复票的派工单，引本包 §5）；D2/D3 的**修复**属另一票，不在 #830 读数内 |

**散件缺失如实标注**：AC1–AC7 的**逐条机器读数**不在散件目录内（散件含派工单/简报/审查记录/驱动脚本，不含 `evidence.json` 输出）；其权威原文即 14.2 的 `docs/evidence/830-mm08-cross-client-local.json`（现已随本文件落盘入库）。散件只提供**汇总读数**（`830-omp-run.log:25`：`counts={'PASS': 7, 'FAIL': 1, 'NOT_RUN': 1}`，沙箱轮）与 **Windows run 的复核**（`830-d2d3-review*`）。本机轮 `counts={'PASS': 8, 'FAIL': 1, 'NOT_RUN': 0}` 的原文见 14.2 `local-verify.txt:16`。

**范围边界**：D1（AC3 fork 读图）在本包中按验证票口径只登记不修（`NEEDS-USER-DECISION`）；D2/D3 的修复见另票 `fix-mm08-d2-d3-windows`（不在本包、不在 #935 范围）。
