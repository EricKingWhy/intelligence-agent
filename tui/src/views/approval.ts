/**
 * 批准/拒绝内联问答（PORT DESIGN：Cline CLI 的 live tool approval 语义）：
 * TTY 内 y/N 键盘决策；非 TTY（管道/录制环境）默认拒绝（审批拒绝不可绕过）。
 * 决策通过 POST /api/sessions/{id}/approve 走服务端唯一入口。
 */
import { Text, type Component } from "@earendil-works/pi-tui";

import type { PendingApproval } from "../adapter.ts";
import { GLYPHS, type IaTheme } from "../theme.ts";
import { collapseArgs } from "../format.ts";

export type ApprovalDecision = "approved" | "denied";

/**
 * 纯决策函数：
 * - 用户给了 y/n -> 按键决策；
 * - TTY 且未决策 -> wait（继续等输入）；
 * - 非 TTY 且未决策 -> denied（默认拒绝，不留悬空审批）。
 */
export function approvalDecision(
  isTTY: boolean,
  answer: string | null,
): ApprovalDecision | "wait" {
  if (answer !== null) {
    return answer === "y" ? "approved" : "denied";
  }
  return isTTY ? "wait" : "denied";
}

/** 内联问答行：状态 chip + 工具名(accent) + 标题(muted) + 参数预览（截断）+ [y/N](accent)。 */
export function approvalLines(approval: PendingApproval, theme: IaTheme): string[] {
  const lines = [
    `${theme.warning(GLYPHS.dot)} ${theme.accent(approval.toolName || "unknown")} ` +
      `${theme.muted(approval.title || approval.description || "请求批准")} ` +
      `${theme.accent("[y/N]")}`,
  ];
  const preview = collapseArgs(approval.argumentsPreview);
  if (preview) lines.push(`${theme.dim(GLYPHS.branch)} ${preview}`);
  return lines;
}

export function approvalComponent(approval: PendingApproval, theme: IaTheme): Component {
  return new Text(approvalLines(approval, theme).join("\n"));
}
