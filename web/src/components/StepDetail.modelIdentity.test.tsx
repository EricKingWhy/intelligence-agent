/** #226 请求侧模型标识的渲染契约（机制全文见 ADR-0034）。
 *
 *  背景：Inspector 的 MODEL 一节此前只有「模型」一行，值来自会话内**最近一次**模型写入
 *  （provider 回显 / fallback 切换 / 会话级切换 / 重放重建），provider 不回显（网关 /
 *  自建端点）时刷新之后这一节只剩「暂无观测数据」，用户连"这一轮请求的是哪个模型"都
 *  看不到，尽管 `run/started` 里一直记着。
 *
 *  这里锁五件事：
 *   - 只有请求侧、没有回显 → 该节**不再是空槽**，出现「请求模型」行；
 *   - 两侧一致（同一 run）→ **不**多渲染一行（无信息量的噪音）；
 *   - 两侧不一致（同一 run）→ 两行都在（这才是真正需要对照的情形）；
 *   - **回显来自别的 run** → 「模型」行标「非本轮」（否则"模型 A / 请求模型 B"会被读成
 *     "请求了 B 却回显 A"，而 A 只是上一轮的值——两轴审查共识 P2）；
 *   - 都没有 → 仍是空槽提示（不铺空行、不编模型名）。
 *
 *  与 `StepDetail.runFailure.test.tsx` 同构：ChatTab 的 SSR 渲染断言，不起浏览器。
 *  **值域断言一律按结构取**（见下面两个 helper）：对整页 `toContain(模型名)` 会被
 *  同一节里别的行、乃至同一元素的 `title` 属性满足——那是本仓踩过的假绿家族。 */

import { describe, expect, it } from 'vitest';
import { createElement } from 'react';
import { renderToString } from 'react-dom/server';
import { EventType } from '../types';
import type { AgentEvent, ConversationState } from '../types';
import { initConversation, applyEvent } from '../lib/projection';
import { ChatTab } from './StepDetail';

const strip = (html: string) => html.replaceAll('<!-- -->', '');

const render = (conv: ConversationState) =>
  strip(renderToString(createElement(ChatTab, { conversation: conv, tools: [] })));

/** 某一行的**值域**（按 `detail-key` 的文本定位到紧随其后的第一个 `detail-val`）。
 *  为什么不直接 `toContain(模型名)`：模型名也出现在同一行的 `title` 属性里，于是
 *  "字符串在整页里存在"会在**值域为空**的树上照样绿（#222 真机实测踩过）。
 *  `class="detail-key">模型` 这个前缀不会命中「请求模型」那一行（它的 key 文本不同）。 */
const rowValue = (html: string, key: string) => {
  const m = html.match(
    new RegExp(`class="detail-key">${key}</span><span[^>]*>([\\s\\S]*?)</span></div>`),
  );
  return m ? m[1].replaceAll(/<[^>]+>/g, '').trim() : null;
};

/** 这一行**存在与否**只能按结构判：`模型` 那行的 `title` 里也写着「请求模型」四个字
 *  （说明文字指向它），所以 `not.toContain('请求模型')` 是恒假的——同为"字符串在整页
 *  里存在"的假绿家族，这里按 key 元素判。 */
const hasRequestedRow = (html: string) => html.includes('class="detail-key">请求模型</span>');

/** 造一条**真实时序**的事件流：run 1（可带请求侧模型 + 回显）+ 可选的 run 2
 *  （只到 `run/started`，即"本轮还没回显"）。
 *
 *  为什么必须按真实时序：`conversation.run_id` 是"最近一条带 run_id 的事件"的 run
 *  （`projection.ts` 的 `applyEvent` 统一写入），所以"上一轮的回显"只可能出现在本轮
 *  `run/started` **之前**。构造出"上一轮回显晚于本轮开始"这种不可能的时序，测出来的
 *  是假行为。 */
function conv(over: {
  first?: string | null;
  echo?: string | null;
  second?: string | null;
} = {}): ConversationState {
  const first = over.first ?? null;
  const echo = over.echo ?? null;
  const second = over.second ?? null;
  let s = initConversation('m');
  const evs: AgentEvent[] = [];
  let seq = 0;
  const started = (runId: string, model: string | null): AgentEvent => {
    const data: Record<string, unknown> = { turn_index: seq + 1 };
    if (model !== null) data.model = model;
    return {
      type: EventType.RUN_STARTED, data, seq: ++seq, run_id: runId, session_id: 'm',
    } as AgentEvent;
  };
  evs.push(started('r1', first));
  if (echo !== null) {
    evs.push({
      type: EventType.MODEL_COMPLETED,
      data: { content: 'hi', model: echo },
      seq: ++seq, run_id: 'r1', session_id: 'm', step_id: 1,
    } as AgentEvent);
  }
  if (second !== null) evs.push(started('r2', second));
  for (const ev of evs) s = applyEvent(s, ev);
  return s;
}

describe('StepDetail 请求侧模型行（#226 / ADR-0034）', () => {
  it('provider 不回显 → 该节不是空槽，且「请求模型」行的值就是请求侧的名字', () => {
    const html = render(conv({ first: 'custom:my-model' }));
    expect(html).not.toContain('暂无观测数据');
    expect(hasRequestedRow(html)).toBe(true);
    expect(rowValue(html, '请求模型')).toBe('custom:my-model');
    // 「模型」行仍在（回显侧缺席读作「—」，不拿请求侧冒充回显）
    expect(rowValue(html, '模型')).toBe('—');
  });

  it('两侧一致（同一 run）→ 不渲染「请求模型」行（重复一行无信息量）', () => {
    const html = render(conv({ first: 'deepseek-chat', echo: 'deepseek-chat' }));
    expect(hasRequestedRow(html)).toBe(false);
    expect(rowValue(html, '模型')).toBe('deepseek-chat');
    // 同一 run 的回显 ⇒ 不标「非本轮」
    expect(rowValue(html, '模型')).not.toContain('非本轮');
  });

  it('两侧不一致（同一 run）→ 两行都在（这才是需要对照的情形）', () => {
    const html = render(conv({ first: 'deepseek-chat', echo: 'deepseek-v4' }));
    expect(hasRequestedRow(html)).toBe(true);
    expect(rowValue(html, '请求模型')).toBe('deepseek-chat');
    expect(rowValue(html, '模型')).toBe('deepseek-v4');
  });

  it('回显来自**别的 run** → 「模型」行标「非本轮」（两轴审查共识 P2）', () => {
    // run1 回显 A；run2 请求 B 且本轮尚无 model/completed（首个调用前失败/取消，
    // 或换成了不回显的 provider）。不标出来的话，这一对值会被读成
    // "请求了 B 却回显 A（provider 无视请求）"——A 其实只是上一轮的值。
    const html = render(conv({ first: 'A', echo: 'A', second: 'B' }));
    expect(rowValue(html, '模型')).toBe('A（非本轮）');
    expect(rowValue(html, '请求模型')).toBe('B');
  });

  it('旧版后端（run/started 不带该键）→ 仍是空槽提示（不编模型名）', () => {
    const html = render(conv());
    expect(html).toContain('暂无观测数据');
    expect(hasRequestedRow(html)).toBe(false);
  });
});
