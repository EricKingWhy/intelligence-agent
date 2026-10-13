/** #1093 请求侧推理档位的渲染契约（Inspector Run 头标）。
 *
 *  背景：Web composer 选了「推理」档位后，此前后端不落任何事件/run 元数据，
 *  Inspector 里无法回溯一个 run 用了哪档。后端 #1093 起 `run/started.data`
 *  在**有声明时**带 `reasoning_effort`（harness 档位名），这里锁渲染侧：
 *   - 有该键 → 头标出现该档位（值就是档位名）；
 *   - 键缺席（模型无能力 / web「默认」位置省略）→ 整段不渲染，**绝不**伪造
 *     "default"（不可得 ≠ 默认档，`11 §6.1`）。
 *
 *  与 `StepDetail.modelIdentity.test.tsx` 同构：ChatTab/StepDetail 的 SSR 渲染
 *  断言，不起浏览器。值域断言按结构取（头标元素），不靠整页 `toContain`——
 *  title 属性里也写着档位名，整页匹配会在渲染崩坏时照样绿。 */

import { describe, expect, it } from 'vitest';
import { createElement } from 'react';
import { renderToString } from 'react-dom/server';
import { EventType } from '../types';
import type { AgentEvent, ConversationState } from '../types';
import { initConversation, applyEvent } from '../lib/projection';
import { StepDetail } from './StepDetail';

const noop = () => {};
const panelProps = {
  panel: { pinned: false, expanded: false, width: 320, peekOpen: true },
  onPanelAction: noop,
} as const;

/** 造一条带可选 reasoning_effort 的 run/started 事件流。 */
function conv(effort: string | null): ConversationState {
  let s = initConversation('eff');
  const data: Record<string, unknown> = { turn_index: 1 };
  if (effort !== null) data.reasoning_effort = effort;
  const started: AgentEvent = {
    type: EventType.RUN_STARTED, data, seq: 1, run_id: 'r1', session_id: 'eff',
  };
  s = applyEvent(s, started);
  return s;
}

const render = (c: ConversationState) =>
  renderToString(createElement(StepDetail, {
    conversation: c,
    streaming: false,
    focus: { kind: 'run' },
    onFocusRun: noop,
    onFocusTool: noop,
    onFocusEvent: noop,
    ...panelProps,
  })).replaceAll('<!-- -->', '');

/** 头标元素是否存在（按 class 判，不按整页文本——title 里也有档位名）。 */
const hasEffortBadge = (html: string) => html.includes('detail-effort-badge');
/** 头标的值域（取该 span 内的文本）。 */
const effortBadgeValue = (html: string) => {
  const m = html.match(/class="detail-effort-badge[^"]*"[^>]*>([\s\S]*?)<\/span>/);
  return m ? m[1].replaceAll(/<[^>]+>/g, '').trim() : null;
};

describe('StepDetail 推理档位头标（#1093）', () => {
  it('run/started 带 reasoning_effort → 头标出现该档位', () => {
    const html = render(conv('deep'));
    expect(hasEffortBadge(html)).toBe(true);
    expect(effortBadgeValue(html)).toBe('推理 · deep');
  });

  it('档位缺席（键不存在）→ 整段不渲染，不伪造 "default"', () => {
    const html = render(conv(null));
    expect(hasEffortBadge(html)).toBe(false);
    expect(html).not.toContain('推理 · default');
  });
});
