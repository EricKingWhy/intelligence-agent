# MM-07 · CLI：`--image`

**目标仓库**：intelligence-agent-backend（Python CLI）。
**类型/优先级**：P2。**Parent**：#821。
**Blocked by**：#822（MM-01）、#823（MM-02）、#824（MM-03）。

## What to build

CLI 用户用 `--image <path>`（可重复）在一次命令里附图；路径不存在或不是支持的图片格式时
立刻收到明确错误，且不发消息、不留残留。

## Acceptance criteria

- [ ] `--image <path>` 可重复（`action="append"`），与既有 argparse 子命令风格一致。
- [ ] 读文件 → magic-bytes MIME 探测（标准库签名表，**不引新依赖**）→ 上传 → 消息事件带引用。
- [ ] 路径不存在 / 非支持格式 / 超限 → 明确错误 + 非零退出；不发消息、不留存储残留。
- [ ] 模型不支持视觉时在提交前明确报错。
- [ ] 不带 `--image` 的既有纯文本 CLI 行为**逐字不变**。
- [ ] focused CLI 测试覆盖成功路径与三条失败路径。

## 复用与来源

- 复用 MM-01 的上传端点与 MM-02 的发送契约（同一 API 客户端，不另开消息路径）。
- 错误语义（提交前拒绝、明确原因）参考 Aider 的 `--message-file` / 图片附件行为（调研报告 §2.1）。

## 明确不做

交互式 REPL 的附图（属 TUI 票 MM-06）；`@path` 之外的引用语法扩展。
