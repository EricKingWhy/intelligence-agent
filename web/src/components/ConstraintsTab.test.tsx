// @vitest-environment jsdom
/** ConstraintsTab / ConstraintsView（#1190 / #1176 Phase 1）—— 约束管理页。
 *
 *  锁的是 issue §四与不变量 #22（UI 不做第二真相）：
 *  ① 纯展示视图 `ConstraintsView` 由数据 props 驱动——状态筛选默认「生效中」、
 *     revoked/superseded 归「已撤销」页、类型下拉纯展示层过滤；
 *  ② 有状态外壳 `ConstraintsTab` 挂载即拉后端投影，**撤销后重拉对齐**（不做乐观
 *     本地更新）——真相在后端；
 *  ③ 空态 / 无权限（403）如实说，不冒充空列表；
 *  ④ 时间/操作人展示：坏值原样、本地哨兵 `local` → 「本人操作」。 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { renderToString } from 'react-dom/server';
import {
  SessionError,
  listProtectedFacts,
  revokeProtectedFact,
  type ProtectedFact,
} from '../lib/api';
import { formatFactTime, operatorLabel } from '../lib/constraints';
import { ConstraintsTab, ConstraintsView } from './ConstraintsTab';

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return { ...actual, listProtectedFacts: vi.fn(), revokeProtectedFact: vi.fn() };
});

const T = '2026-09-07T00:00:00Z';

const ACTIVE: ProtectedFact = {
  fact_id: 'f-active',
  type: 'constraint',
  value: '不要改动生产环境配置',
  status: 'active',
  source_event_id: 'ev-3',
  source_seq: 3,
  registered_at: T,
  revoked_at: null,
  revoked_by: null,
};

const REVOKED: ProtectedFact = {
  fact_id: 'f-revoked',
  type: 'user_goal',
  value: '先修好登录页对比度',
  status: 'revoked',
  source_event_id: 'ev-4',
  source_seq: 4,
  registered_at: T,
  revoked_at: T,
  revoked_by: 'local',
};

const SUPERSEDED: ProtectedFact = {
  fact_id: 'f-superseded',
  type: 'authorization',
  value: '允许写 workspace',
  status: 'superseded',
  source_event_id: 'ev-5',
  source_seq: 5,
  registered_at: T,
  revoked_at: null,
  revoked_by: null,
};

const viewBase = {
  sessionId: 's-1',
  facts: [] as ProtectedFact[],
  loading: false,
  error: null as string | null,
  forbidden: false,
  statusFilter: 'active' as const,
  typeFilter: '',
  onStatusFilter: () => {},
  onTypeFilter: () => {},
  onRetry: () => {},
  onRequestRevoke: () => {},
};

function ssr(over: Partial<Parameters<typeof ConstraintsView>[0]> = {}): string {
  return renderToString(<ConstraintsView {...viewBase} {...over} />);
}

describe('ConstraintsView（纯展示，SSR）', () => {
  it('页眉与标题常驻（不因空/错而消失）', () => {
    const html = ssr();
    expect(html).toContain('CONSTRAINTS');
    expect(html).toContain('约束管理');
    expect(html).toContain('s-1');
  });

  it('默认「生效中」只渲染 active 行', () => {
    const html = ssr({ facts: [ACTIVE, REVOKED, SUPERSEDED] });
    expect(html).toContain('不要改动生产环境配置');
    expect(html).not.toContain('先修好登录页对比度');
    expect(html).not.toContain('允许写 workspace');
  });

  it('「已撤销」页含 revoked + superseded（issue §四.3），active 退场', () => {
    const html = ssr({ facts: [ACTIVE, REVOKED, SUPERSEDED], statusFilter: 'revoked' });
    expect(html).toContain('先修好登录页对比度');
    expect(html).toContain('允许写 workspace');
    expect(html).not.toContain('不要改动生产环境配置');
  });

  it('「全部」页三条都在', () => {
    const html = ssr({ facts: [ACTIVE, REVOKED, SUPERSEDED], statusFilter: 'all' });
    expect(html).toContain('不要改动生产环境配置');
    expect(html).toContain('先修好登录页对比度');
    expect(html).toContain('允许写 workspace');
  });

  it('类型下拉按 type 过滤（纯展示层）', () => {
    const html = ssr({ facts: [ACTIVE, REVOKED], statusFilter: 'all', typeFilter: 'user_goal' });
    expect(html).toContain('先修好登录页对比度');
    expect(html).not.toContain('不要改动生产环境配置');
  });

  it('撤销/取代行：撤销按钮 disabled 且文案=状态标签', () => {
    const html = ssr({ facts: [REVOKED], statusFilter: 'revoked' });
    expect(html).toContain('disabled');
    expect(html).toContain('已撤销');
  });

  it('已撤销行的元信息行含撤销时间与操作人（AC3：记录可审计）', () => {
    const html = ssr({ facts: [REVOKED], statusFilter: 'revoked' });
    // revoked_by='local'（哨兵 → 「本人操作」）。SSR 会在相邻文本节点间插 <!-- -->，
    // 故按**单个文本节点**断言（不跨注释边界）。
    expect(html).toContain('本人操作');
    expect(html).toContain('撤销');
    expect(html).toContain('f-revoked');
  });

  it('已被取代行文案区分「已被取代」，且无撤销时间', () => {
    const html = ssr({ facts: [SUPERSEDED], statusFilter: 'revoked' });
    expect(html).toContain('已被取代');
    expect(html).not.toContain('· 本人操作');
  });

  it('零约束 → 显式「没有符合条件的约束」', () => {
    expect(ssr()).toContain('没有符合条件的约束');
  });

  it('403 → 「无权限查看约束」（不冒充空列表）', () => {
    const html = ssr({ forbidden: true });
    expect(html).toContain('无权限查看约束');
    expect(html).not.toContain('没有符合条件的约束');
  });

  it('错误态带原因与重试入口', () => {
    const html = ssr({ error: '加载约束失败（500）' });
    expect(html).toContain('加载失败，请重试');
    expect(html).toContain('加载约束失败（500）');
    expect(html).toContain('重试');
  });
});

describe('formatFactTime / operatorLabel', () => {
  it('坏值原样返回，不猜', () => {
    expect(formatFactTime('not-a-date')).toBe('not-a-date');
  });
  it('空值 → 空串', () => {
    expect(formatFactTime(null)).toBe('');
    expect(operatorLabel(null)).toBe('');
  });
  it('本地哨兵 local → 「本人操作」', () => {
    expect(operatorLabel('local')).toBe('本人操作');
    expect(operatorLabel('alice')).toBe('alice');
  });
});

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

/** 按钮按**精确**文本找（行内撤销按钮文案就是「撤销」/「已撤销」）。 */
const buttonByText = (text: string): HTMLButtonElement => {
  const found = [...document.querySelectorAll('button')].find(
    (b) => (b.textContent ?? '').trim() === text,
  );
  if (!found) throw new Error(`找不到按钮：${text}`);
  return found as HTMLButtonElement;
};

/** 状态筛选 tab 按**前缀**找（其文本带计数后缀，如「已撤销1」，精确匹配找不到）。 */
const tabByLabel = (label: string): HTMLButtonElement => {
  const found = [...document.querySelectorAll<HTMLButtonElement>('[role="tab"]')].find((b) =>
    (b.textContent ?? '').trim().startsWith(label),
  );
  if (!found) throw new Error(`找不到 tab：${label}`);
  return found;
};

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.mocked(listProtectedFacts).mockReset();
  vi.mocked(revokeProtectedFact).mockReset();
});

afterEach(() => {
  if (root) act(() => root!.unmount());
  container?.remove();
  container = null;
  root = null;
  document.body.innerHTML = '';
});

describe('ConstraintsTab（状态外壳）', () => {
  it('挂载即拉后端投影；撤销走确认 → 重拉对齐（不乐观本地更新）', async () => {
    let current: ProtectedFact[] = [ACTIVE];
    vi.mocked(listProtectedFacts).mockImplementation(async () => current);
    vi.mocked(revokeProtectedFact).mockImplementation(async () => {
      current = [{ ...ACTIVE, status: 'revoked', revoked_at: T, revoked_by: 'local' }];
      return { fact_id: ACTIVE.fact_id, status: 'revoked', revoked_at: T };
    });

    render(<ConstraintsTab sessionId="s-1" />);
    await act(async () => {});

    expect(listProtectedFacts).toHaveBeenCalledWith('s-1');
    expect(document.body.textContent).toContain('不要改动生产环境配置');

    // 点「撤销」→ 二次确认框（不立刻发请求）。
    act(() => buttonByText('撤销').click());
    expect(document.body.textContent).toContain('撤销这条约束？');
    expect(revokeProtectedFact).not.toHaveBeenCalled();

    // 确认 → 发请求 + 重拉（第二次 GET）。
    await act(async () => {
      buttonByText('确认撤销').click();
    });
    expect(revokeProtectedFact).toHaveBeenCalledWith('s-1', 'f-active');
    expect(listProtectedFacts).toHaveBeenCalledTimes(2);

    // 重拉后（默认「生效中」页）这条消失；「已撤销」页能看到且按钮 disabled。
    expect(document.body.textContent).not.toContain('不要改动生产环境配置');
    act(() => tabByLabel('已撤销').click());
    expect(document.body.textContent).toContain('不要改动生产环境配置');
    expect(buttonByText('已撤销').disabled).toBe(true);
  });

  it('取消确认框：不发请求、不重拉', async () => {
    vi.mocked(listProtectedFacts).mockResolvedValue([ACTIVE]);
    render(<ConstraintsTab sessionId="s-1" />);
    await act(async () => {});

    act(() => buttonByText('撤销').click());
    expect(document.body.textContent).toContain('撤销这条约束？');
    await act(async () => {
      buttonByText('取消').click();
    });
    expect(revokeProtectedFact).not.toHaveBeenCalled();
    expect(listProtectedFacts).toHaveBeenCalledTimes(1);
    expect(document.body.textContent).not.toContain('撤销这条约束？');
  });

  it('403 → 「无权限查看约束」', async () => {
    vi.mocked(listProtectedFacts).mockRejectedValue(new SessionError(403, '非本机来源'));
    render(<ConstraintsTab sessionId="s-1" />);
    await act(async () => {});
    expect(document.body.textContent).toContain('无权限查看约束');
  });

  it('加载失败 → 错误态 + 重试可重拉', async () => {
    vi.mocked(listProtectedFacts)
      .mockRejectedValueOnce(new Error('boom'))
      .mockResolvedValueOnce([ACTIVE]);
    render(<ConstraintsTab sessionId="s-1" />);
    await act(async () => {});
    expect(document.body.textContent).toContain('加载失败，请重试');

    await act(async () => {
      buttonByText('重试').click();
    });
    expect(listProtectedFacts).toHaveBeenCalledTimes(2);
    expect(document.body.textContent).toContain('不要改动生产环境配置');
  });
});
