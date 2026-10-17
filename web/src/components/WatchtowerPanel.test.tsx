// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  createWatchtowerRule,
  getWatchtower,
  type WatchtowerRuleEntry,
  type WatchtowerSnapshot,
} from '../lib/api';
import { WatchtowerPanel } from './WatchtowerPanel';

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return {
    ...actual,
    getWatchtower: vi.fn(),
    createWatchtowerRule: vi.fn(),
    actOnWatchtowerRule: vi.fn(),
    rescheduleWatchtowerRule: vi.fn(),
  };
});

let host: HTMLDivElement;
let root: Root;

function rule(overrides: Partial<WatchtowerRuleEntry> = {}): WatchtowerRuleEntry {
  return {
    rule_id: 'rule-1',
    prompt: '检查夜间构建并汇总失败原因',
    trigger_kind: 'relative',
    trigger_at: new Date(Date.now() + 3_600_000).toISOString(),
    timezone: 'Asia/Shanghai',
    local_time: null,
    delay_seconds: 3_600,
    model: null,
    sandbox_backend: 'docker',
    cwd: 'D:/repo',
    budget_turns: 20,
    budget_tokens: 64_000,
    budget_duration_seconds: 7_200,
    consent_version: 'watchtower-consent-v1',
    consented_at: new Date().toISOString(),
    state: 'active',
    version: 1,
    fired_session_id: null,
    last_error: null,
    last_notice: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    occurrence_count: 0,
    executions: [],
    ...overrides,
  };
}

function snapshot(overrides: Partial<WatchtowerSnapshot> = {}): WatchtowerSnapshot {
  return {
    scheduler: {
      running: true,
      single_instance: true,
      last_tick_at: new Date().toISOString(),
      error: null,
      consent_version: 'watchtower-consent-v1',
    },
    rules: [rule()],
    pending_verification: [{
      session_id: 'session-12345678',
      rule_id: 'rule-1',
      prompt: '检查夜间构建并汇总失败原因',
      updated_at: new Date().toISOString(),
    }],
    ...overrides,
  };
}

async function renderPanel(): Promise<void> {
  await act(async () => {
    root.render(
      <WatchtowerPanel
        onClose={() => {}}
        onOpenSession={() => {}}
        models={[{ name: 'test-model', provider: null, model: null, default: false }]}
        sandboxBackends={[
          { backend: 'local', available: true, reason: null, details: {} },
          { backend: 'docker', available: true, reason: null, details: {} },
        ]}
        defaultModel="test-model"
        defaultCwd="D:/repo"
        defaultSandboxBackend="docker"
      />,
    );
    await Promise.resolve();
  });
}

function setTextValue(element: HTMLInputElement | HTMLTextAreaElement, value: string): void {
  const prototype = element instanceof HTMLTextAreaElement
    ? HTMLTextAreaElement.prototype
    : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(prototype, 'value')?.set?.call(element, value);
  element.dispatchEvent(new Event('input', { bubbles: true }));
}

function setSelectValue(element: HTMLSelectElement, value: string): void {
  Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')?.set?.call(element, value);
  element.dispatchEvent(new Event('change', { bubbles: true }));
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  vi.mocked(getWatchtower).mockResolvedValue(snapshot());
  vi.mocked(createWatchtowerRule).mockResolvedValue({ rule: rule() });
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.mocked(getWatchtower).mockReset();
  vi.mocked(createWatchtowerRule).mockReset();
});

describe('WatchtowerPanel', () => {
  it('shows scheduler truth, a pending-verification summary, and a rule linked to its sandbox', async () => {
    await renderPanel();

    expect(document.body.textContent).toContain('调度器运行中 · 单实例');
    expect(document.body.textContent).toContain('1 个值班会话等待你验收');
    expect(document.body.textContent).toContain('检查夜间构建并汇总失败原因');
    expect(document.body.textContent).toContain('Sandbox · docker');
    expect(document.body.textContent).toContain('64,000 tokens');
  });

  it('requires consent and sends the selected model, Sandbox, cwd, and duration as a duration', async () => {
    await renderPanel();
    const openButton = [...document.body.querySelectorAll('button')]
      .find((button) => button.textContent?.includes('新建值班'));
    await act(async () => openButton?.click());

    const prompt = document.body.querySelector<HTMLTextAreaElement>('textarea');
    const duration = [...document.body.querySelectorAll('label')]
      .find((label) => label.textContent?.includes('运行时长'))
      ?.querySelector('select');
    const createButton = [...document.body.querySelectorAll('button')]
      .find((button) => button.textContent?.includes('创建值班规则'));
    expect(prompt).not.toBeNull();
    expect(duration).not.toBeNull();
    expect(createButton?.disabled).toBe(true);
    expect(document.body.textContent).toContain('只有 run 和 session 都没有绝对截止时间，且运行时确认同 run 有足够恢复预算时');
    expect(document.body.textContent).toContain('时长预算会创建持续计时的截止时间，等待请求会被拒绝');
    expect(document.body.textContent).toContain('停止受影响工作，等待你处理');
    expect(document.body.textContent).not.toContain('保持暂停');

    await act(async () => {
      setTextValue(prompt!, '到点后检查构建');
      setSelectValue(duration!, '7200');
    });
    expect(createButton?.disabled).toBe(true);

    const consent = document.body.querySelector<HTMLInputElement>('.watchtower-consent-check input');
    await act(async () => consent?.click());
    expect(createButton?.disabled).toBe(false);

    await act(async () => {
      createButton?.click();
      await Promise.resolve();
    });

    expect(createWatchtowerRule).toHaveBeenCalledWith(expect.objectContaining({
      prompt: '到点后检查构建',
      trigger_kind: 'relative',
      delay_seconds: 7_200,
      model: 'test-model',
      sandbox_backend: 'docker',
      cwd: 'D:/repo',
      budget_duration_seconds: 7_200,
      consent_acknowledged: true,
      consent_version: 'watchtower-consent-v1',
    }));
  });
});
