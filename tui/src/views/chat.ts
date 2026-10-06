/**
 * 对话视图：滚动对话（Pi Markdown 流式渲染）+ 用户消息底色块（Pi user-message 套路：
 * 整块底色、无前缀符号）+ 工具卡嵌入。组件按轮次增量追加/替换，不做全量重建。
 */
import {
  Box,
  Markdown,
  Spacer,
  Text,
  type Component,
  type MarkdownTheme,
} from "@earendil-works/pi-tui";

import type { ConversationState, Turn } from "../adapter.ts";
import { GLYPHS, tintFn, parseColor, type IaTheme } from "../theme.ts";
import { toolCardComponent } from "./toolcard.ts";

export function markdownTheme(theme: IaTheme): MarkdownTheme {
  return {
    heading: (text) => theme.accent(text),
    link: (text) => theme.accent(text),
    linkUrl: (text) => theme.dim(text),
    code: (text) => theme.success(text),
    codeBlock: (text) => theme.text(text),
    codeBlockBorder: (text) => theme.dim(text),
    quote: (text) => theme.muted(text),
    quoteBorder: (text) => theme.dim(text),
    hr: (text) => theme.dim(text),
    listBullet: (text) => theme.accent(text),
    bold: (text) => `\x1b[1m${text}\x1b[22m`,
    italic: (text) => `\x1b[3m${text}\x1b[23m`,
    strikethrough: (text) => `\x1b[9m${text}\x1b[29m`,
    underline: (text) => `\x1b[4m${text}\x1b[24m`,
  };
}

const USER_TINT = "rgb(38, 34, 38)";

export function turnComponents(turn: Turn, theme: IaTheme): Component[] {
  const components: Component[] = [];
  if (turn.role === "user") {
    const box = new Box(1, 0, tintFn(parseColor(USER_TINT), theme.mode));
    box.addChild(new Text(theme.text(turn.text)));
    components.push(box);
    return components;
  }
  if (turn.text) {
    components.push(new Markdown(turn.text, 0, 0, markdownTheme(theme)));
  }
  for (const tool of turn.tools) {
    components.push(toolCardComponent(tool, theme));
    components.push(new Spacer()); // 卡与后续内容空一行（aesthetics 第 1 节的间距纪律）
  }
  return components;
}

/** 全量重建（进会话 / truncated 重建）：事件状态 -> 组件列表。 */
export function conversationComponents(state: ConversationState, theme: IaTheme): Component[] {
  const out: Component[] = [];
  for (const turn of state.turns) {
    if (out.length > 0) out.push(new Spacer()); // 轮次之间空一行
    out.push(...turnComponents(turn, theme));
  }
  return out;
}

/** 空状态：居中短文案 + 可用命令提示（不堆装饰框）。 */
export function emptyStateComponent(theme: IaTheme): Component {
  return new Text(
    [
      theme.muted("还没有会话内容。"),
      theme.dim("直接输入任务开始，或 /help 查看可用命令。"),
    ].join("\n"),
  );
}

/** 运行状态一行（状态栏用）：四态可区分（Spec 11 第 6.1 节）。
 *  glyph 与颜色双重区分（16 色/哑终端下颜色可能丢失）。 */
export function statusLine(state: ConversationState, theme: IaTheme): string {
  switch (state.runStatus) {
    case "running":
      return theme.success(`${GLYPHS.dot} running`);
    case "paused":
      return theme.warning(`${GLYPHS.circle} paused: ${state.pauseInfo?.reason ?? "unknown"}`);
    case "completed":
      return theme.muted(`${GLYPHS.hline} completed`);
    case "failed":
      return theme.error(`${GLYPHS.dot} failed: ${state.failReason ?? "unknown"}`);
    case "interrupted":
      return theme.warning(`${GLYPHS.circle} interrupted`);
    default:
      return theme.dim(`${GLYPHS.circle} idle`);
  }
}
