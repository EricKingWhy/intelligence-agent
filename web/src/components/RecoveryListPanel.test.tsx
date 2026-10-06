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
    expect(degraded!.textContent).toContain('上次运行信息不可用');
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
