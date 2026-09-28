// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryActivity } from './MemoryActivity';
import { applyEvent, initConversation } from '../lib/projection';
import type { AgentEvent, ConversationState } from '../types';

let host: HTMLDivElement;
let root: Root;

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function render(conversation: ConversationState) {
  act(() => root.render(<MemoryActivity conversation={conversation} />));
}

function event(type: string, seq: number, data: Record<string, unknown>, runId = 'run-1'): AgentEvent {
  return { type, seq, data, run_id: runId, time: '2026-09-25T10:00:00Z' };
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
  vi.unstubAllGlobals();
});

describe('MemoryActivity disclosure', () => {
  it('shows only a committed update count until explicit expansion, then reads the current record', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({
      id: 'm-1', root_id: 'r-1', version: 1, content: 'private current memory',
      scope: 'user_global', tier: 'profile', status: 'active', kind: 'semantic', project_id: null,
      source_type: 'automatic', source_session_id: null, source_event_ids: [],
      payload: { kind: 'semantic', subject: 'u', fact: 'f', category: 'profile' },
      metadata: {}, created_at: '2026-09-25T10:00:00Z',
    }));
    vi.stubGlobal('fetch', fetchMock);
    const conversation = applyEvent(initConversation('s-1'), event('memory/updated', 1, {
      count: 1, memory_ids: ['m-1'],
    }));

    render(conversation);

    expect(host.textContent).toContain('已更新 1 条记忆');
    expect(host.textContent).not.toContain('private current memory');
    expect(fetchMock).not.toHaveBeenCalled();

    const toggle = host.querySelector('button')!;
    await act(async () => { toggle.click(); await Promise.resolve(); });
    expect(String(fetchMock.mock.calls[0][0])).toBe('/api/memories/m-1');
    expect(host.textContent).toContain('private current memory');
  });

  it('resolves a project-scoped update through the project trusted by its session', async () => {
    const projectRecord = {
      id: 'm-project', root_id: 'r-project', version: 1, content: 'project memory',
      scope: 'project', tier: 'collection', status: 'active', kind: 'episodic', project_id: 'p-1',
      source_type: 'automatic', source_session_id: 's-1', source_event_ids: [],
      payload: { kind: 'episodic', situation: 's', action: 'a', outcome: 'o', lesson: 'l' },
      metadata: {}, created_at: '2026-09-25T10:00:00Z',
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ detail: 'not found' }, 404))
      .mockResolvedValueOnce(response([{
        id: 'p-1', path: 'C:/project', title: 'Project One', status: 'ok',
        session_ids: ['s-1'], created_at: '', updated_at: '',
      }]))
      .mockResolvedValueOnce(response(projectRecord));
    vi.stubGlobal('fetch', fetchMock);
    const conversation = applyEvent(initConversation('s-1'), event('memory/updated', 1, {
      count: 1, memory_ids: ['m-project'],
    }));

    render(conversation);
    await act(async () => {
      host.querySelector('button')!.click();
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(String(fetchMock.mock.calls[0][0])).toBe('/api/memories/m-project');
    expect(String(fetchMock.mock.calls[1][0])).toBe('/api/projects');
    expect(new URL(String(fetchMock.mock.calls[2][0]), 'http://localhost').searchParams.get('project_id')).toBe('p-1');
    expect(host.textContent).toContain('project memory');
  });

  it('loads recall details only when expanded and renders allowlisted redacted ranking fields', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response([{
      run_id: 'run-1', seq: 2, time: '2026-09-25T10:00:00Z', memories: [{
        memory_id: 'm-1', kind: 'semantic', scope: 'user_global', source_type: 'automatic',
        version: 3, source_session_id: 'source-session', source_event_ids: ['evt-1'],
        ranking: { dense: 0.8, score: 0.9, raw_evidence: 'never show this' },
        content: 'do not render raw memory body',
      }],
    }]));
    vi.stubGlobal('fetch', fetchMock);
    const conversation = applyEvent(initConversation('s-1'), event('memory/recalled', 2, {}));

    render(conversation);

    expect(host.textContent).toContain('记忆召回说明');
    expect(fetchMock).not.toHaveBeenCalled();
    await act(async () => { host.querySelector('button')!.click(); await Promise.resolve(); });
    expect(String(fetchMock.mock.calls[0][0])).toBe('/api/sessions/s-1/memory-recalls');
    expect(host.textContent).toContain('dense');
    expect(host.textContent).toContain('0.8');
    expect(host.textContent).not.toContain('never show this');
    expect(host.textContent).not.toContain('do not render raw memory body');
  });

  it('omits update events with zero or malformed counts', () => {
    let conversation = initConversation('s-1');
    conversation = applyEvent(conversation, event('memory/updated', 1, { count: 0, memory_ids: [] }));
    conversation = applyEvent(conversation, event('memory/updated', 2, { count: '2', memory_ids: [] }));
    render(conversation);
    expect(host.textContent).not.toContain('已更新');
  });
});
