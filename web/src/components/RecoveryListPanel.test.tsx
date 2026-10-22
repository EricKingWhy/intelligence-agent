// @vitest-environment jsdom
/** RecoveryListPanel（#357 W-13 契约 5）—— 恢复列表页。
 *
 *  锁的是修订 A §9.4 与产品要求：
 *  ① 四要素（Task / 无终态 run / 工作目录 / 进度文件版本）如实展示；
 *  ② 进度文件缺失/不可读/不匹配如实标注，不伪造「最新」；
 *  ③ snapshot_available=false 时整页诚实降级，不展示编造列表；
 *  ④ 「先列后继续」：列表加载完成前「继续」按钮 disabled 且有原因说明
 *     （aria-disabled 如实）；
 *  ⑤ 状态不可读行默认亮 Resume（Cline fail-safe）。 */

import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { listInterruptedRecoveries, type InterruptedRecoveries } from '../lib/api';
import { RecoveryListPanel } from './RecoveryListPanel';

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return { ...actual, listInterruptedRecoveries: vi.fn() };
});

let host: HTMLDivElement;
let root: Root;
/** #1477：菜单夹具挂在 host 之外（模拟 Radix Portal 落在 body），逐个清掉。 */
let fixtures: HTMLElement[] = [];

const LOADED: InterruptedRecoveries = {
  snapshot_available: true,
  items: [
    {
      session_id: 'sess-1',
      recovery: 'needs_manual_reconcile',
      detail: '存在 UNKNOWN 操作',
      interrupted_runs: [
        { run_id: 'run-7', interrupted_seq: 12, step_id: 3, agent_id: null },
      ],
      resume_available: true,
      task: '修好登录页的对比度',
      workspace_root: '/home/me/proj',
      progress: { schema_version: '2', source_event_seq: 128 },
    },
    {
      session_id: 'sess-2',
      recovery: 'recovered',
      detail: null,
      interrupted_runs: [{ run_id: null, interrupted_seq: 4, step_id: null, agent_id: 'agent-x' }],
      resume_available: false,
      task: null,
      workspace_root: null,
      progress: { status: 'missing', reason: 'no progress file' },
    },
  ],
};

async function render(
  props: Partial<Parameters<typeof RecoveryListPanel>[0]> = {},
): Promise<{ onResume: ReturnType<typeof vi.fn> }> {
  const onResume = vi.fn();
  await act(async () => {
    root.render(
      <RecoveryListPanel open onOpenChange={() => {}} onResume={onResume} {...props} />,
    );
    await Promise.resolve();
  });
  return { onResume };
}

/** 切到关闭态，并等 Radix FocusScope 的 0ms 归还回调真正跑完。 */
async function close(): Promise<void> {
  await act(async () => {
    root.render(<RecoveryListPanel open={false} onOpenChange={() => {}} onResume={() => {}} />);
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

/** #1477：造一个 TopBar「更多工具」形状的菜单——常驻触发器 + 菜单内容。
 *  与 Radix DropdownMenu.Content 一致：内容挂 `role="menu"` 与
 *  `aria-labelledby=<触发器 id>`；菜单关闭时整段内容（含菜单项）卸载。 */
function mountMoreMenu(): { trigger: HTMLButtonElement; menu: HTMLDivElement; item: HTMLDivElement } {
  const trigger = document.createElement('button');
  trigger.id = `more-trigger-${Math.random().toString(36).slice(2)}`;
  trigger.setAttribute('aria-haspopup', 'menu');
  trigger.setAttribute('aria-label', '更多工具');
  document.body.appendChild(trigger);
  const menu = document.createElement('div');
  menu.setAttribute('role', 'menu');
  menu.setAttribute('aria-labelledby', trigger.id);
  const item = document.createElement('div');
  item.setAttribute('role', 'menuitem');
  item.tabIndex = -1;
  item.textContent = '恢复列表';
  menu.appendChild(item);
  document.body.appendChild(menu);
  fixtures.push(trigger, menu);
  return { trigger, menu, item };
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  vi.mocked(listInterruptedRecoveries).mockResolvedValue(LOADED);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  fixtures.forEach((node) => node.remove());
  fixtures = [];
  vi.mocked(listInterruptedRecoveries).mockReset();
});

describe('RecoveryListPanel', () => {
  it('加载完成前「继续」disabled 且有原因说明（先列后继续）', async () => {
    vi.mocked(listInterruptedRecoveries).mockReturnValue(new Promise(() => {}));
    await render();

    const loading = document.querySelector('.recovery-list-loading');
    expect(loading).not.toBeNull();
    const button = loading!.querySelector<HTMLButtonElement>('.recovery-resume-btn')!;
    expect(button.disabled).toBe(true);
    expect(button.getAttribute('aria-disabled')).toBe('true');
    expect(loading!.querySelector('.recovery-gate-reason')!.textContent).toContain('加载完成前');
    // 未加载完就不该有可点的行
    expect(document.querySelector('.recovery-list-item')).toBeNull();
  });

  it('四要素如实展示：Task / run / 工作目录 / 进度文件版本', async () => {
    await render();
    const items = document.querySelectorAll('.recovery-list-item');
    expect(items).toHaveLength(2);

    const first = items[0].textContent ?? '';
    expect(first).toContain('修好登录页的对比度');
    expect(first).toContain('run-7');
    expect(first).toContain('/home/me/proj');
    expect(first).toContain('2');
    expect(first).toContain('128');
  });

  it('进度文件缺失如实写「未知」，不写「最新」', async () => {
    await render();
    const second = document.querySelectorAll('.recovery-list-item')[1].textContent ?? '';
    expect(second).toContain('未知');
    expect(second).not.toContain('最新');
  });

  it('resume_available=false 的行「继续」disabled 且有原因', async () => {
    await render();
    const second = document.querySelectorAll('.recovery-list-item')[1];
    const button = second.querySelector<HTMLButtonElement>('.recovery-resume-btn')!;
    expect(button.disabled).toBe(true);
    expect(button.getAttribute('aria-disabled')).toBe('true');
    expect(second.querySelector('.recovery-gate-reason')).not.toBeNull();
  });

  it('加载完成后：可恢复行的「继续」可点，点击带 session_id 回调', async () => {
    const { onResume } = await render();
    const button = document.querySelectorAll<HTMLButtonElement>('.recovery-resume-btn')[0];
    expect(button.disabled).toBe(false);
    expect(button.getAttribute('aria-disabled')).toBe('false');
    act(() => button.click());
    expect(onResume).toHaveBeenCalledWith('sess-1');
  });

  it('disabled 的行点击不触发回调', async () => {
    const { onResume } = await render();
    const button = document.querySelectorAll<HTMLButtonElement>('.recovery-resume-btn')[1];
    act(() => button.click());
    expect(onResume).not.toHaveBeenCalled();
  });

  it('snapshot_available=false 时整页诚实降级，不展示编造列表', async () => {
    vi.mocked(listInterruptedRecoveries).mockResolvedValue({ snapshot_available: false, items: [] });
    await render();
    const degraded = document.querySelector('.recovery-list-degraded');
    expect(degraded).not.toBeNull();
    expect(degraded!.textContent).toContain('上次任务信息不可用');
    expect(document.querySelector('.recovery-list-item')).toBeNull();
    expect(degraded!.querySelector<HTMLButtonElement>('.recovery-resume-btn')?.disabled).toBe(true);
  });

  it('形状不合法（undefined）按不可用降级，不当作空列表', async () => {
    vi.mocked(listInterruptedRecoveries).mockResolvedValue(undefined);
    await render();
    expect(document.querySelector('.recovery-list-degraded')).not.toBeNull();
    expect(document.querySelector('.recovery-list-empty')).toBeNull();
  });

  it('加载失败显示错误并可重试', async () => {
    vi.mocked(listInterruptedRecoveries).mockRejectedValueOnce(new Error('boom'));
    await render();
    const error = document.querySelector('.recovery-list-error');
    expect(error).not.toBeNull();
    expect(error!.textContent).toContain('boom');

    vi.mocked(listInterruptedRecoveries).mockResolvedValue(LOADED);
    await act(async () => {
      (error!.querySelector('.recovery-list-retry') as HTMLButtonElement).click();
      await Promise.resolve();
    });
    expect(document.querySelectorAll('.recovery-list-item')).toHaveLength(2);
  });

  it('加载完成但无中断会话时如实说明空态', async () => {
    vi.mocked(listInterruptedRecoveries).mockResolvedValue({ snapshot_available: true, items: [] });
    await render();
    expect(document.querySelector('.recovery-list-empty')).not.toBeNull();
    expect(document.querySelector('.recovery-list-item')).toBeNull();
  });
});

describe('#1477 面板关闭后焦点回到常驻控件', () => {
  it('经「更多工具」菜单打开：关闭后焦点回到菜单触发器（菜单项已随菜单卸载）', async () => {
    const { trigger, menu, item } = mountMoreMenu();
    item.focus();
    expect(document.activeElement).toBe(item);

    await render();
    // 菜单在面板打开的同时关闭（Radix 在 onSelect 之后卸载内容）——记下的 opener 成了死节点。
    menu.remove();
    expect(item.isConnected).toBe(false);

    await close();

    expect(document.activeElement).toBe(trigger);
  });

  it('菜单解析不出触发器：不归焦（交回 Radix 默认，不乱归焦）', async () => {
    const { trigger, menu, item } = mountMoreMenu();
    // 有 [role=menu] 祖先但 aria-labelledby 缺失 ⇒ 解析不出常驻触发器。
    menu.removeAttribute('aria-labelledby');
    item.focus();

    await render();
    menu.remove();

    await close();

    // fail-safe：不把焦点送给「更多工具」触发器，也不造一个新的落点。
    expect(document.activeElement).not.toBe(trigger);
    expect(document.activeElement).toBe(document.body);
  });

  it('入口本身是常驻按钮：关闭后焦点仍回到该按钮（旧行为不回归）', async () => {
    const button = document.createElement('button');
    button.textContent = '恢复列表';
    document.body.appendChild(button);
    fixtures.push(button);
    button.focus();

    await render();
    await close();

    expect(document.activeElement).toBe(button);
  });
});
