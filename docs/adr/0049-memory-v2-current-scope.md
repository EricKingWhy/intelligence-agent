# ADR-0049 — Memory V2 当前版本的作用范围

- **状态**：已接受
- **日期**：2026-09-30
- **关联**：Memory V2 #296；PRD §4.3、§6.1；Engineering Spec 06 §7
- **决策人**：用户明确批准

## 1. 背景

Engineering Spec 06 §7 原要求至少支持 user、project、task、agent 四种抽象 scope。Memory V2 PRD §4.3 与 §6.1、当前 schema 和 provider 只定义 user/project。按项目文档优先级，这一差异使 #296 的范围无法从现有实现推断。

## 2. 决策

Phase 6 的 Memory V2 只支持 user（PRD 中命名为 user_global）和 project 两种 scope。task 与 agent scope 延后到后续经批准的阶段；本版本不得创建、查询或注入这两种 scope 的记忆。

增加 task 或 agent scope 前，必须通过单独批准的 ticket 定义可信身份来源、生命周期、隔离、权限与召回可见性，并同步 PRD、schema、API 和验收测试。

## 3. 影响

- 当前 Memory V2 PRD 的作用范围与 Engineering Spec 06 §7 一致。
- 本决策不改变当前 schema、provider、存储结构或 API。
- #296 不得因当前版本未实现 task/agent scope 判为缺陷；其余父票 AC 仍需逐项通过。
- 后续 scope 扩展需要新的产品批准，不由本 ADR 自动授权。
