# 多模态图片输入 · 票包（2026-10-07）

**规格**：[`docs/PRD_MULTIMODAL_IMAGE_INPUT.md`](../../PRD_MULTIMODAL_IMAGE_INPUT.md)（发布为 [#821](https://github.com/EricKingWhy/intelligence-agent/issues/821)）
**调研与方案依据**：[`docs/research/2026-10-07-multimodal-image-input-research.md`](../../research/2026-10-07-multimodal-image-input-research.md)
**用户方向**（2026-10-07 已确认）：web / 桌面 / CLI 三端都做；Web 与桌面以 DeepSeek Harness（MIT）为主，
CLI/TUI 以 Pi（MIT）、oh-my-pi（MIT）、Claude Code（语义参考）、Cline（Apache-2.0）为主；
**能抄就不自己写**；走 to-spec → to-tickets 立票。

## 票面清单

| 票 | Issue | 目标仓库 | 依赖 | 一句话 |
| --- | --- | --- | --- | --- |
| MM-01 | [#822](https://github.com/EricKingWhy/intelligence-agent/issues/822) | backend | — | 字节进得来、存得住、按授权取得回（上传 / 内容寻址存储 / 受控读回） |
| MM-02 | [#823](https://github.com/EricKingWhy/intelligence-agent/issues/823) | backend | MM-01 | 模型真的看到图：事件引用 + 投影物化 + Provider 载荷 |
| MM-03 | [#824](https://github.com/EricKingWhy/intelligence-agent/issues/824) | backend | MM-02 | 上限、非视觉降级、预算计入 |
| MM-04 | [#825](https://github.com/EricKingWhy/intelligence-agent/issues/825) | frontend（`web/`） | MM-01,02,03 | Web 附图 Composer 与消息内渲染（DSH 复制） |
| MM-05 | [#826](https://github.com/EricKingWhy/intelligence-agent/issues/826) | frontend（`desktop/`） | MM-04 | 桌面宿主路径桥 + 拖入分流（含独立安全审查） |
| MM-06 | [#827](https://github.com/EricKingWhy/intelligence-agent/issues/827) | frontend（`tui/`） | MM-01,02,03 | TUI 粘贴 / 路径识别 / `[Image #N]` / 终端降级 |
| MM-07 | [#828](https://github.com/EricKingWhy/intelligence-agent/issues/828) | backend | MM-01,02,03 | CLI `--image` |
| MM-08 | [#830](https://github.com/EricKingWhy/intelligence-agent/issues/830) | 集成仓库（证据） | MM-03,04,05,06,07 | 跨端 / 重启 / fork / 压缩一致性 Gate |

依赖已同时写成 GitHub **原生 `blocked_by`** 边（`issue_dependencies_summary.blocked_by` 可机读），
票面正文的 Blocked by 与之一致。

## 依赖图

```text
MM-01 ──▶ MM-02 ──▶ MM-03 ──┬──▶ MM-04 ──▶ MM-05
                            ├──▶ MM-06
                            └──▶ MM-07
MM-03, MM-04, MM-05, MM-06, MM-07 ──▶ MM-08
```

**Frontier（可立即开工）**：MM-01 一张。MM-02 起严格串行到 MM-03；MM-04/06/07 在 MM-03 后可并行。

## 拆票理由

- **MM-01 与 MM-02 分开**：存储/HTTP 契约与「模型看到图」是两类风险，各自能独立验证
  （前者 curl 上传读回即可验证，后者才是真正的 tracer bullet）；合并会超出单个上下文窗口。
- **MM-03 单独成票**：上限与降级是边界行为，含失败路径与预算口径，需要自己的失败路径测试族；
  它同时产出 `supports_vision` 的对外暴露，是三张客户端票的共同前置。
- **客户端按端拆票**：三端复制来源不同（DSH / DSH+宿主桥 / pi-tui+Cline），且桌面票带独立安全审查，
  合成一票会掩盖这个差异。
- **MM-08 是 Gate 票而非功能票**：只验证跨端与跨生命周期一致性，发现缺陷回报到对应仓库，不就地扩范围。

## 开工前必读

1. `docs/PRD_MULTIMODAL_IMAGE_INPUT.md` 全文，特别是 **Seams** 一节（唯一新增接缝 = 服务端用户轮次入站边界，
   待 reviewer 确认）与 D1–D12。
2. `docs/research/2026-10-07-multimodal-image-input-research.md` 的复制清单（§7.3）与 License 边界。
3. 按 `docs/agents/reference-sources.md` 纪律**重新核对上游当前版本与 License**（本票包记录的是
   2026-10-07 的 commit，会漂移）。
4. 三个 clone 的实际分支与状态（目标仓库见上表）。
