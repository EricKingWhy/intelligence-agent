/**
 * Task/Session 选择器：Pi SelectList 的会话选择装配。
 * 数据只来自 GET /api/sessions（服务端投影），不本地缓存第二份列表。
 */
import { SelectList, type SelectItem } from "@earendil-works/pi-tui";

import type { IaTheme } from "../theme.ts";

export interface SessionSummaryView {
  session_id: string;
  event_count: number;
  first_user_message: string | null;
  last_event_time: string | null;
  archived: boolean;
}

export function sessionItems(sessions: SessionSummaryView[]): SelectItem[] {
  return sessions.map((s) => {
    const label = s.first_user_message ?? "";
    return {
      value: s.session_id,
      label: label
        ? label.length > 60
          ? `${label.slice(0, 60)}...`
          : label
        : s.session_id,
      description:
        `${s.event_count} events` +
        (s.last_event_time ? ` · ${s.last_event_time}` : "") +
        (s.archived ? " [archived]" : ""),
    };
  });
}

export function sessionSelectList(
  sessions: SessionSummaryView[],
  theme: IaTheme,
  maxVisible = 10,
): SelectList {
  return new SelectList(sessionItems(sessions), maxVisible, {
    selectedPrefix: (text) => theme.accent(text),
    selectedText: (text) => theme.accent(text),
    description: (text) => theme.muted(text),
    scrollInfo: (text) => theme.dim(text),
    noMatch: (text) => theme.muted(text),
  });
}
