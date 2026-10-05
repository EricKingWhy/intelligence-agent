// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  listApprovePolicyRules,
  revokeApprovePolicyRule,
  type ApprovePolicyRule,
} from '../lib/api';
import { ApprovePolicyPanel } from './ApprovePolicyPanel';

vi.mock('../lib/api', () => ({
  listApprovePolicyRules: vi.fn(),
  revokeApprovePolicyRule: vi.fn(),
}));

let host: HTMLDivElement;
let root: Root;

function rule(overrides: Partial<ApprovePolicyRule> = {}): ApprovePolicyRule {
  return {
    id: 'abcdef0123456789',
    tool: 'bash',
    key: 'npm run build',
    granularity: 'command',
    permission_at_approval: 'danger',
    created_at: '2026-10-05T12:00:00+00:00',
    ...overrides,
  };
}

async function renderPanel(): Promise<void> {
  await act(async () => {
    root.render(<ApprovePolicyPanel open onOpenChange={() => {}} />);
    await Promise.resolve();
  });
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  vi.mocked(listApprovePolicyRules).mockResolvedValue([rule()]);
  vi.mocked(revokeApprovePolicyRule).mockResolvedValue({ id: rule().id, revoked: true });
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.mocked(listApprovePolicyRules).mockReset();
  vi.mocked(revokeApprovePolicyRule).mockReset();
});

describe('ApprovePolicyPanel', () => {
  it('renders the persistent rules with tool/key/granularity', async () => {
    await renderPanel();

    expect(document.body.querySelector('.approve-policy-tool')?.textContent).toBe('bash');
    expect(document.body.querySelector('.approve-policy-key')?.textContent).toBe('npm run build');
    expect(document.body.querySelector('.approve-policy-granularity')?.textContent).toContain('命令级');
  });

  it('shows a friendly empty state when there are no rules', async () => {
    vi.mocked(listApprovePolicyRules).mockResolvedValue([]);
    await renderPanel();

    const empty = document.body.querySelector('.approve-policy-empty');
    expect(empty?.textContent).toContain('暂无持久审批规则');
    expect(document.body.querySelector('.approve-policy-row')).toBeNull();
  });

  it('requires explicit confirmation before revoking (F21)', async () => {
    await renderPanel();

    // 第一次点击「撤销」只进入确认态，绝不调用后端。
    const revokeBtn = document.body.querySelector<HTMLButtonElement>('.approve-policy-actions button');
    act(() => revokeBtn!.click());
    expect(revokeApprovePolicyRule).not.toHaveBeenCalled();
    expect(document.body.querySelector('.approve-policy-confirm')).not.toBeNull();

    // 第二次点「确认撤销」才真调用，并从列表移除。
    const confirmBtn = document.body.querySelector<HTMLButtonElement>('.approve-policy-danger');
    await act(async () => {
      confirmBtn!.click();
      await Promise.resolve();
    });
    expect(revokeApprovePolicyRule).toHaveBeenCalledWith('abcdef0123456789');
    expect(document.body.querySelector('.approve-policy-row')).toBeNull();
  });

  it('surfaces a load error', async () => {
    vi.mocked(listApprovePolicyRules).mockRejectedValue(new Error('boom'));
    await renderPanel();

    expect(document.body.querySelector('.approve-policy-error')?.textContent).toContain('boom');
  });
});
