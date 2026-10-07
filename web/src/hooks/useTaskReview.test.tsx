// @vitest-environment jsdom
/** #353 W-09：任务审阅数据 hook 的契约测试。
 *
 *  只 mock `lib/api` 这一层 seam（不 mock 组件内部、不 mock fetch 细节）。钉住的是
 *  不变量 #22 相关的三条：状态一律来自服务端投影、刷新/重连后重新拉取、
 *  前端**不写** localStorage 业务事实。 */

import { act, useEffect } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '../lib/api';
import { useTaskReview, type TaskReviewState } from './useTaskReview';

vi.mock('../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../lib/api')>('../lib/api');
  return {
    ...actual,
    getTaskState: vi.fn(),
    getEvidenceState: vi.fn(),
    getWorkspaceGitStatus: vi.fn(),
    getWorkspaceGitDiff: vi.fn(),
    acceptTask: vi.fn(),
    releaseTaskAcceptance: vi.fn(),
    releaseTaskLease: vi.fn(),
  };
});

const getTaskState = vi.mocked(api.getTaskState);
const getEvidenceState = vi.mocked(api.getEvidenceState);
const getWorkspaceGitStatus = vi.mocked(api.getWorkspaceGitStatus);
const getWorkspaceGitDiff = vi.mocked(api.getWorkspaceGitDiff);
const acceptTask = vi.mocked(api.acceptTask);
const releaseTaskAcceptance = vi.mocked(api.releaseTaskAcceptance);
const releaseTaskLease = vi.mocked(api.releaseTaskLease);

function taskState(over: Partial<api.TaskState> = {}): api.TaskState {
  return {
    defined: true,
    task_text: '把 CSV 导入写对',
    read_write_intent: '拟写入',
    cwd: '/repo',
    authorization: null,
    criteria: [{ item_id: 'ac-1', text: '导入去重', origin: 'user', confirmed: true }],
    verification: { 'ac-1': { value: 'passed', evidence: null } },
    acceptance: null,
    version: 1,
    open_run_ids: [],
    product_state: 'deliverable',
    ...over,
  };
}

let host: HTMLDivElement;
let root: Root;
let current: TaskReviewState | undefined;
let sessionId = 's1';

function Harness() {
  const state = useTaskReview(sessionId);
  useEffect(() => {
    current = state;
  }, [state]);
  return null;
}

const okGit = { exit_code: 0, stdout: '', stderr: '', artifact_ref: null };

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  sessionId = 's1';
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  getTaskState.mockResolvedValue(taskState());
  getEvidenceState.mockResolvedValue({});
  getWorkspaceGitStatus.mockResolvedValue(okGit);
  getWorkspaceGitDiff.mockResolvedValue(okGit);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  current = undefined;
  vi.clearAllMocks();
  vi.unstubAllGlobals();
  Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
});

async function mount() {
  await act(async () => { root.render(<Harness />); });
  await act(async () => {});
}

describe('#353 useTaskReview — 服务端投影加载', () => {
  it('挂载时拉 task + evidence + git status + git diff', async () => {
    await mount();
    expect(getTaskState).toHaveBeenCalledWith('s1');
    expect(getEvidenceState).toHaveBeenCalledWith('s1');
    expect(getWorkspaceGitStatus).toHaveBeenCalledWith('s1');
    expect(getWorkspaceGitDiff).toHaveBeenCalledWith('s1');
    expect(current?.task?.product_state).toBe('deliverable');
    expect(current?.loading).toBe(false);
  });

  it('404（未定义任务）→ notDefined=true，不是错误横幅', async () => {
    getTaskState.mockRejectedValue(new api.TaskNotDefinedError('未定义任务'));
    await mount();
    expect(current?.notDefined).toBe(true);
    expect(current?.error).toBeNull();
    expect(current?.task).toBeNull();
  });

  it('不写 localStorage 业务事实（不变量 #22）', async () => {
    const setItem = vi.fn();
    vi.stubGlobal('localStorage', { setItem, getItem: vi.fn(), removeItem: vi.fn() });
    await mount();
    expect(setItem).not.toHaveBeenCalled();
  });
});

describe('#353 useTaskReview — 刷新 / 重连重新拉取', () => {
  it('窗口隐藏再显示（visibilitychange→visible）重新拉取', async () => {
    await mount();
    getTaskState.mockClear();
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')); });
    expect(getTaskState).toHaveBeenCalledTimes(1);
  });

  it('断线重连（window online）重新拉取', async () => {
    await mount();
    getTaskState.mockClear();
    await act(async () => { window.dispatchEvent(new Event('online')); });
    expect(getTaskState).toHaveBeenCalledTimes(1);
  });

  it('refresh() 重新拉取', async () => {
    await mount();
    getTaskState.mockClear();
    await act(async () => { await current!.refresh(); });
    expect(getTaskState).toHaveBeenCalledTimes(1);
  });
});

describe('#353 useTaskReview — 三操作', () => {
  it('accept：用当前 version 发 CAS；成功后刷新', async () => {
    acceptTask.mockResolvedValue(taskState({ acceptance: { decision: 'accepted', reason: null }, version: 2, product_state: 'accepted' }));
    await mount();
    await act(async () => { await current!.accept(); });
    expect(acceptTask).toHaveBeenCalledWith('s1', { decision: 'accepted', expected_version: 1 });
    expect(current?.operationNotice).toContain('已接受');
  });

  it('accept：409 且最新投影已接受 → 幂等成功（不报错吓人）', async () => {
    await mount();
    acceptTask.mockRejectedValue(new api.TaskReviewRequestError(409, '已存在接受事实（不能双写）'));
    getTaskState.mockResolvedValue(taskState({ acceptance: { decision: 'accepted', reason: null }, version: 2, product_state: 'accepted' }));
    await act(async () => { await current!.accept(); });
    expect(current?.operationError).toBeNull();
    expect(current?.operationNotice).toContain('幂等');
  });

  it('acceptWithGaps：reason 为空 → 客户端先校验，不发请求', async () => {
    await mount();
    await act(async () => { await current!.acceptWithGaps('   '); });
    expect(acceptTask).not.toHaveBeenCalled();
    expect(current?.operationError).toContain('必须填写原因');
  });

  it('acceptWithGaps：有 reason → 发 accepted_with_gaps，服务端 422 原样展示', async () => {
    await mount();
    acceptTask.mockRejectedValue(new api.TaskReviewRequestError(422, 'reason 超过长度上限'));
    await act(async () => { await current!.acceptWithGaps('缺一项没跑'); });
    expect(acceptTask).toHaveBeenCalledWith('s1', {
      decision: 'accepted_with_gaps', reason: '缺一项没跑', expected_version: 1,
    });
    expect(current?.operationError).toContain('长度上限');
  });

  it('releaseAcceptance：已接受时用当前 version 撤销', async () => {
    getTaskState.mockResolvedValue(taskState({ acceptance: { decision: 'accepted', reason: null }, version: 5, product_state: 'accepted' }));
    releaseTaskAcceptance.mockResolvedValue(taskState({ acceptance: null, version: 6 }));
    await mount();
    await act(async () => { await current!.releaseAcceptance(); });
    expect(releaseTaskAcceptance).toHaveBeenCalledWith('s1', { expected_version: 5 });
    expect(current?.operationNotice).toContain('撤销');
  });

  it('releaseLease：released=false 如实说"未释放（非持有者）"，不冒充成功', async () => {
    await mount();
    releaseTaskLease.mockResolvedValue({ released: false, promoted_to: null });
    await act(async () => { await current!.releaseLease(); });
    expect(current?.leaseNotice).toContain('未释放');
    expect(current?.leaseNotice).toContain('不是该目录的写租约持有者');
  });

  it('releaseLease：released=true 且提升队首时如实报告', async () => {
    await mount();
    releaseTaskLease.mockResolvedValue({ released: true, promoted_to: 's9' });
    await act(async () => { await current!.releaseLease(); });
    expect(current?.leaseNotice).toContain('已释放');
    expect(current?.leaseNotice).toContain('s9');
  });
});

describe('#353 P1-1 — 证据加载失败如实呈现（不可得≠缺证据）', () => {
  it('getEvidenceState 抛错 → evidenceError 非空，evidence 保持空对象', async () => {
    getEvidenceState.mockRejectedValue(new Error('boom'));
    await mount();
    expect(current?.evidenceError).toContain('boom');
    expect(current?.evidence).toEqual({});
    // 任务本身加载成功，不判成整体加载失败
    expect(current?.error).toBeNull();
    expect(current?.task).not.toBeNull();
  });

  it('证据恢复成功后 evidenceError 清零', async () => {
    getEvidenceState.mockRejectedValueOnce(new Error('boom'));
    await mount();
    expect(current?.evidenceError).toContain('boom');
    await act(async () => { await current!.refresh(); });
    expect(current?.evidenceError).toBeNull();
  });
});
