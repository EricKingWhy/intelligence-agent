# MM-04 · Web：附图 Composer 与消息内图片渲染

**目标仓库**：intelligence-agent-frontend（React，`web/`）。
**类型/优先级**：P1。**Parent**：#821。
**Blocked by**：#822（MM-01）、#823（MM-02）、#824（MM-03）。

## What to build

Web 用户把剪贴板里的截图直接粘进 Composer、把图片文件拖进页面、或用文件选择器一次选多张；
发送前看到缩略图列表并能删掉某张；发送后在对话里看到自己发的图，刷新页面后图还在；
超限时立刻看到「哪个文件、超了什么」；所选模型不支持视觉时附图入口被禁用并说明原因。

## Acceptance criteria

- [ ] 粘贴（剪贴板文件项）、拖拽（含拖拽遮罩反馈）、文件选择器三条路径都能附图，一次可选多张。
- [ ] 发送前缩略图列表 + 单张删除 + 剩余数量/大小预算提示。
- [ ] 超限就地显示明确原因（哪个文件、超了什么），不等发送失败。
- [ ] 上传中显示进度与失败重试入口；**上传失败不阻塞纯文本发送**。
- [ ] 消息发出后按受控端点渲染图片；刷新页面 / 重新进入会话后仍能显示（历史来自事件引用）。
- [ ] 点开可看大图，并能复制 / 下载原图。
- [ ] 所选模型 `supports_vision=false` 时入口禁用并给出原因；已附图但模型降级时显示「图已被省略」标注。
- [ ] 附图不影响既有 queue / steer、暂停恢复、预算展示。
- [ ] CSP 保持 `img-src 'self' data:`，不引入外域。
- [ ] 粘贴取文件逻辑抽成独立纯函数，供桌面复用。
- [ ] 复制 / 改写的上游文件带文件头（上游仓库 + 文件 + commit + License），新增 `web/THIRD_PARTY_NOTICES.md`
      （对齐既有 `desktop/THIRD_PARTY_NOTICES.md` 惯例）。
- [ ] Playwright e2e 覆盖：粘贴 / 拖拽 / 选择 → 缩略图 → 发送 → 渲染 → 刷新后仍在；超限提示；非视觉禁用态。

## 复用与来源

- **整文件 COPY**（DSH，MIT，commit `5badb150`）：`drop-events`、`DropOverlay`。
- **ADAPT**（同上）：`AttachmentRail`、`FileCard`、`MessageImage`、`MessageImages`、`ComposerAttachments`
  —— 图标 / CSS / lightbox 换成本仓既有原语（如 `@radix-ui/react-dialog`）。
- **重写**（同上的编排）：`service.ts`、`historical-images.ts` 的流程改成本仓 hook，剥离 Cordis 绑定。
- 目标 React 19 与 DSH React 18.2 的差异不构成阻碍（所涉组件只用通用 hook + `createPortal`）。

## 明确不做

桌面宿主路径桥（MM-05）；任何后端改动；图片编辑 / 生成。
