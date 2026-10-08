# MM-02 · 模型真的看到图：事件引用、投影物化、Provider 载荷

**目标仓库**：intelligence-agent-backend（SessionEvent / derive / context / model adapter）。
**类型/优先级**：P0 Contract。**Parent**：#821。
**Blocked by**：#822（MM-01）。

## What to build

用户带一张图发一条消息，真实视觉模型能正确描述这张图（竖拍照片不躺倒）；会话事件流里**没有**
任何 base64；重启或重放后，从事件前缀重建出的请求与当初那次一致。

这是本特性的核心 tracer bullet：从 HTTP 入站一路切到模型请求装配。

## Acceptance criteria

- [ ] `user/message` 事件的 `data` **加法式**增加附件引用数组
      （`kind` / `attachment_id` / `media_type` / `bytes` / `width` / `height` / `name?`）；
      `content` 仍为 `str`。**不新增事件类型**，上传本身不产生事件。
- [ ] 字节先落存储再 append 事件（persist-before-event）：事件指向的字节必然可读。
- [ ] 事件流 / JSONL 中不出现 base64 图片载荷（以扫描断言证明，不是靠人工检查）。
- [ ] 发送端点（HTTP 与 WS 帧同形）接受附件 id 列表；校验 id 存在且属于本 Session 上下文。
- [ ] `derive_messages` 在**当前请求模型**支持视觉时把引用物化成图片内容块；请求装配层负责把标准内容块
      翻译成 provider 载荷（OpenAI 风格 `image_url` data URL + `detail`，默认 `auto`），
      由本仓 adapter 承担，不依赖 LangChain 代做请求方向翻译。
- [ ] 图片只出现在 user 消息（满足 DeepSeek 的 user-only 约束），并有测试固定这一点。
- [ ] EXIF 方向被正确应用；有 alpha → WebP，否则 JPEG（归一化语义移植 DSH `normalization.ts`，
      实现换成 Pillow，不引新依赖）。
- [ ] 无附件的纯文本消息行为**逐字不变**（既有 golden / 回归测试全绿）。
- [ ] 重放 / resume 后由事件前缀重建的请求与首次一致（图片内容块形状相同）。
- [ ] 用 scripted / fake provider 断言请求体中的图片块形状与 `detail`。
- [ ] 真实入口验证：真机发一张真实截图给真实视觉模型并得到正确回答；记录命令、响应与事件 JSONL 片段。

## 复用与来源

- DSH（MIT，commit `5badb150`）：persist-before-event 次序、`normalization.ts` 归一化语义、
  事件只带引用的领域模型。本仓与其同构（事件存引用、字节外置），故为契约移植而非新设计。
- oh-my-pi 的 `blob:sha256:` 外置方案是本设计的直接先例（`docs/research/...-research.md` §7）。
- 本仓 `session/derive.py`、`context/builder.py`、`model/` adapter 为 REUSE 扩展。

## 承接 MM-01 的必做项（补回「事件引用」授权闸门）

MM-01（#822）的受控读回端点（`GET /api/sessions/{id}/attachments/{aid}/content`）在**本票范围内**
把授权单位前移成**会话命名空间归属**（"上传即归属本会话"）——因为 MM-01 没有任何"事件引用附件"
的机制，字面执行 PRD D5 的"未被本 Session 事件引用 → 404"会与"上传即读回字节相等"这条 AC
结构上互斥（见 `web/attachments.py` 模块 docstring 的「授权口径」段）。

**本票必须补回「事件引用」授权闸门**：一旦 `user/message` 带上附件引用数组（见上面 AC 第 1 条），
受控读回（或投影 / 装配入口）必须**额外**校验该 `attachment_id` 被本 Session 的 `user/message`
事件真实引用；未被引用的 id 返回 404（DSH `ATTACHMENT_NOT_REFERENCED` 语义，即 PRD D5 原文）。
这是 MM-01 遗留、由本票闭合的契约缺口：MM-01 的测试 `test_never_uploaded_id_is_404` 只覆盖
"从未上传"，本票必须补"已上传但未被引用 → 404"的用例。

## 明确不做

上限聚合、非视觉降级与预算计入（MM-03）；任何客户端 UI；图片生成 / 图片编辑；
tool 结果图与 `view_image` 类工具（PRD Out of Scope）。
