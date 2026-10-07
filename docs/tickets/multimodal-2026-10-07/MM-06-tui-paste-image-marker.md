# MM-06 · TUI：粘贴图片、路径识别、`[Image #N]` 与终端降级

**目标仓库**：intelligence-agent-frontend（TypeScript TUI，`tui/`）。
**类型/优先级**：P1。**Parent**：#821。
**Blocked by**：#822（MM-01）、#823（MM-02）、#824（MM-03）。

## What to build

TUI 用户用 `Alt+V` 粘贴剪贴板图片、粘贴图片路径时被自动识别为附图（而不是当成一段文本发出去）、
编辑器里出现 `[Image #N]` 标记、删掉标记即撤销该图；提交后请求体带引用；
在 Windows Terminal 下看到的是明确的文本占位（文件名 + 尺寸），**不是**假缩略图。

## Acceptance criteria

- [ ] `Alt+V`（Windows）粘贴剪贴板图片；WSL 双绑并经 `powershell.exe` 取 Windows 侧剪贴板。
- [ ] 粘贴的图片文件路径被自动识别为附图，不当成文本发出。
- [ ] 编辑器内 `[Image #N]` 标记 + 待发图片数组；删除标记即撤销该图，提交内容不含它。
- [ ] 图片路径可作为命令行参数（`@path` 风格）一次带入首轮消息。
- [ ] 经既有 adapter 接缝（vitest）断言：附图提交后请求体带引用。
- [ ] Windows Terminal（`WT_SESSION`）下渲染为文本占位 + 可打开的原图路径；**不出现假缩略图**。
- [ ] 支持终端图片协议的终端下按既有 pi-tui 能力渲染缩略图。
- [ ] 模型不支持视觉时在提交前明确提示 / 拒绝。
- [ ] 复制 / 改写文件带文件头与 `tui/THIRD_PARTY_NOTICES.md`。
- [ ] **不新增 TUI 库，不接入任何上游 Agent Loop。**

## 复用与来源

- **REUSE（不复制）**：既有依赖 `@earendil-works/pi-tui`（MIT）——终端图片协议层、`Image` 组件、
  `getNativeClipboard()`（含 win32 预编译）。
- **COPY**（Pi，MIT）：magic-bytes MIME 探测、剪贴板读取、剪贴板命令、WSL 辅助（纯函数，零依赖）。
- **COPY / ADAPT**（Cline，Apache-2.0）：`image-paste.ts` 的平台分支与 data-url 构造
  （换掉 OpenTUI 的 `PasteEvent` 类型与 1 个 `@cline/shared` import）。
- **PORT DESIGN**（oh-my-pi / Cline / Codex 的共同语义）：编辑器内标记 + 待发数组的交互。

## 明确不做

Windows Terminal 的图片渲染（需 SIXEL，pi-tui 不支持）；TUI 拖拽事件（终端里拖拽即路径粘贴）；
oh-my-pi 的 `@oh-my-pi/pi-tui` 整包；Cline 的 OpenTUI React 组件。
