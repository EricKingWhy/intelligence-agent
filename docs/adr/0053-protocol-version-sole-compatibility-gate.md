# ADR-0053 — protocol_version 是 shell / service 之间唯一的兼容性判据

**Status**: Accepted（#914 用户授权裁决 D2，2026-10-11）
**Date**: 2026-10-12
**Related**: #914（W-21 / W-16 R2）；`src/agent_harness/host_service.py:95-101`；`desktop/src/host-protocol.ts:9-19`；`desktop/test/host-protocol.test.ts`；ADR-0032（服务复用同一坐标）
**真实证据**: `desktop/test/host-protocol.test.ts` 5 passed（含单端 bump 消融：把 shell 常量改成 2 后 parity 用例实测红）；`tests/host_service/test_host_service.py::test_attach_probe_stale_on_protocol_mismatch`（服务端既有读数）

---

## Context

1. **症状与裁决**：#914 的 W-16 R2 分支追问「更新后旧服务继续应答新客户端」是否仍是缺陷。用户授权裁决（方向 3）判定：**不是**——只要 `protocol_version` 未变，旧服务与新客户端的组合属于设计内行为，不该用产品版本/构建号去识别；真正的根因是缺少一条「破坏协议必须 bump `protocol_version`」的规约和钉子。
2. **当时的实际状态**：两端各有一份手写常量（Python `HOST_PROTOCOL_VERSION`、TypeScript `HOST_PROTOCOL_VERSION`），注释写着「必须相等」，但**没有任何测试**钉住它们；兼容性判据本身（服务端 `attach_probe`、客户端 `classifyHealth`）已经只看 `protocol_version`，产品版本字符串只被当作信息透传。
3. **风险**：常量漂移（一端 bump、另一端忘）会让「可附着」判断凭虚假的相等成立；而「破坏协议不 bump」会让客户端在协议已变的情况下按同一版本号继续复用旧服务——这正是 R2 描述的现象。

## Decision

1. `protocol_version` 是 shell 与 service 之间**唯一**的兼容性判据；产品版本 / 构建号**永不**参与判据。
2. 破坏协议的变更（改名、删除、既有字段或路由的语义变化）必须在**两端同时**递增该值。
3. 该不变量由测试钉住（`desktop/test/host-protocol.test.ts`）：
   - 跨端常量一致（直接读 Python 源文件比对）；
   - 同协议、不同产品版本 ⇒ `ready`（并把版本原样透传）；
   - 协议不同 ⇒ `incompatible`（产品版本相同也不例外）；
   - 两端常量旁必须保留规约文本（文本钉子，防止规约被静默删除）。

## Consequences

- **正面**：旧服务 / 新客户端这类组合有了明确判据，不必再作为缺陷反复排查；常量漂移会在桌面测试里立刻变红（已用单端 bump 消融验证）。
- **代价**：破坏性协议变更需要一次显式的双端改动（bump + 同步），不能用产品版本自动区分；一旦 bump，历史服务按既有 `attach_probe` / `classifyHealth` 语义一律不可附着（fail-closed，沿用现行为）。
- **边界**：本 ADR 只约束宿主附着面（endpoint 文件、健康面、宿主凭据通道的坐标语义），不引入新的运行时机制，也不改变 `AGENTS.md` §7 任何不变量。
