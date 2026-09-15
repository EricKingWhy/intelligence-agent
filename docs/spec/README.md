# 这个目录不是 Engineering Specification

如果你是被根规则文件（`AGENTS.md` / `CLAUDE.md`）或者某份历史文档带到这里的，**先停一下**。

## 这里是什么

流式 UI / Web UI 那一套产品与实现规格：

- `01_AGENT_RUNTIME_STREAMING_UI_PRD_v2.md`
- `02_RUNTIME_STREAMING_PROTOCOL_SPEC.md`
- `03_FRONTEND_STREAMING_UI_IMPLEMENTATION_SPEC.md`
- `web-ui-redesign-implementation-spec.md`
- `Observable_Agent_Workspace_SDD/`（工作区 UI 的 SDD 子目录）

## 工程规格在哪

```text
SPEC_ROOT = goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/
```

`00_PROJECT_VISION.md`、`01_SYSTEM_ARCHITECTURE.md`、`02_AGENT_RUNTIME.md`、
`13_OPEN_SOURCE_REUSE_MATRIX.md`、`14_IMPLEMENTATION_ROADMAP.md` 等**都在 `SPEC_ROOT/` 下**，
本目录里一个都没有。

## 为什么要在意

根规则文件曾长期把工程规格写成裸路径 `docs/spec/...`。同名不同物，按字面解析会落到本目录，
而引用到的文件在这里不存在——此时正确行为是**回 `SPEC_ROOT/` 找**，不是怀疑规格缺失、
更不是照着这里的流式 UI 规格去改后端。
