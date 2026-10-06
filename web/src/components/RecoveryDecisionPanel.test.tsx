// @vitest-environment jsdom
/** RecoveryDecisionPanel（#357 W-13 契约 2/3/4）渲染 + 交互。
 *
 *  锁的是修订 A §9.4 的四条产品要求：
 *  ① 标题是**后果语言**，界面不出现「这个 tool_call_id 到底生效没」这类开放技术问答；
 *  ② 每卡三选一，**默认选中最安全项**（= 后端 default_action）；
 *  ③ 「当作已生效」才采集来源，来源逐字进提交载荷、不伪造；
 *  ④ 「全部按默认安全动作处理」一键把各卡设回各自默认项。
 *  Esc 可关闭面板；提交中禁用提交按钮。 */

import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { PendingDecision, RecoverDecisionInput } from '../lib/api';
import { RecoveryDecisionPanel } from './RecoveryDecisionPanel';

let host: HTMLDivElement;
let root: Root;

const CARDS: PendingDecision[] = [
  {
    tool_call_id: 'call-a',
    tool_name: 'git_status',
    state: 'NEED_RECONCILE',
    default_action: 'RETRY',
    risk_level: 'low',
    probe: { verifiable: true, suggested_action: '重新运行 git status，核对工作区状态。' },
  },
  {
    tool_call_id: 'call-b',
    tool_name: 'bash',
    state: 'UNKNOWN',
    default_action: 'DEFER',
    risk_level: 'high',
    probe: { verifiable: false, suggested_action: null },
  },
];

function render(
  props: Partial<Parameters<typeof RecoveryDecisionPanel>[0]> = {},
): { onSubmit: ReturnType<typeof vi.fn>; onClose: ReturnType<typeof vi.fn> } {
  const onSubmit = vi.fn();
  const onClose = vi.fn();
  act(() => {
    root.render(
      <RecoveryDecisionPanel
        decisions={CARDS}
        submitting={false}
        message="存在需要人工裁决的高风险操作"
        onSubmit={onSubmit as (d: RecoverDecisionInput[]) => void}
        onClose={onClose}
        {...props}
      />,
    );
  });
  return { onSubmit, onClose };
}

function radio(callId: string, verdict: string): HTMLInputElement {
  const el = document.querySelector<HTMLInputElement>(
    `input[name="recovery-${callId}"][value="${verdict}"]`,
  );
  if (!el) throw new Error(`no radio ${callId}/${verdict}`);
  return el;
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

describe('RecoveryDecisionPanel', () => {
  it('标题用后果语言，且不出现工具调用 id 或开放技术问答', () => {
    render();
    const title = document.querySelector('.recovery-decision-title')!.textContent ?? '';
    expect(title).toContain('2');
    expect(title).toContain('结果不确定');
    expect(title).not.toContain('call-a');
    expect(title).not.toContain('tool_call_id');
  });

  it('每卡默认选中最安全项（= 后端 default_action）', () => {
    render();
    expect(radio('call-a', 'RETRY').checked).toBe(true);
    expect(radio('call-a', 'DEFER').checked).toBe(false);
    expect(radio('call-b', 'DEFER').checked).toBe(true);
    expect(radio('call-b', 'RETRY').checked).toBe(false);
    // CONFIRM_SUCCESS 永远不是默认项
    expect(radio('call-a', 'CONFIRM_SUCCESS').checked).toBe(false);
  });

  it('撤销/后续裁决后「当作没生效」的文案点明服务端行为（永不盲跑）', () => {
    render();
    const text = document.querySelector('.recovery-decision-panel')!.textContent ?? '';
    expect(text).toContain('永不盲跑');
    expect(text).toContain('稍后');
  });

  it('批量「全部按默认安全动作处理」把各卡设回各自默认项，提交后载荷取默认 verdict', () => {
    const { onSubmit } = render();
    // 先手动改一卡，再点批量
    act(() => radio('call-a', 'CONFIRM_SUCCESS').click());
    act(() => (document.querySelector('.recovery-batch-btn') as HTMLButtonElement).click());
    expect(radio('call-a', 'RETRY').checked).toBe(true);
    expect(radio('call-b', 'DEFER').checked).toBe(true);

    act(() => (document.querySelector('.recovery-submit-btn') as HTMLButtonElement).click());
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit.mock.calls[0][0]).toEqual([
      { tool_call_id: 'call-a', verdict: 'RETRY', source: undefined },
      { tool_call_id: 'call-b', verdict: 'DEFER', source: undefined },
    ]);
  });

  it('「当作已生效」才出现来源采集，预设来源逐字进载荷', () => {
    const { onSubmit } = render();
    expect(document.querySelector('.recovery-source')).toBeNull();

    act(() => radio('call-a', 'CONFIRM_SUCCESS').click());
    const choice = document.querySelector('.recovery-source-choice') as HTMLSelectElement;
    expect(choice).not.toBeNull();
    act(() => {
      choice.value = '我查了外部系统';
      choice.dispatchEvent(new Event('change', { bubbles: true }));
    });

    act(() => (document.querySelector('.recovery-submit-btn') as HTMLButtonElement).click());
    expect(onSubmit.mock.calls[0][0][0]).toEqual({
      tool_call_id: 'call-a',
      verdict: 'CONFIRM_SUCCESS',
      source: '我查了外部系统',
    });
  });

  it('自定义来源优先于预设', () => {
    const { onSubmit } = render();
    act(() => radio('call-a', 'CONFIRM_SUCCESS').click());
    const choice = document.querySelector('.recovery-source-choice') as HTMLSelectElement;
    act(() => {
      choice.value = '我亲眼看到结果了';
      choice.dispatchEvent(new Event('change', { bubbles: true }));
    });
    const custom = document.querySelector('.recovery-source-custom') as HTMLInputElement;
    act(() => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;
      setter.call(custom, '看了 DB 表');
      custom.dispatchEvent(new Event('input', { bubbles: true }));
    });
    act(() => (document.querySelector('.recovery-submit-btn') as HTMLButtonElement).click());
    expect(onSubmit.mock.calls[0][0][0].source).toBe('看了 DB 表');
  });

  it('probe 只转述后端 hint：不可核验的卡片写「未查到」，不猜', () => {
    render();
    const cards = document.querySelectorAll('.recovery-card');
    expect(cards[0].querySelector('.recovery-probe-state')!.textContent).toContain('已查到');
    expect(cards[0].textContent).toContain('重新运行 git status');
    expect(cards[1].querySelector('.recovery-probe-state')!.textContent).toContain('未查到');
    expect(cards[1].querySelector('.recovery-probe-detail')).toBeNull();
  });

  it('参数摘要缺省时如实显示「—」，不编造', () => {
    render();
    const args = document.querySelector('.recovery-args')!.textContent ?? '';
    expect(args).toContain('—');
  });

  it('Esc 关闭面板', () => {
    const { onClose } = render();
    act(() => {
      document.querySelector('.recovery-decision-panel')!.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
      );
    });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('来源输入框内按 Esc 不关闭面板、不丢已输入内容', () => {
    const { onClose } = render();
    act(() => radio('call-a', 'CONFIRM_SUCCESS').click());
    const custom = document.querySelector('.recovery-source-custom') as HTMLInputElement;
    act(() => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;
      setter.call(custom, '看了 DB 表');
      custom.dispatchEvent(new Event('input', { bubbles: true }));
    });
    act(() => {
      custom.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    });
    expect(onClose).not.toHaveBeenCalled();
    expect(custom.value).toBe('看了 DB 表');
  });

  it('提交中禁用提交按钮并显示进度文案', () => {
    render({ submitting: true });
    const submit = document.querySelector('.recovery-submit-btn') as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    expect(submit.textContent).toContain('提交中');
  });

  it('每张卡有唯一的单选组与可读标签（键盘/读屏可达）', () => {
    render();
    const groups = document.querySelectorAll('.recovery-options');
    expect(groups).toHaveLength(2);
    for (const group of groups) {
      expect(group.getAttribute('role')).toBe('radiogroup');
      // 原生 radio：Tab 进组、方向键切换、aria-checked 隐式如实
      expect(group.querySelectorAll('input[type="radio"]')).toHaveLength(3);
    }
  });
});
