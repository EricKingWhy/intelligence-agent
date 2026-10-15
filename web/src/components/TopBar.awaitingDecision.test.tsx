// @vitest-environment jsdom
/** #1030（缺陷①）：顶栏徽标缺「待裁决」态——坏死 run 误显「思考中」。
 *
 *  票面现场：会话 `b9aca235-…` 的 bash 调用（find，60.4s 超时被杀）结果 UNKNOWN，
 *  run 无终态、`run_status` 停在 'running'，顶栏却显示「思考中 · 176,512 tok」——
 *  用户会去等一个永远不会回来的结果，而唯一能解封会话的动作（裁决）就藏在别处。
 *
 *  本文件锁的是**呈现层**的行为：同一个 conversation，`awaitingDecision` 为真时
 *  徽标必须是「待裁决」，且不得带耗时秒数（它没在跑，不能计秒）。
 *  投影侧的判据在 runState.test.ts（awaiting-decision 通道）。
 */
import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { applyEvent, initConversation } from '../lib/projection';
import type { TraceDensity } from '../lib/density';
import type { Theme } from '../lib/theme';
import { EventType, type AgentEvent, type ConversationState, type EventTypeValue } from '../types';
import { TopBar } from './TopBar';

let seq = 0;
function ev(sid: string, type: EventTypeValue, data: Record<string, unknown>): AgentEvent {
  seq += 1;
  return { session_id: sid, time: '2026-10-12T00:00:00Z', type, seq, run_id: 'r', step_id: 1, data };
}

/** 坏死 run：run/started + 模型流，**没有任何终态事件**（票面现场的形状）。 */
function zombieState(sid: string): ConversationState {
  let s = applyEvent(initConversation(sid), ev(sid, EventType.RUN_STARTED, { turn_index: 1 }));
  s = applyEvent(s, ev(sid, EventType.MODEL_STARTED, { step: 1 }));
  return applyEvent(s, ev(sid, EventType.MODEL_DELTA, { delta: 'work', step: 1 }));
}

/** 重启后：run/interrupted 已落盘（run_status 收成 completed，标记仍在）。 */
function restartedState(sid: string): ConversationState {
  let s = zombieState(sid);
  return applyEvent(
    s,
    ev(sid, EventType.RUN_INTERRUPTED, { interrupted_seq: 3, reason: 'process_restart' }),
  );
}

let container: HTMLDivElement | null = null;
let root: Root | null = null;

function renderTopBar(conversation: ConversationState, awaitingDecision: boolean, streaming = false): void {
  const ui: ReactElement = (
    <TopBar
      conversation={conversation}
      streaming={streaming}
      inspectorOpen={false}
      onToggleInspector={() => {}}
      density={'balanced' as TraceDensity}
      onDensityChange={() => {}}
      theme={'dark' as Theme}
      onToggleTheme={() => {}}
      authRequired={false}
      onOpenMemories={() => {}}
      onOpenApprovePolicy={() => {}}
      sessionId={conversation.session_id}
      onOpenContextUsage={() => {}}
      awaitingDecision={awaitingDecision}
    />
  );
  act(() => {
    root!.render(ui);
  });
}

function pulse(): HTMLElement {
  const el = container!.querySelector<HTMLElement>('.run-pulse');
  if (!el) throw new Error('no .run-pulse');
  return el;
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  seq = 0;
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => {
    root?.unmount();
  });
  container?.remove();
  root = null;
  container = null;
});

describe('#1030 TopBar 徽标 · 待裁决是一等态（AC1/AC4）', () => {
  it('AC1：坏死 run + 待裁决 ⇒ 「待裁决」，不再显示「思考中」', () => {
    renderTopBar(zombieState('s-1'), true);
    expect(pulse().textContent).toContain('待裁决');
    expect(pulse().textContent).not.toContain('思考中');
  });

  it('AC1（反证）：同一会话无待裁决时**仍**显示「思考中」——差异只由这个事实驱动', () => {
    // 若这一条也变成「待裁决」，说明徽标不是被"待裁决"这个事实驱动的，
    // 上一条用例就证明不了任何事（消融实验的对照臂）。
    renderTopBar(zombieState('s-1'), false);
    expect(pulse().textContent).toContain('思考中');
  });

  it('AC4：重启后（run/interrupted 落盘）裁决仍挂 ⇒ 仍「待裁决」，压过「已中断」', () => {
    // 票面实测的峰值误导态：徽标转「已中断」+ 黄横幅，"看似痊愈"，
    // 但 UNKNOWN 裁决还挂着、发消息照样 409，且此时连红色横幅都没了。
    renderTopBar(restartedState('s-1'), true);
    expect(pulse().textContent).toContain('待裁决');
    expect(pulse().textContent).not.toContain('已中断');
  });

  it('AC4（反证）：无裁决时同一重启会话就是「已中断」', () => {
    renderTopBar(restartedState('s-1'), false);
    expect(pulse().textContent).toContain('已中断');
  });

  it('待裁决**不计秒**：它没在跑，给死掉的 run 计秒就是谎报', () => {
    // active 只含 thinking/tool；待裁决若被算进 active，徽标会随 elapsedSec 长出
    // 「· 12s」——读起来"还在推进"。静态渲染下 elapsedSec=0，故断言 class 而非文本：
    // 用 CSS 类锁定它走的是静止态样式（.pulse-awaiting-decision，无动画）。
    renderTopBar(zombieState('s-1'), true, true);
    expect(pulse().className).toContain('pulse-awaiting-decision');
    expect(pulse().className).not.toContain('pulse-thinking');
    expect(pulse().className).not.toContain('pulse-tool');
  });

  it('缺省该 prop ⇒ 行为与改动前一致（既有夹具零迁移）', () => {
    const ui: ReactElement = (
      <TopBar
        conversation={zombieState('s-1')}
        streaming={false}
        inspectorOpen={false}
        onToggleInspector={() => {}}
        density={'balanced' as TraceDensity}
        onDensityChange={() => {}}
        theme={'dark' as Theme}
        onToggleTheme={() => {}}
        authRequired={false}
        onOpenMemories={() => {}}
        onOpenApprovePolicy={() => {}}
        sessionId="s-1"
        onOpenContextUsage={() => {}}
      />
    );
    act(() => {
      root!.render(ui);
    });
    expect(pulse().textContent).toContain('思考中');
  });
});
