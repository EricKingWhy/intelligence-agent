// @vitest-environment jsdom
/** ConstraintInheritance 组件测试（#1192 / PRD §6.3）。
 *
 *  锁的是：① 有快照事件才渲染，无则 null（不伪造）；② INHERITED / NOT
 *  INHERITED 两组的 scope 眉标、类型标签、注入状态文案；③ 干净启动置灰
 *  + 提示（只读展示，无交互开关）。 */

import { describe, expect, it } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import type { AgentEvent } from '../types';
import { CONSTRAINT_INHERITANCE_EVENT_TYPE } from '../lib/constraintInheritance';
import { ConstraintInheritance } from './ConstraintInheritance';

// createRoot + act 的环境开关（仓库测试先例：App.*.test.tsx 同款）
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function ev(data: unknown): AgentEvent {
  return {
    type: CONSTRAINT_INHERITANCE_EVENT_TYPE,
    data,
    seq: 1,
    run_id: null,
    step_id: null,
  } as unknown as AgentEvent;
}

const ROW_PROJECT = {
  fact_id: 'f-1',
  type: 'constraint',
  value: '所有 Python 改动必须先写 failing test',
  scope: 'project',
};
const ROW_SESSION_AUTH = {
  fact_id: 'f-2',
  type: 'authorization',
  value: '允许读写 ~/workspace/intelligence-agent',
  scope: 'session',
};
const ROW_SESSION_OTHER = {
  fact_id: 'f-3',
  type: 'constraint',
  value: '回复代码审查意见时，先给结论再列细节。',
  scope: 'session',
};

describe('ConstraintInheritance', () => {
  let root: Root | null = null;
  let container: HTMLDivElement | null = null;

  function render(ui: React.ReactElement): HTMLElement {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => {
      root!.render(ui);
    });
    return container;
  }

  function cleanup() {
    if (root) {
      act(() => root!.unmount());
      root = null;
    }
    container?.remove();
    container = null;
  }

  it('renders nothing without an inheritance event', () => {
    const c = render(<ConstraintInheritance events={[]} />);
    expect(c.querySelector('.ci-section')).toBeNull();
    cleanup();
  });

  it('renders INHERITED rows with scope eyebrow, type tag and injected status', () => {
    const c = render(
      <ConstraintInheritance
        events={[ev({ omit_constraints: false, inherited: [ROW_PROJECT, ROW_SESSION_AUTH], not_inherited: [] })]}
      />,
    );
    const rows = [...c.querySelectorAll('.ci-row')];
    expect(rows).toHaveLength(2);
    expect(rows[0].textContent).toContain('PROJECT · 本项目');
    expect(rows[0].textContent).toContain('constraint');
    expect(rows[0].textContent).toContain('所有 Python 改动必须先写 failing test');
    expect(rows[0].textContent).toContain('已注入');
    expect(rows[1].textContent).toContain('SESSION · 本会话');
    expect(rows[1].textContent).toContain('authorization');
    expect(rows[1].textContent).toContain('已注入');
    expect(c.querySelector('.ci-clean-msg')).toBeNull();
    // 无 NOT INHERITED 条目 → 该组不渲染
    expect(c.textContent).not.toContain('NOT INHERITED');
    cleanup();
  });

  it('renders NOT INHERITED rows dimmed with the relay hint', () => {
    const c = render(
      <ConstraintInheritance
        events={[ev({ omit_constraints: false, inherited: [ROW_PROJECT], not_inherited: [ROW_SESSION_OTHER] })]}
      />,
    );
    const skip = c.querySelectorAll('.ci-row.ci-skip');
    expect(skip).toHaveLength(1);
    expect(skip[0].textContent).toContain('未注入');
    expect(skip[0].textContent).toContain('由父在委派任务描述里转述');
    expect(c.textContent).toContain('NOT INHERITED');
    cleanup();
  });

  it('shows clean-start banner and dims the inherited block', () => {
    const c = render(
      <ConstraintInheritance
        events={[ev({ omit_constraints: true, inherited: [], not_inherited: [ROW_SESSION_OTHER] })]}
      />,
    );
    const section = c.querySelector('.ci-section')!;
    expect(section.getAttribute('data-omit')).toBe('true');
    expect(c.querySelector('.ci-clean-msg')!.textContent).toContain('干净启动已开启');
    expect(c.querySelector('.ci-rows.ci-dim')).not.toBeNull();
    expect(c.querySelector('.ci-empty')!.textContent).toContain('没有注入任何约束');
    cleanup();
  });
});
