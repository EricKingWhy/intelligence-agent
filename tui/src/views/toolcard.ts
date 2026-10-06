/**
 * 工具卡：oh-my-pi ToolCard 的信息结构（状态行五件套 + 分区）x Pi Box 底色 tint
 * （PORT DESIGN：只抄交互结构，不依赖 OMP 包、不抄代码；aesthetics.md 第 2 节）。
 *
 * 状态机与服务端 tool/* 事件 1:1（running -> success/error），不自创状态；
 * 状态靠底色 tint 传达（Pi tool-execution 套路），不靠图标。
 */
import { Box, Text, type Component } from "@earendil-works/pi-tui";

import type { ToolCard as ToolCardModel } from "../adapter.ts";
import { collapseArgs } from "../format.ts";
import { CARD_TINTS, GLYPHS, parseColor, tintFn, type IaTheme } from "../theme.ts";

const PREVIEW_LINES = 6;

/** 状态行五件套：chip + 标题(accent) + 描述(muted) + badge + meta(dim，点分隔)。 */
export function toolStatusLine(tool: ToolCardModel, theme: IaTheme): string {
  const chip =
    tool.status === "running"
      ? theme.accent(GLYPHS.circle)
      : tool.status === "success"
        ? theme.success(GLYPHS.dot)
        : theme.error(GLYPHS.dot);
  const title = theme.accent(tool.name);
  const description = tool.title ? theme.muted(tool.title) : "";
  const badge =
    tool.status === "success"
      ? theme.success("[ok]")
      : tool.status === "error"
        ? theme.error("[fail]")
        : theme.muted("[run]");
  const metaParts: string[] = [];
  if (tool.durationMs !== null) metaParts.push(`${tool.durationMs.toFixed(1)}s`);
  if (tool.artifact !== null) metaParts.push(`artifact ${tool.artifact.artifact_id}`);
  const meta = theme.dim(metaParts.join(" · "));
  return [chip, title, description, badge, meta].filter(Boolean).join(" ");
}

/** 事件 -> 卡的视图模型：Pi Box + OMP 信息结构（纯装配，测试断言行内容）。 */
export function toolCardLines(tool: ToolCardModel, theme: IaTheme): string[] {
  const lines: string[] = [toolStatusLine(tool, theme)];
  const argsLine = collapseArgs(tool.args);
  if (argsLine) lines.push(`${theme.dim("args")} ${argsLine}`);
  if (tool.output) {
    for (const line of tool.output.split("\n").slice(-PREVIEW_LINES)) {
      lines.push(`${theme.dim(GLYPHS.branch)} ${line}`);
    }
  } else if (tool.message) {
    for (const line of tool.message.split("\n").slice(0, PREVIEW_LINES)) {
      lines.push(`${theme.dim(GLYPHS.branch)} ${line}`);
    }
  }
  return lines;
}

/** 装配成带状态 tint 底色的 Box 组件（增量更新时由 app 层重建/替换）。 */
export function toolCardComponent(tool: ToolCardModel, theme: IaTheme): Component {
  const bg =
    tool.status === "success"
      ? tintFn(parseColor(CARD_TINTS.success), theme.mode)
      : tool.status === "error"
        ? tintFn(parseColor(CARD_TINTS.error), theme.mode)
        : tool.status === "running"
          ? tintFn(parseColor(CARD_TINTS.running), theme.mode)
          : undefined;
  const box = new Box(1, 0, bg);
  box.addChild(new Text(toolCardLines(tool, theme).join("\n")));
  return box;
}
