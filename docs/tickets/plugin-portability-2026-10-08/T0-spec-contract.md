# [Plugin T0][Spec] 将跨市场移植 PRD 对齐 Engineering Specification 与 ADR

Part of #868

## What to build

在现有工程规格和 ADR 内明确安装、预检、项目/全局范围、逐项目启用、版本/回退、信任、Tool Runtime 与滑条复用的合同；不写产品代码。

## Acceptance criteria

- [x] PRD #868 的每项产品决策在现有规格中有明确条款；V1 不做 Marketplace 的旧决策与新阶段范围有清晰时间边界。
- [x] 明确标准 Skill 整目录、MCP tools、原生薄适配三种路径，以及‘完整兼容’的逐贡献判据和阻断条件。
- [x] 明确项目/全局同 ID 冲突、全局逐项目启用、待重启状态、固定 Git commit、升级回退和 OAuth-only 不兼容语义。
- [x] 逐项核对 Vision 的 Python Core、自有 Agent Runtime、统一 Tool/Permission/Ledger/SessionEvent 与 Optional 降级；#865 的 Composer 所有权不转移到本票。

## Verification

规格条款与 PRD 决策逐条对照；检查相关 ADR、Glossary 与 Phase 状态指针，确认无第二套主规格或历史进度改写。

## Blocked by

无

## Out of scope

不实现安装器、UI、MCP OAuth、原生 SPI；不重写已完成 Phase 的历史状态。

## 方案依据

[DSH 插件管理器](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/boot/plugin-manager/README.md) 的安装/版本边界；[Pi packages](https://github.com/earendil-works/pi/blob/1b347794e2a630e4359f2584f4eea388145d0ddf/packages/coding-agent/docs/packages.md) 的包与作用域。PORT DESIGN。

**可行性：9/10。**
