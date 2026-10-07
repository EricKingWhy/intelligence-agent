# MM-08 · 跨端与恢复验证：换客户端、重启、fork、压缩后图片仍可解释

**目标仓库**：intelligence-agent（集成仓库，证据）；修复回落到对应仓库。
**类型/优先级**：P1 Gate。**Parent**：#821。
**Blocked by**：#824（MM-03）、#825（MM-04）、#826（MM-05）、#827（MM-06）、#828（MM-07）。

## What to build

在任一端附的图，换到另一端能看到同一张图与同一事件历史；crash / resume 后图片仍可用；
fork 出的会话仍能看到 fork 边界之前的图；context compaction 后旧图仍能按 artifact 引用找回；
三端各走一次真机并留下证据。

这一票验证的是**跨端与跨生命周期的一致性**，不是重复单端功能测试。

## Acceptance criteria

- [ ] A 端附图 → B 端（换客户端）看到同一张图与同一事件历史。
- [ ] kill / resume 后附件字节与事件引用都不丢，图片仍可显示。
- [ ] fork 会话能看到 fork 边界之前的图（按既有 Artifact Ref 复用语义，**不新造机制**）。
- [ ] compaction 后摘要保留 artifact refs，旧图可找回；不因压缩删除事实。
- [ ] 事件流里可定位「这条用户消息带了哪些附件引用」，且事件流内**无 base64**。
- [ ] 真机走查（证据绑定运行 ID / 命令 / 退出码 / 事件 JSONL 片段）：
      Web 粘贴真实截图 → 真实视觉模型 → 正确回答；TUI 在 Windows Terminal 走一次（占位路径）；
      CLI 一次 `--image`。
- [ ] 跨 session 附件 id 读取 404 的授权断言在真实服务上复验（不只在单元测试里）。
- [ ] 发现的缺陷以票面 + 证据形式回报到对应仓库；**不在本票顺手扩大范围**。

## 复用与来源

- 既有 Artifact Ref 复用语义（spec 03 §7）与摘要保留 artifact refs（spec 06 §5）——
  本票只验证，不新造机制。
- 证据形式沿用 V3.1-lite 的真实入口 Runtime Verification 惯例。

## 明确不做

新功能；孤儿附件清理；发布 / 安装包改动。
