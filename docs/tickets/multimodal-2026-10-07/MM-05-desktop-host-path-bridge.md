# MM-05 · 桌面：宿主路径桥与拖入分流

**目标仓库**：intelligence-agent-frontend（Electron，`desktop/`）。
**类型/优先级**：P1（含安全审查）。**Parent**：#821。
**Blocked by**：#825（MM-04）。

## What to build

桌面用户把本地文件拖进窗口时，图片走上传、非图片自动变成 `@path` 引用（不上传字节）；
附图交互与 Web 完全一致（同一套组件与快捷键）；渲染层拿不到任意文件系统读取能力。

## Acceptance criteria

- [ ] preload 新增一个**窄**桥：只对「用户主动选择 / 拖入的 File」返回其真实路径，
      不提供任意 fs 读取（对齐 DSH `__DSH_HOST_PATHS__` 的收窄语义）。
- [ ] 分流规则：有真实路径且**非图片** → 生成 `@path` 引用；图片或剪贴板字节 → 走上传。
- [ ] `sandbox` / `contextIsolation` 现状不变；渲染层无法通过该桥读取未主动选择的文件。
- [ ] 桌面加载的同一 React 应用在附图交互上与 Web 完全一致（同一份组件与快捷键）。
- [ ] **独立安全审查完成并记录**：该桥是渲染层能力面扩展，审查须给出结论与残余风险，不能由实现者自证。
- [ ] 托盘 / 多窗口场景下附图行为一致（同一服务、同一会话）。
- [ ] 复用同一份 Playwright spec（桌面加载同一 React 应用，不另开测试接缝）。
- [ ] 新增 / 更新桌面侧 NOTICE 归属（既有 `desktop/THIRD_PARTY_NOTICES.md` 惯例）。

## 复用与来源

- DSH desktop（MIT，commit `5badb150`）的 host-path 桥语义与分流规则。
- 既有 `desktop/THIRD_PARTY_NOTICES.md` 与逐文件头归属惯例（本仓已有 DSH 适配先例）。

## 明确不做

Electron 生命周期 / 单实例 / 托盘 / 更新器改动（属既有 workbench 票）；
任意文件系统读取能力；目录选择等既有能力的扩大。
