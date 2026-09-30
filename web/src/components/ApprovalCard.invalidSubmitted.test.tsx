// @vitest-environment jsdom
/** #444：「失效」与「已提交」同卡时的文案矛盾锁。
 *  #460：失效说明的措辞按「是否已提交」分支——已提交的失效卡不得再说
 *  「决策无法再提交」（用户已经提交过），也不得回到 #444 修掉的「等待后端确认」
 *  式措辞（该审批已死，没有等待对象）；未提交的失效卡文案原样（既有断言的场景）。
 *
 *  可达序列：POST 成功置 submitted（卡内交互状态，无 prop 可注入）→ 随后该审批
 *  被判失效（投影 stale 或 404-gone，invalid 由 App 统一计算后经 prop 下发）。
 *  标题三态互斥（invalid 优先）本来就对，但 submitted 说明文字没有 `!invalid` 门
 *  ⇒ 同一张卡同现「审批已失效」标题与「决策已提交」说明。
 *
 *  submitted 只能真驱动：mock postApproval 成功 → 点击批准 → 同一根重渲染加
 *  invalid（组件位置不变 ⇒ 内部 state 保留）。宿主照抄 ApprovalModal.test.tsx。
 */
import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return { ...actual, postApproval: vi.fn() };
});

import { postApproval } from '../lib/api';
import { ApprovalCard } from './ApprovalCard';
import type { PendingApproval } from '../types';

function approval(): PendingApproval {
  return {
    approval_id: 'ap-1',
    tool_name: 'bash',
    tool_call_id: 'tc-1',
    action_type: 'danger',
    title: '执行命令',
    description: 'rm -rf 之类',
    arguments_preview: { command: 'ls' },
    permission: 'bash',
    policy: 'ask',
    reason: '危险操作',
    allowed_decisions: ['approve_once', 'deny'],
  };
}

let container: HTMLDivElement | null = null;
let root: Root | null = null;

function render(ui: ReactElement): void {
  if (!container) {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  }
  act(() => root!.render(ui));
}

const card = () => document.body.querySelector('.approval-card')!;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
});

afterEach(() => {
  if (root) act(() => root!.unmount());
  container?.remove();
  container = null;
  root = null;
  vi.mocked(postApproval).mockReset();
});

describe('ApprovalCard — invalid 与 submitted 相遇（#444 / #460）', () => {
  it('已提交后再被判失效：只说「审批已失效」，不再说「决策已提交」', async () => {
    vi.mocked(postApproval).mockResolvedValueOnce(undefined as never);
    render(<ApprovalCard sessionId="s" approval={approval()} />);
    await act(async () => {
      (document.body.querySelector('.approval-approve') as HTMLButtonElement).click();
    });
    // 前置：确已进入 submitted 态（否则本测试没咬住目标状态）
    expect(card().className).toContain('submitted');
    expect(card().textContent).toContain('决策已提交');

    // 随后审批被判失效：同一实例重渲染，invalid prop 下发、内部 submitted 保留
    render(<ApprovalCard sessionId="s" approval={approval()} invalid />);
    expect(card().className).toContain('invalid');
    expect(card().textContent).toContain('审批已失效');
    // #460：用户已提交过 ⇒ 失效说明按已提交措辞，且不得出现两处旧措辞。
    const note = card().querySelector('.approval-invalid-note')!;
    expect(note.textContent).toContain('决策已提交');
    expect(note.textContent).toContain('以事件流为准');
    expect(note.textContent).not.toContain('无法再提交');
    expect(note.textContent).not.toContain('等待后端确认');
  });

  it('未提交即失效（对照）：文案原样——「无法再提交」，且不蹭「决策已提交」措辞', () => {
    render(<ApprovalCard sessionId="s" approval={approval()} invalid />);
    expect(card().className).toContain('invalid');
    expect(card().className).not.toContain('submitted');
    const note = card().querySelector('.approval-invalid-note')!;
    expect(note.textContent).toContain('无法再提交');
    expect(note.textContent).not.toContain('决策已提交');
  });
});
