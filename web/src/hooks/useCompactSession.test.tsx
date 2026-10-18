// @vitest-environment jsdom
/** #1325 S5/S9：手动压缩失败文案的 `reason` 分派。
 *
 *  只 mock `lib/api` 的 `compactSession`（网络 seam）；`SessionError` 用真类，
 *  钉住「按 status + reason 分流、不按文案子串猜」。 */

import { act, useEffect } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '../lib/api';
import { useCompactSession } from './useCompactSession';

vi.mock('../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../lib/api')>('../lib/api');
  return { ...actual, compactSession: vi.fn() };
});

const compactSession = vi.mocked(api.compactSession);

let host: HTMLDivElement;
let root: Root;
let current: ReturnType<typeof useCompactSession> | undefined;

function Harness() {
  const state = useCompactSession('s1');
  useEffect(() => {
    current = state;
  }, [state]);
  return null;
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
  current = undefined;
  vi.clearAllMocks();
});

async function runCompact() {
  await act(async () => { root.render(<Harness />); });
  await act(async () => { await current!.run(); });
}

describe('#1325 S5 — 压缩被拒按 reason 映射中文', () => {
  it('409 + no_early_turns → 中文「没有可压缩的早期内容」，不含 UUID / 英文原文', async () => {
    compactSession.mockRejectedValue(
      new api.SessionError(
        409,
        "session '9f1c2d3e-aaaa-bbbb-cccc-0123456789ab' has no compactable early turn; there is nothing to summarize yet",
        'no_early_turns',
      ),
    );
    await runCompact();
    expect(current?.compactError).toBe('没有可压缩的早期内容');
  });

  it('其余 409（run_busy）保持后端可读文案，不被误映射', async () => {
    compactSession.mockRejectedValue(
      new api.SessionError(409, 'session has a run in flight; compact it after the run finishes', 'run_busy'),
    );
    await runCompact();
    expect(current?.compactError).toBe('session has a run in flight; compact it after the run finishes');
  });

  it('对象形 detail 但无 reason → 用后端 message（不猜成因）', async () => {
    compactSession.mockRejectedValue(new api.SessionError(409, '压缩已在进行中'));
    await runCompact();
    expect(current?.compactError).toBe('压缩已在进行中');
  });

  it('非 409 的错误即便带同名 reason 也不被改写成「没得压」', async () => {
    compactSession.mockRejectedValue(
      new api.SessionError(500, 'internal error while compacting', 'no_early_turns'),
    );
    await runCompact();
    expect(current?.compactError).toBe('internal error while compacting');
  });

  it('非 SessionError（网络层）→ 沿用 describeSessionError 口径', async () => {
    compactSession.mockRejectedValue(new Error('网络断了'));
    await runCompact();
    expect(current?.compactError).toBe('网络断了');
  });
});
