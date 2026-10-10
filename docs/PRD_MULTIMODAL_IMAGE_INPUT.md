# PRD：用户发送图片的多模态输入（Web / 桌面 / CLI+TUI 三端）

**状态**：用户方向已确认（2026-10-07），供拆票与实现。**日期**：2026-10-07。
**性质**：在冻结 Engineering Specification 之上新增的**输入能力**，不修改既有不变量、不改 Agent Loop、
不新增 Tool 执行路径。上游复用源由用户指定：**Web/桌面以 DeepSeek Harness（MIT）为主**，
**CLI/TUI 以 Pi（MIT）、oh-my-pi（MIT）、Claude Code（闭源，语义参考）、Cline（Apache-2.0）为主**。
调研与复制清单见 `docs/research/2026-10-07-multimodal-image-input-research.md`（下称「调研报告」）。

**开工前必须重新核对**：三个 clone 的实际分支/状态、各上游当前版本与 License、以及本文件引用的
本仓 file:line（会漂移）。

---

## Problem Statement

用户今天只能发文本。真实工作里大量问题天然是图片：UI 截图、报错弹窗、设计稿、架构图、
数据表格照片、白板照。用户被迫先把图转成文字描述，丢失细节、来回澄清，且 Agent 无法核对
「你说的和我看到的是否同一件事」。

三个入口都有同样缺陷，且互相不能替代：

- **Web**（React Inspector）：Composer 是纯 textarea，粘贴/拖拽/选图都没有落点；
- **桌面**（Electron 承载同一 React 页面）：同上；
- **CLI/TUI**：Python CLI 只有一次性子命令与纯文本位置参数；TS TUI 的输入编辑器无粘贴分支。

同时，本仓的既有不变量（`AGENTS.md` §7 #15「Artifact 大内容优先 Local/MinIO，模型只拿 summary + ref」、
spec 03 §3.1「事件只留 ref，正文不进事件流」）决定：图片**不能**像多数 CLI 产品那样把 base64
内联进会话 JSONL。

## Solution

给三个入口都加上「附图」能力，服务端作为唯一 Contract：

1. 用户附图（Web/TUI 粘贴、拖拽、选文件；桌面同上并额外支持「有真实路径的文件→`@path` 引用」；
   CLI `--image`）→ 字节**流式上传**到服务端附件存储（内容寻址、sha256、opaque id）；
2. `user/message` 事件只携带**引用块**（`artifact_id` + mime + size + 尺寸），`content` 仍是 str；
3. 发给模型前，按**当前请求模型的能力**把引用物化成图片内容块；模型不支持视觉时替换为
   明确文本占位符（不静默丢弃、不报错中断）；
4. 历史/重放/刷新后，图片从**受控端点**按 session 授权取回显示，不把 base64 塞回历史响应；
5. 入口侧前置门禁（无视觉模型时禁用/拒绝）与投影侧占位符构成双保险。

用户视角的结果：粘贴一张截图就能问「这个报错什么意思」，刷新/重启/换客户端后图还在，
换到不支持视觉的模型时界面明确告诉用户「图被省略了」，而不是悄悄发出去或悄悄丢掉。

---

## Seams（测试接缝；请 reviewer 确认后再开工）

**唯一新增接缝 = 服务端「用户轮次入站」边界**：
`POST /api/sessions/{id}/attachments`（流式上传）+ `POST /api/sessions/{id}/messages`（带附件引用）
+ `GET /api/sessions/{id}/attachments/{id}/content`（受控读取）。
测试在**进程内 FastAPI app + Fake/Stub ModelProvider + Local 附件存储**上做端到端断言：
事件形状、存储落点、`derive_messages` 投影（视觉/非视觉两条分支）、授权与上限。

**其余全部复用既有接缝，不新增**：

- Web 与桌面：既有 Playwright e2e（`web/e2e/*.spec.ts`）——桌面加载同一 React 应用，不另开接缝；
- TUI：既有 vitest + `tui/src/adapter.ts` 适配器接缝；
- Provider 载荷：既有 provider 测试替身（`model/scripted.py` 一族）。

不新增「图片专用」测试框架或第二条消息路径。

---

## User Stories

### Web

1. 作为 Web 用户，我想把剪贴板里的截图直接粘进 Composer，这样我不用先存文件再选文件。
2. 作为 Web 用户，我想把图片文件拖进页面任意位置完成附图，并有明确的拖拽遮罩反馈。
3. 作为 Web 用户，我想通过文件选择器一次选多张图，并能在发送前看到缩略图列表。
4. 作为 Web 用户，我想在发送前删掉某张已附的图，并看到剩余数量/大小预算。
5. 作为 Web 用户，我想在图片超出单张/单条上限时立刻看到明确原因（哪个文件、超了什么），而不是等发送失败。
6. 作为 Web 用户，我想在图片上传中看到进度与失败重试入口，且失败不阻塞文本发送。
7. 作为 Web 用户，我想在消息发出后看到自己发的图（按受控端点渲染），刷新页面后依然在。
8. 作为 Web 用户，我想点开图片看大图，并能复制/下载原图。
9. 作为 Web 用户，当所选模型不支持视觉时，我想在上传前就被明确告知并禁止附图（而不是发出后失败）。
10. 作为 Web 用户，我想在模型不支持视觉却已附图时看到「图已被省略」的明确标注，而不是静默丢失。
11. 作为 Web 用户，我想附图后仍能正常使用 queue/steer 与既有的暂停/恢复、预算展示。
12. 作为 Web 用户，我想在恢复一个历史会话（resume/replay/fork）时，旧图仍能正确显示与回溯。

### 桌面（Electron）

13. 作为桌面用户，我想用与 Web 完全一致的方式附图（同一套组件与快捷键），不必学第二套交互。
14. 作为桌面用户，我想把本地文件拖进窗口时，非图片文件自动变成 `@path` 引用（不上传字节），图片则走上传。
15. 作为桌面用户，我想确认渲染层没有获得任意文件系统读取能力（只能拿到我主动选择/拖入的文件路径）。
16. 作为桌面用户，我想在托盘/多窗口场景下附图行为与 Web 一致（同一服务、同一会话）。

### CLI / TUI

17. 作为 TUI 用户，我想用 `Alt+V`（Windows）/ `Ctrl+V` 粘贴剪贴板图片，并在编辑器里看到 `[Image #N]` 标记。
18. 作为 TUI 用户，我想粘贴图片文件的路径时被自动识别为附图，而不是当成一段文本发出去。
19. 作为 TUI 用户，我想把图片路径作为命令行参数（`@path` 风格）一次带入首轮消息。
20. 作为 WSL 用户，我想粘贴 Windows 侧截图也能工作（经 `powershell.exe` 取剪贴板）。
21. 作为 TUI 用户，我想在提交前按 `[Image #N]` 删除某张图，并知道删除后不会再上传。
22. 作为 TUI 用户，在 Windows Terminal 下我想看到明确的文本占位（文件名/尺寸）而不是期待中的缩略图——
    因为该终端不支持图片协议，系统不应对此撒谎。
23. 作为 CLI 用户，我想用 `--image <path>`（可重复）在一次性命令里附图。
24. 作为 CLI 用户，当图片路径不存在/不是支持的图片格式时，我想立刻收到明确错误，而不是发出坏消息。
25. 作为 CLI/TUI 用户，当模型不支持视觉时，我想在提交前就收到明确提示。

### 跨端与系统行为

26. 作为用户，我想在任何一端附图后，在另一端（换客户端）看到同一张图与同一事件历史。
27. 作为用户，我想在 crash/resume 后图片仍然可用（附件字节与事件引用都不丢）。
28. 作为用户，我想在 fork 出的新会话里仍能看到 fork 边界之前的图（按既有 Artifact Ref 复用语义）。
29. 作为用户，我想在 context compaction 之后仍能通过 artifact 引用找回旧图，摘要里保留 refs。
30. 作为用户，我想图片被计入 token/预算，不会因为「看不见的图」导致窗口被撑爆或预算失真。
31. 作为运维/审计者，我想在事件流里看到「这条用户消息带了哪些附件引用」，且事件流里**没有** base64。
32. 作为安全责任人，我想确认图片读取受 session 授权约束——别的会话的附件 id 拿不到内容。
33. 作为安全责任人，我想确认 MIME 是按**字节**判定的（不是信客户端声明），且超限在服务端权威强制。
34. 作为开发者，我想复制来的上游文件带来源与 License 标注（MIT/Apache-2.0），NOTICE 可审计。
35. 作为开发者，我想图片能力不影响既有纯文本链路（不带附件的消息行为逐字不变）。

---

## Implementation Decisions

### D1. 附件领域模型（移植 DSH `packages/attachment` 契约，MIT）

新增服务端附件域，契约取自 DSH（同构：事件存引用、字节外置）：

- `AttachmentId`：不透明内容寻址 id（`sha256:<hex>`），**永不**是路径或 bearer URL；
- `ImageAttachmentRef`：`attachment_id / media_type / bytes / width / height / name? / original_dimensions?`；
- `FileAttachmentRef`：`attachment_id / name / bytes`（通用文件只做 `@path`/引用，不进 provider）；
- `ImageAttachmentLimits`：`max_image_bytes / max_images_per_message / max_message_image_bytes /
  max_image_pixels / max_image_dimension / media_types`；默认沿用 DSH：20 MiB/图、20 张/消息、
  200 MiB/消息、64M 像素、8192px/边；归一化目标 2048² 像素 / 8192px / 4 MiB；
- `media_types` 冻结为 `image/png | image/jpeg | image/webp | image/gif`。

移植为 Python 类型（`NewType`/pydantic），**只移植契约与纯算法**，不移植任何 Cordis 绑定的类
（调研报告 §7.3-A 已列逐文件处置）。

### D2. 存储：ArtifactStore 二进制化 + 内容寻址

- 现有 `ArtifactStore` 是 UTF-8 文本假设（hash 基于 `content.encode("utf-8")`，三个 Provider 全部
  UTF-8 编解码）。本次**加法式扩展**：新增按字节的 `save_bytes/load_bytes/hash`，`mime_type` 参与
  元数据；既有文本路径行为不变。
- 落盘算法移植 DSH `attachment-local`：staging → fsync → 原子发布（hardlink 去重）→ 权限收紧 →
  目录 fsync；读时校验（尺寸/类型一致性）。文件名消毒沿用 DSH（含 Windows 保留设备名、255 字节截断）。
- Local Provider 先行；MinIO Provider 同步支持（既有 SDK 已在 Reuse Matrix）。**不引入对象存储之外
  的新基础设施**。
- 图片归一化（移植 DSH `normalization.ts` 语义，实现换成 Pillow）：EXIF 方向、8-bit sRGB、
  质量阶梯、有 alpha→WebP 否则 JPEG、像素/边长/字节上限。
- **不做自动清理**（与 workbench PRD §4.3 一致：首版不按天数清理，提供显式清理预览属后续票）。
  孤儿附件（上传后未发送）在 v1 保留，登记为已知行为。

### D3. 事件模型：引用进 `user/message`，不新增事件类型

- `user/message` 的 `data` **加法式**增加 `attachments: [ {kind:"image", attachment_id, media_type,
  bytes, width, height, name?} | {kind:"file", attachment_id, name, bytes} ]`；`content` 仍为 `str`。
- 依据 DSH 的 persist-before-event：字节先落存储，再 append 事件（避免事件指向不存在的字节）。
- **不为上传本身新增事件**（DSH 同构）：附件的对话事实就是「这条用户消息带了这些引用」；被放弃的
  上传不产生事件（孤儿字节见 D2）。既有 `artifact/created` 保持其 tool-overflow 语义不变
  （当前无生产发射点，本票不改它）。
- 所有把 `content` 当 str 的消费方（derive、memory v2、queue/steer、session service）必须保持兼容：
  附件是平行字段，不是 content 的替换。
- `derive_messages` 投影：`user/message` → `HumanMessage(content=[{type:"text"},{type:"image_url",...}])`
  仅当该次请求的模型支持视觉；否则 `content` 文本追加占位符
  `"(image omitted: model does not support images)"`（Pi 语义，逐字可核对）。
- token 估算（#935 / M-03 记录的实现口径，**取代** #824 的固定常量；单一事实源
  `context/tokens.py::image_tokens_for_size` 及其模块常量 docstring）：图片计入估算与
  预算/上下文压力，避免「图不计费导致 hard guard 失守」。本仓取**尺寸相关近似公式**
  （tile 制）`base + per_tile × ceil(w/tile) × ceil(h/tile)`，长边按归一化上限截断；
  尺寸来自标准图片块携带的 `width`/`height`（投影处即已知）。三家主流 Provider
  （OpenAI / Gemini / Anthropic）均为尺寸相关、无一家用固定常量；常数取 OpenAI
  `gpt-4o` 的 512px tile + 85/170（与本仓 OpenAI 风格 `image_url` 装配面最贴合）。
  真实 usage 仍以 Provider 回执为权威（`_usage_anchored_tokens` 的锚价只抬高估算）。
- compaction：摘要保留 artifact refs（spec 06 §5 已要求），旧图不因压缩被删除事实。

### D4. Provider 载荷

- 现有唯一适配器是 langchain-openai 的 `ChatOpenAI` 子类（Qwen/DeepSeek 只是 base_url 预设），
  载荷用 OpenAI 风格 `{type:"image_url", image_url:{url:"data:<mime>;base64,...", detail}}`；
  四家默认 Provider 方向（OpenAI/Qwen/DeepSeek/GLM）均已核实支持 data URL（调研报告 §2.3）。
- `detail` 默认 `auto`，可配置；per-model 缩放档（发送前 resize）沿用 DSH/Pi 的分档思路。
- **DeepSeek 约束**：图片只允许出现在 user 消息；本设计只往 user 消息附图，天然满足。
- `supports_vision` 从「死元数据」接入运行时：入口门禁 + 投影降级双保险（见 D6）。
- 请求装配处新增「标准内容块 → provider 载荷」这一层；LangChain core 不代做请求方向翻译
  （调研报告 §2.3 末条），由本仓 adapter 承担。

### D5. HTTP 契约

- **上传**：`POST /api/sessions/{id}/attachments`，`multipart/form-data`（或 octet-stream 流式），
  返回 `{attachment_id, media_type, bytes, width, height, name}`。该路由**必须**绕开既有
  `BODY_MAX_BYTES = 1 MiB` 的 JSON body 上限（用流式请求体），**不**采用 DSH 的 300 MiB 放宽方案；
  1 MiB 上限对既有 JSON 端点保持不变。
- **发送**：`POST /api/sessions/{id}/messages` 增加可选 `attachments: [attachment_id, ...]`；
  WS 帧同形。服务端校验：id 存在、属于本 session 上下文、未超数量上限、模型支持视觉（否则 422）。
- **读取**：`GET /api/sessions/{id}/attachments/{attachment_id}/content`，返回原始字节 +
  `Content-Type`；**先证明该 id 被本 Session 的事件引用**，否则 404（DSH
  `ATTACHMENT_NOT_REFERENCED` 语义）。绝不返回可猜测的路径/裸 URL。
- **CLI/TUI 侧**：复用同一三端点（TUI 走 HTTP，CLI 走同一 API 客户端）。
- 错误码沿用既有口径：形状/上限/能力不符 → 422；不存在/未引用 → 404；超大小 → 413。

### D6. 门禁与降级（双保险）

- **入口**：Web/桌面在所选模型 `supports_vision=false` 时禁用附图入口并给出原因；TUI/CLI 在提交前
  拒绝并报错（Aider 语义）。服务端在发送端点做权威校验（422），不信任客户端。
- **投影**：即便入口被绕过（例如发送后 fallback 到非视觉模型），`derive_messages` 按**当前请求模型**
  替换为文本占位符。这一层是「fallback 自动降级」的机制保证。
- 占位符文案固定且可测试；不得静默丢弃。

### D7. 前端（Web + 桌面共用）

- 复制/改写 DSH（MIT，逐文件头标注上游文件 + commit `5badb150` + License）：
  `drop-events`、`DropOverlay`（可整文件 COPY）；`AttachmentRail`、`FileCard`、`MessageImage`、
  `MessageImages`、`ComposerAttachments`（ADAPT：图标/CSS/lightbox 换成目标既有原语，如
  `@radix-ui/react-dialog`）；`service.ts`/`historical-images.ts` 的编排**重写**为本仓 hook
  （剥离 Cordis）。
- 粘贴取文件逻辑抽成独立纯函数（供 Composer 与桌面共用）。
- 新增 `web/THIRD_PARTY_NOTICES.md`（对齐 `desktop/THIRD_PARTY_NOTICES.md` 惯例）。
- 目标 React 19 与 DSH React 18.2 差异不构成阻碍（所涉组件只用通用 hook + `createPortal`）。
- CSP `img-src 'self' data:` 已放行；显示走受控端点（`'self'`），不引入外域。

### D8. 桌面特有：host 路径桥

- 在 preload 新增一个**窄**桥（对齐 DSH `__DSH_HOST_PATHS__`）：仅对「用户主动选择/拖入的 File」
  返回其真实路径（`webUtils.getPathForFile`），不提供任意 fs 读取。
- 分流规则：有真实路径且**非图片** → 生成 `@path` 引用（不上传字节）；图片或粘贴字节 → 走上传。
- 该桥是一次渲染层能力面扩展，**必须**有独立的安全审查（sandbox/contextIsolation 现状不变）。
- 目录选择等既有能力不变，不新增。

### D9. TUI（复用既有 pi-tui，不新增 TUI 库）

- 目标 TUI 已依赖 `@earendil-works/pi-tui`，其自带终端图片协议层、`Image` 组件与
  `getNativeClipboard()`（含 win32 预编译）——**REUSE，不复制**。
- COPY（MIT，来自 Pi）：magic-bytes MIME 探测、剪贴板读取、剪贴板命令、WSL 辅助（纯函数，零依赖）。
- COPY/ADAPT（Apache-2.0，来自 Cline）：`image-paste.ts`（平台分支）与 data-url 构造（换掉
  OpenTUI 的 `PasteEvent` 类型与 1 个 `@cline/shared` import）；新增 `tui/THIRD_PARTY_NOTICES.md`。
- 键位：Windows `Alt+V`（`Ctrl+V` 常被终端截获），WSL 双绑；WSL 取图必须经 `powershell.exe`。
- 交互：编辑器内 `[Image #N]` 标记 + 待发图片数组（PORT DESIGN 自 oh-my-pi/Cline/Codex 的共同语义）；
  删除标记即撤销该图。
- **Windows Terminal 不承诺缩略图**：pi-tui 在 `WT_SESSION` 下返回 `images: null`；退化为
  文本占位（文件名 + 尺寸）+ 可打开的原图路径。UI 不得显示假缩略图。

### D10. CLI（Python）

- 新增 `--image <path>`（`action="append"`，可多张），与既有 argparse 子命令风格一致。
- 读文件 → magic-bytes MIME 探测（标准库签名表，不引新依赖）→ 上传 → 消息事件带引用。
- 错误：路径不存在/非支持格式/超限 → 明确错误且不发消息。

### D11. 配置

新增配置键（默认值取 DSH 一组）：上传大小/单消息数量/单消息总字节/像素与边长上限/允许的
media types/发送前缩放档/`detail` 默认值。所有上限服务端权威，前端预检镜像同一份值（沿用
Open WebUI/LibreChat 的「后端为权威并把配置下发前端」模式）。

### D12. 归属与 License

- 每个 COPY/ADAPT 文件加文件头：上游仓库 + 文件 + commit + License；MIT（DSH/Pi/oh-my-pi）
  与 Apache-2.0（Cline）分别保留许可文本与 NOTICE。
- **不复制**：任何 Cordis/DSH Runtime、`session-format-*`、`llm-*` adapter、`connection/*`；
  oh-my-pi 的 `@oh-my-pi/pi-tui` 整包；Cline 的 OpenTUI React 组件；codex 的 Rust 实现。
- 不接入任何上游的 Agent Loop。

---

## Testing Decisions

**好测试的标准**：只断言外部可观察行为（事件形状、HTTP 响应、投影结果、渲染出的 DOM），
不断言内部实现细节（私有函数、字段顺序、具体存储路径）。每条上限/降级都必须有**失败路径**测试。

- **服务端（主接缝，pytest）**：
  - 上传→发送→事件形状：事件里只有引用、**没有** base64；字节可从存储回读；
  - `derive_messages` 两条分支：视觉模型→图片内容块；非视觉模型→文本占位符（逐字）；
  - 授权：跨 session 的 attachment_id 读取返回 404；未发送（未被事件引用）的 id 读取 404；
  - 上限：超单张/超数量/超总字节 → 413/422，且不落任何事件、不落存储残留；
  - MIME：声明与字节不符（伪扩展名）→ 拒绝（按 magic bytes 判定）；
  - 流式上传不触发 1 MiB JSON body 上限；既有 JSON 端点上限行为不变；
  - 无附件的纯文本消息行为**逐字不变**（既有 golden/回归测试全绿）。
  - 前例：既有 `tests/` 的 session/event、context、web 端点测试族与 Fake provider 替身。
- **Web/桌面（既有 Playwright e2e）**：粘贴/拖拽/选择→缩略图→发送→消息里渲染图→刷新后仍在；
  超限提示；非视觉模型的禁用态。桌面复用同一 spec（同一 React 应用）。
- **TUI（既有 vitest）**：经 adapter 接缝断言「附图提交后请求体带引用」；粘贴→`[Image #N]` 标记→
  删除标记→提交内容不含该图；Windows 占位渲染不出现假缩略图。
- **Provider 载荷**：用 scripted/fake provider 断言请求体中的图片内容块形状与 `detail`。
- **真实入口验证（Runtime Verification 阶段）**：至少一次真机走查——Web 粘贴一张真实截图发给
  真实视觉模型并得到正确回答；TUI 在 Windows Terminal 下走一次（占位路径）；CLI 一次 `--image`。
  记录证据（命令/响应/事件 JSONL 片段）。

---

## Out of Scope

- 图片**生成**（image generation）、图片编辑；
- **tool 结果图**（如 sandbox 截图回喂模型）与 `view_image` 类工具；
- 视频/音频/PDF 文档的多模态输入（`documents` block）；
- OCR/图像理解管线、Knowledge/RAG 的图片摄取；
- Windows Terminal 的图片渲染（需 SIXEL，pi-tui 不支持）；
- TUI 的拖拽事件（终端里拖拽即路径粘贴，已在范围内）；
- 孤儿附件自动清理与空间占用界面（与 workbench PRD §4.3 一致，后续票）；
- 跨 session 共享附件、公开分享链接（LobeChat 式无鉴权路由明确不做）；
- 远程/公网访问附件（服务仍只监听 loopback）；
- 多人协作场景下的附件权限模型。

---

## Further Notes

1. **与 CLI 主流的刻意偏离**：Claude Code/Codex/Gemini CLI/Pi 都把 base64 内联进会话文件。
   本仓不采用——事件流是事实源、Artifact 层已有投资，且不变量 #15 明确要求大内容外置。
   oh-my-pi 的 `blob:sha256:` 外置方案与本仓同构，是本设计的直接先例。
2. **与 DSH 的唯一硬冲突**：DSH 把图片 base64 塞进 JSON prompt（默认 body 上限 300 MiB 且启动断言），
   本仓 1 MiB 上限不允许。本方案选择**流式上传 + prompt 只带引用**，既不放松既有上限，也保留
   DSH 的领域模型与前端组件复用。这是本 PRD 相对 DSH 的最重要设计偏离，必须在审查时重点复核。
3. **fork 与 compaction**：两者都要求图片在边界之后仍可解释。依赖既有 Artifact Ref 复用语义
   （spec 03 §7）与摘要保留 artifact refs（spec 06 §5），本 PRD 不新造机制。
4. **可观察性**：附件相关事实（上传、引用、省略降级）都要能在 SessionEvent/JSONL 中定位；
   诊断日志与事件分层不变（不变量 #4）。
5. **上游漂移**：所有上游引用登记在调研报告（含 commit）；开工时按 `reference-sources.md` 纪律
   重新核对版本与 License。`reference-sources.md` 的「多模态图片输入 / 附件」领域条目与本地克隆
   （deepseek-harness / oh-my-pi / codex）已补录，并披露了 §3.1「先补清单再调研」的事后补录偏差。
