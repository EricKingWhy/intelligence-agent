// @vitest-environment jsdom
/** ConstraintsTab / ConstraintsView（#1190 Phase 1 / #1191 Phase 2）—— 约束管理页。
 *
 *  锁的是 issue §四/§五与不变量 #22（UI 不做第二真相）：
 *  ① 纯展示视图 `ConstraintsView` 由数据 props 驱动——状态筛选默认「生效中」、
 *     revoked/superseded 归「已撤销」页、类型下拉纯展示层过滤；
 *  ② 有状态外壳 `ConstraintsTab` 挂载即拉后端投影，**撤销后重拉对齐**（不做乐观
 *     本地更新）——真相在后端；
 *  ③ 空态 / 无权限（403）如实说，不冒充空列表；
 *  ④ 时间/操作人展示：坏值原样、本地哨兵 `local` → 「本人操作」。
 *
 *  #1191 增量：三组按 `scope` 分组（SESSION/PROJECT/GLOBAL）+ 顶部优先级条；
 *  项目组带 `git 根 · label`；被更高优先级覆盖的条目加「被本会话覆盖」；
 *  非 git（`project === null`）不渲染 PROJECT 组；撤销按 scope 分派端点。 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { renderToString } from 'react-dom/server';
import {
  SessionError,
  listEffectiveConstraints,
  revokeProtectedFact,
  revokeScopedConstraint,
  type EffectiveConstraintRow,
  type EffectiveProjectMeta,
} from '../lib/api';
import { formatFactTime, operatorLabel } from '../lib/constraints';
import { formatDateTime } from '../lib/format';
import { ConstraintsTab, ConstraintsView } from './ConstraintsTab';

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return {
    ...actual,
    listEffectiveConstraints: vi.fn(),
    revokeProtectedFact: vi.fn(),
    revokeScopedConstraint: vi.fn(),
  };
});

const T = '2026-09-07T00:00:00Z';

const ACTIVE: EffectiveConstraintRow = {
  fact_id: 'f-active',
  type: 'constraint',
  value: '不要改动生产环境配置',
  status: 'active',
  source_event_id: 'ev-3',
  source_seq: 3,
  registered_at: T,
  revoked_at: null,
  revoked_by: null,
  scope: 'session',
  overridden_by: null,
};

const REVOKED: EffectiveConstraintRow = {
  fact_id: 'f-revoked',
  type: 'user_goal',
  value: '先修好登录页对比度',
  status: 'revoked',
  source_event_id: 'ev-4',
  source_seq: 4,
  registered_at: T,
  revoked_at: T,
  revoked_by: 'local',
  scope: 'session',
  overridden_by: null,
};

const SUPERSEDED: EffectiveConstraintRow = {
  fact_id: 'f-superseded',
  type: 'authorization',
  value: '允许写 workspace',
  status: 'superseded',
  source_event_id: 'ev-5',
  source_seq: 5,
  registered_at: T,
  revoked_at: null,
  revoked_by: null,
  scope: 'session',
  overridden_by: null,
};

const PROJECT_ROW: EffectiveConstraintRow = {
  fact_id: 'prj-1',
  type: 'constraint',
  value: '所有 Python 改动先写 failing test',
  status: 'active',
  source_event_id: null,
  source_seq: null,
  registered_at: T,
  revoked_at: null,
  revoked_by: null,
  scope: 'project',
  overridden_by: null,
};

const GLOBAL_ROW: EffectiveConstraintRow = {
  fact_id: 'glb-1',
  type: 'constraint',
  value: '和我说话温柔一点',
  status: 'active',
  source_event_id: null,
  source_seq: null,
  registered_at: T,
  revoked_at: null,
  revoked_by: null,
  scope: 'global',
  overridden_by: null,
};

const PROJECT_META: EffectiveProjectMeta = { label: 'EricKingWhy/intelligence-agent' };

const viewBase = {
  sessionId: 's-1',
  facts: [] as EffectiveConstraintRow[],
  project: null as EffectiveProjectMeta | null,
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

/** SSR 会在相邻文本节点间插 `<!-- -->`（如 `1<!-- --> 条生效中`）：跨节点断言前先剥掉，
 *  否则按整句匹配必假红。 */
const plain = (html: string): string => html.replace(/<!-- -->/g, '');

describe('ConstraintsView（纯展示，SSR）', () => {
  it('页眉与标题常驻（不因空/错而消失）', () => {
    const html = ssr();
    expect(html).toContain('cn-eyebrow');
    expect(html).toContain('我的要求');
    expect(html).toContain('s-1');
  });

  it('#1191：顶部优先级条逐字在场', () => {
    const html = ssr();
    expect(html).toContain('同样内容的要求，生效优先级：会话＞项目＞全局');
    expect(html).toContain('不同内容会全部提供给 AI，由它决定怎么处理。');
    expect(html).toContain('标记的条目不生效。撤销后仍保留历史记录。');
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

  it('零约束 → 显式「没有符合条件的要求」', () => {
    expect(ssr()).toContain('没有符合条件的要求');
  });

  it('403 → 「无权限查看要求」（不冒充空列表）', () => {
    const html = ssr({ forbidden: true });
    expect(html).toContain('无权限查看要求');
    expect(html).not.toContain('没有符合条件的要求');
  });

  it('错误态带原因与重试入口', () => {
    const html = ssr({ error: '加载约束失败（500）' });
    expect(html).toContain('加载失败，请重试');
    expect(html).toContain('加载约束失败（500）');
    expect(html).toContain('重试');
  });

  it('AC4：错误态**不展示旧数据**（即使 props 里还带着 facts）', () => {
    const html = ssr({ facts: [ACTIVE], error: '加载约束失败（500）' });
    expect(html).toContain('加载失败，请重试');
    expect(html).not.toContain('不要改动生产环境配置');
  });

  it('#1191：三组眉标 + 计数（SESSION/PROJECT/GLOBAL）', () => {
    const html = ssr({
      facts: [ACTIVE, PROJECT_ROW, GLOBAL_ROW],
      project: PROJECT_META,
    });
    expect(html).toContain('SESSION');
    expect(html).toContain('本会话');
    expect(html).toContain('PROJECT');
    expect(html).toContain('本项目');
    expect(html).toContain('GLOBAL');
    expect(html).toContain('我的全局');
    // 每组 1 条生效中。
    expect((plain(html).match(/1 条生效中/g) ?? []).length).toBe(3);
  });

  it('#1191：PROJECT 组带 `git 根 · owner/repo`', () => {
    const html = ssr({ facts: [PROJECT_ROW], project: PROJECT_META });
    expect(plain(html)).toContain('git 根 · EricKingWhy/intelligence-agent');
  });

  it('#1191：非 git（project=null）不渲染 PROJECT 组', () => {
    const html = ssr({ facts: [ACTIVE, GLOBAL_ROW], project: null });
    expect(html).not.toContain('PROJECT');
    expect(html).not.toContain('本项目');
    expect(html).toContain('SESSION');
    expect(html).toContain('GLOBAL');
  });

  it('#1191：被覆盖条目加「被本会话覆盖」且**不计入**「N 条生效中」', () => {
    const overridden: EffectiveConstraintRow = {
      ...PROJECT_ROW,
      fact_id: 'prj-covered',
      overridden_by: 'f-active',
    };
    const html = ssr({ facts: [ACTIVE, overridden, GLOBAL_ROW], project: PROJECT_META });
    expect(html).toContain('被本会话覆盖');
    // 被覆盖 ⇒ 不注入 ⇒ 不算「生效中」（审查 finding C7：旧实现按 status==='active'
    // 计数会把这条算进去，与优先级条「overridden_by 非 null 的条目不注入」自相矛盾）。
    // SESSION 1 + GLOBAL 1 生效；PROJECT 只剩那条被覆盖的 ⇒ 0。
    const text = plain(html);
    expect((text.match(/0 条生效中/g) ?? []).length).toBe(1); // PROJECT：被覆盖不计
  });
});

describe('formatFactTime / operatorLabel', () => {
  it('坏值原样返回，不猜', () => {
    expect(formatFactTime('not-a-date')).toBe('not-a-date');
  });
  it('合法 ISO → 全站统一 YYYY-MM-DD HH:mm（本地时区）', () => {
    const iso = '2026-09-07T00:05:00Z';
    expect(formatFactTime(iso)).toBe(formatDateTime(iso));
    expect(formatFactTime(iso)).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
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

/** tab 上的计数（`.cn-tab-count`）——它由**内存态 `facts`** 算出，故是「旧数据是否
 *  真被清空」的唯一可靠断言口：渲染分支互斥会让旧值在 DOM 里本就不出现，
 *  只查 DOM 的断言对 `setFacts([])` 无区分度（假绿）。 */
const tabCount = (label: string): string =>
  (tabByLabel(label).querySelector('.cn-tab-count')?.textContent ?? '').trim();

const page = (facts: EffectiveConstraintRow[], project: EffectiveProjectMeta | null = null) => ({
  facts,
  project,
});

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.mocked(listEffectiveConstraints).mockReset();
  vi.mocked(revokeProtectedFact).mockReset();
  vi.mocked(revokeScopedConstraint).mockReset();
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
    let current: EffectiveConstraintRow[] = [ACTIVE];
    vi.mocked(listEffectiveConstraints).mockImplementation(async () => page(current));
    vi.mocked(revokeProtectedFact).mockImplementation(async () => {
      current = [{ ...ACTIVE, status: 'revoked', revoked_at: T, revoked_by: 'local' }];
      return { fact_id: ACTIVE.fact_id, status: 'revoked', revoked_at: T };
    });

    render(<ConstraintsTab sessionId="s-1" />);
    await act(async () => {});

    expect(listEffectiveConstraints).toHaveBeenCalledWith('s-1');
    expect(document.body.textContent).toContain('不要改动生产环境配置');

    // 点「撤销」→ 二次确认框（不立刻发请求）。
    act(() => buttonByText('撤销').click());
    expect(document.body.textContent).toContain('撤销这条要求？');
    expect(revokeProtectedFact).not.toHaveBeenCalled();

    // 确认 → 发请求 + 重拉（第二次 GET）。
    await act(async () => {
      buttonByText('确认撤销').click();
    });
    expect(revokeProtectedFact).toHaveBeenCalledWith('s-1', 'f-active');
    expect(listEffectiveConstraints).toHaveBeenCalledTimes(2);

    // 重拉后（默认「生效中」页）这条消失；「已撤销」页能看到且按钮 disabled。
    expect(document.body.textContent).not.toContain('不要改动生产环境配置');
    act(() => tabByLabel('已撤销').click());
    expect(document.body.textContent).toContain('不要改动生产环境配置');
    expect(buttonByText('已撤销').disabled).toBe(true);
  });

  it('#1191：项目级撤销走规则层端点（revokeScopedConstraint），确认框明示影响面', async () => {
    vi.mocked(listEffectiveConstraints).mockResolvedValue(
      page([PROJECT_ROW], PROJECT_META),
    );
    vi.mocked(revokeScopedConstraint).mockResolvedValue({
      fact_id: PROJECT_ROW.fact_id,
      status: 'revoked',
      revoked_at: T,
    });

    render(<ConstraintsTab sessionId="s-1" />);
    await act(async () => {});

    act(() => buttonByText('撤销').click());
    // 二次确认必须明示影响面（票面 §六.3）。
    expect(document.body.textContent).toContain('将影响本仓库所有会话');
    await act(async () => {
      buttonByText('确认撤销').click();
    });
    expect(revokeScopedConstraint).toHaveBeenCalledWith('prj-1', {
      scope: 'project',
      sessionId: 's-1',
    });
    expect(revokeProtectedFact).not.toHaveBeenCalled();
  });

  it('取消确认框：不发请求、不重拉', async () => {
    vi.mocked(listEffectiveConstraints).mockResolvedValue(page([ACTIVE]));
    render(<ConstraintsTab sessionId="s-1" />);
    await act(async () => {});

    act(() => buttonByText('撤销').click());
    expect(document.body.textContent).toContain('撤销这条要求？');
    await act(async () => {
      buttonByText('取消').click();
    });
    expect(revokeProtectedFact).not.toHaveBeenCalled();
    expect(listEffectiveConstraints).toHaveBeenCalledTimes(1);
    expect(document.body.textContent).not.toContain('撤销这条要求？');
  });

  it('403 → 「无权限查看要求」', async () => {
    vi.mocked(listEffectiveConstraints).mockRejectedValue(new SessionError(403, '非本机来源'));
    render(<ConstraintsTab sessionId="s-1" />);
    await act(async () => {});
    expect(document.body.textContent).toContain('无权限查看要求');
  });

  it('AC4：先有数据、后拉取失败 → 清空旧数据不说谎', async () => {
    vi.mocked(listEffectiveConstraints).mockImplementation(async (sid: string) => {
      if (sid === 's-ok') return page([ACTIVE]);
      throw new Error('boom');
    });
    render(<ConstraintsTab sessionId="s-ok" />);
    await act(async () => {});
    expect(document.body.textContent).toContain('不要改动生产环境配置');
    expect(tabCount('全部')).toBe('1');

    // 换会话 → 触发重新拉取并失败：旧会话的数据必须消失（不许用缓存冒充）。
    await act(async () => {
      root!.render(<ConstraintsTab sessionId="s-bad" />);
    });
    expect(document.body.textContent).toContain('加载失败，请重试');
    expect(document.body.textContent).not.toContain('不要改动生产环境配置');
    // 内存态也必须清空：tab 计数由 `facts` 算出（渲染分支互斥会让旧值本就不进 DOM，
    // 只查 DOM 的断言对 `setFacts([])` 无区分度）。
    expect(tabCount('全部')).toBe('0');
  });

  it('AC4：先有数据、后 403 → 同样清空旧数据（内存态不留上一会话行）', async () => {
    vi.mocked(listEffectiveConstraints).mockImplementation(async (sid: string) => {
      if (sid === 's-ok') return page([ACTIVE]);
      throw new SessionError(403, '非本机来源');
    });
    render(<ConstraintsTab sessionId="s-ok" />);
    await act(async () => {});
    expect(document.body.textContent).toContain('不要改动生产环境配置');
    expect(tabCount('全部')).toBe('1');

    await act(async () => {
      root!.render(<ConstraintsTab sessionId="s-forbidden" />);
    });
    expect(document.body.textContent).toContain('无权限查看要求');
    expect(document.body.textContent).not.toContain('不要改动生产环境配置');
    expect(tabCount('全部')).toBe('0');
  });

  it('加载失败 → 错误态 + 重试可重拉', async () => {
    vi.mocked(listEffectiveConstraints)
      .mockRejectedValueOnce(new Error('boom'))
      .mockResolvedValueOnce(page([ACTIVE]));
    render(<ConstraintsTab sessionId="s-1" />);
    await act(async () => {});
    expect(document.body.textContent).toContain('加载失败，请重试');

    await act(async () => {
      buttonByText('重试').click();
    });
    expect(listEffectiveConstraints).toHaveBeenCalledTimes(2);
    expect(document.body.textContent).toContain('不要改动生产环境配置');
  });

  it('AC4：切换会话 → 新 GET 落定前不显示上一会话的旧行（无陈旧窗口）', async () => {
    let resolveSecond: ((rows: ReturnType<typeof page>) => void) | null = null;
    vi.mocked(listEffectiveConstraints).mockImplementation((sid: string) => {
      if (sid === 's-old') return Promise.resolve(page([ACTIVE]));
      return new Promise((res) => {
        resolveSecond = res;
      });
    });
    render(<ConstraintsTab sessionId="s-old" />);
    await act(async () => {});
    expect(tabCount('全部')).toBe('1');

    // 切会话 → 第二个 GET 悬而未决：旧行与旧计数必须**已**消失（不是等它落定才清）。
    await act(async () => {
      root!.render(<ConstraintsTab sessionId="s-new" />);
    });
    expect(tabCount('全部')).toBe('0');
    expect(document.body.textContent).not.toContain('不要改动生产环境配置');

    // 收尾：让挂起的请求落定。
    await act(async () => {
      resolveSecond?.(page([ACTIVE]));
    });
  });

  it('重试：错误文案在重试开始时即清除（不残留上一次错误）', async () => {
    vi.mocked(listEffectiveConstraints)
      .mockRejectedValueOnce(new Error('boom'))
      .mockImplementation(() => new Promise(() => {}));
    render(<ConstraintsTab sessionId="s-1" />);
    await act(async () => {});
    expect(document.body.textContent).toContain('加载失败，请重试');

    // 重试在途（挂起）→ 上一次的错误文案不该还挂着，应回到加载态。
    await act(async () => {
      buttonByText('重试').click();
    });
    expect(document.body.textContent).not.toContain('加载失败，请重试');
    expect(document.body.textContent).toContain('正在加载要求…');
  });
});
