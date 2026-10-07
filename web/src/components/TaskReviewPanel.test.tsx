// @vitest-environment jsdom
/** #353 W-09：任务审阅面板的组件契约测试（六态 + 消歧 + 三操作 + 两轴 + diff）。
 *
 *  只 mock `lib/api` seam；断言落在**可见文本与交互**上，不碰组件内部实现。
 *  核心是守两条不变量：① chip 文案由服务端 `product_state` 决定，前端**不改**它
 *  （可交付 + 证据已过期并存是诚实呈现）；② 前端不产生业务结论。 */

import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '../lib/api';
import { TaskReviewPanel } from './TaskReviewPanel';

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
const releaseTaskLease = vi.mocked(api.releaseTaskLease);

function criterion(id: string, text: string, origin: 'user' | 'agent' = 'user') {
  return { item_id: id, text, origin, confirmed: origin === 'user' };
}

function taskState(over: Partial<api.TaskState> = {}): api.TaskState {
  return {
    defined: true,
    task_text: '把 CSV 导入写对',
    read_write_intent: '拟写入',
    cwd: '/repo',
    authorization: null,
    criteria: [criterion('ac-1', '导入去重')],
    verification: { 'ac-1': { value: 'passed', evidence: null } },
    acceptance: null,
    version: 1,
    open_run_ids: [],
    product_state: 'deliverable',
    ...over,
  };
}

function evidenceRecord(over: Partial<api.EvidenceRecord> = {}): api.EvidenceRecord {
  return {
    evidence_id: 'ev-1',
    task_session_id: 's1',
    run_id: 'r1',
    criterion_id: 'ac-1',
    kind: 'test',
    source_event_seq: 3,
    tool_call_id: null,
    captured_at: '2026-10-06T00:00:00Z',
    result: 'pass',
    command_or_action: 'pytest tests/x.py',
    exit_code_or_observation: 0,
    artifact_ref: null,
    base_head: null,
    workspace_manifest: { files: [{ path: 'src/a.py', sha256: 'a'.repeat(64) }], manifest_hash: 'b'.repeat(64), progress_md_sha256: null },
    freshness: { status: 'fresh', reasons: [] },
    ...over,
  };
}

let host: HTMLDivElement;
let root: Root;
let onClose: () => void;
let onCloseCalls = 0;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  onCloseCalls = 0;
  onClose = () => { onCloseCalls += 1; };
  getTaskState.mockResolvedValue(taskState());
  getEvidenceState.mockResolvedValue({});
  getWorkspaceGitStatus.mockResolvedValue({ exit_code: 0, stdout: '', stderr: '', artifact_ref: null });
  getWorkspaceGitDiff.mockResolvedValue({ exit_code: 0, stdout: '', stderr: '', artifact_ref: null });
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.clearAllMocks();
});

async function renderPanel() {
  await act(async () => {
    root.render(<TaskReviewPanel sessionId="s1" open onClose={onClose} />);
  });
  await act(async () => {});
}

const text = () => host.textContent ?? '';

describe('#353 TaskReviewPanel — 空态与两轴', () => {
  it('未定义任务（404）→ 空态，不是错误', async () => {
    getTaskState.mockRejectedValue(new api.TaskNotDefinedError('未定义任务'));
    await renderPanel();
    expect(text()).toContain('未定义任务');
  });

  it('两轴分区：在途 run + 四态 chip 分两区', async () => {
    getTaskState.mockResolvedValue(taskState({ product_state: 'executing', open_run_ids: ['r1', 'r2'] }));
    await renderPanel();
    expect(text()).toContain('Run / 操作态');
    expect(text()).toContain('执行中（2 个在途 run）');
    expect(text()).toContain('Task 交付态');
    expect(text()).toContain('执行中');
  });

  it('四个交付态 chip 各自映射（可交付）', async () => {
    getTaskState.mockResolvedValue(taskState({ product_state: 'deliverable' }));
    await renderPanel();
    expect(host.querySelector('.task-review-chip')?.textContent).toBe('可交付');
  });
});

describe('#353 P2-2 — 授权档位投影（创建期声明，不猜）', () => {
  it('authorization 有值 → 原目标区渲染该档位', async () => {
    getTaskState.mockResolvedValue(taskState({ authorization: 'workspace-write' }));
    await renderPanel();
    const meta = host.querySelector('.task-review-meta')?.textContent ?? '';
    expect(meta).toContain('授权档位');
    expect(meta).toContain('workspace-write');
  });

  it('authorization 为 null → 渲染「未声明」（不替用户猜档位）', async () => {
    getTaskState.mockResolvedValue(taskState({ authorization: null }));
    await renderPanel();
    expect(host.querySelector('.task-review-meta')?.textContent).toContain('未声明');
  });
});

describe('#353 TaskReviewPanel — 逐项六态 + 证据', () => {
  it('六态 verification 逐一映射中文', async () => {
    const values: api.VerificationValue[] = [
      'not_started', 'in_progress', 'passed', 'failed', 'blocked', 'incomplete',
    ];
    const criteria = values.map((v, i) => criterion(`ac-${i}`, `项 ${v}`));
    getTaskState.mockResolvedValue(taskState({
      criteria,
      verification: Object.fromEntries(values.map((v, i) => [`ac-${i}`, { value: v, evidence: null }])),
      product_state: 'pending_verification',
    }));
    await renderPanel();
    for (const label of ['未开始', '进行中', '通过', '失败', '受阻', '未完成']) {
      expect(text()).toContain(label);
    }
  });

  it('证据 result + 服务端 freshness(stale + reasons) 逐条列出', async () => {
    getTaskState.mockResolvedValue(taskState({ verification: { 'ac-1': { value: 'failed', evidence: null } } }));
    getEvidenceState.mockResolvedValue({
      'ac-1': [evidenceRecord({ result: 'fail', freshness: { status: 'stale', reasons: ['覆盖文件已变动：src/a.py'] } })],
    });
    await renderPanel();
    expect(text()).toContain('失败');
    expect(text()).toContain('证据新鲜度：已过期');
    expect(text()).toContain('覆盖文件已变动：src/a.py');
  });

  it('criterion 无 evidence 记录 → "缺证据"（与 failed / stale 区分显示）', async () => {
    getTaskState.mockResolvedValue(taskState());
    getEvidenceState.mockResolvedValue({});
    await renderPanel();
    expect(text()).toContain('缺证据');
  });

  it('可交付 + 证据已过期并存：chip 仍「可交付」，叠加消歧警示（选项 1-C）', async () => {
    getTaskState.mockResolvedValue(taskState({ product_state: 'deliverable' }));
    getEvidenceState.mockResolvedValue({
      'ac-1': [evidenceRecord({ result: 'pass', freshness: { status: 'stale', reasons: ['base_head 已变动'] } })],
    });
    await renderPanel();
    // chip 不被前端"修正"
    expect(host.querySelector('.task-review-chip')?.textContent).toBe('可交付');
    const disambiguation = host.querySelector('.task-review-disambiguation')?.textContent ?? '';
    expect(disambiguation).toContain('服务端判定：可交付');
    expect(disambiguation).toContain('证据新鲜度：已过期');
  });
});

describe('#353 TaskReviewPanel — 文件/diff/快照', () => {
  it('git status 文件列表（含 agent-progress/）+ unified diff + 快照 manifest + 一致性 reasons', async () => {
    getTaskState.mockResolvedValue(taskState());
    getEvidenceState.mockResolvedValue({
      'ac-1': [evidenceRecord({ freshness: { status: 'stale', reasons: ['覆盖文件已变动：src/a.py'] } })],
    });
    getWorkspaceGitStatus.mockResolvedValue({
      exit_code: 0,
      stdout: ' M src/a.py\n?? agent-progress/s1/progress.md',
      stderr: '',
      artifact_ref: null,
    });
    getWorkspaceGitDiff.mockResolvedValue({
      exit_code: 0,
      stdout: 'diff --git a/src/a.py b/src/a.py\n+added line',
      stderr: '',
      artifact_ref: null,
    });
    await renderPanel();
    expect(text()).toContain('agent-progress/s1/progress.md');
    expect(text()).toContain('+added line');
    expect(text()).toContain('快照（证据记录时的覆盖清单）');
    expect(text()).toContain('src/a.py');
    // 一致性结论只引用服务端 freshness reasons
    expect(host.querySelector('.task-review-consistency')?.textContent).toContain('覆盖文件已变动：src/a.py');
    // 暂存/提交不做（放的是说明文字，不是入口按钮）
    const labels = [...host.querySelectorAll('button')].map((b) => b.textContent ?? '');
    expect(labels.some((l) => l.includes('暂存') || l.includes('提交'))).toBe(false);
  });
});

describe('#353 TaskReviewPanel — 失败尝试 / UNKNOWN / 三操作', () => {
  it('失败尝试列出 failed 验收项；UNKNOWN 留槽显示"需 reconcile（来源未接入）"不推断', async () => {
    getTaskState.mockResolvedValue(taskState({
      criteria: [criterion('ac-1', '导入去重'), criterion('ac-2', '错误行不谎报完成')],
      verification: {
        'ac-1': { value: 'passed', evidence: null },
        'ac-2': { value: 'failed', evidence: null },
      },
      product_state: 'pending_verification',
    }));
    await renderPanel();
    expect(host.querySelector('.task-review-section[aria-label="失败尝试与待对账"]')?.textContent).toContain('错误行不谎报完成');
    expect(text()).toContain('需 reconcile（来源未接入）');
  });

  it('三操作各自后果文案在场；带原因接受 reason 为空 → 客户端先校验、不发请求', async () => {
    await renderPanel();
    const ops = host.querySelector('.task-review-ops')?.textContent ?? '';
    expect(ops).toContain('接受');
    expect(ops).toContain('带原因接受');
    expect(ops).toContain('释放目录');
    expect(ops).toContain('这不是撤销接受');
    // 填空白原因点提交
    const btn = [...host.querySelectorAll('button')].find((b) => b.textContent?.includes('带原因接受'))!;
    await act(async () => { btn.click(); });
    expect(acceptTask).not.toHaveBeenCalled();
    expect(text()).toContain('必须填写原因');
  });

  it('已接受态下提供「撤销接受」入口（与释放目录分开）', async () => {
    getTaskState.mockResolvedValue(taskState({
      acceptance: { decision: 'accepted', reason: null },
      product_state: 'accepted',
    }));
    await renderPanel();
    const ops = host.querySelector('.task-review-ops')?.textContent ?? '';
    expect(ops).toContain('撤销接受');
    expect(ops).toContain('与「释放目录」是两个不同事实');
  });

  it('释放目录点击 → releaseTaskLease 受理，回执如实（released:false 不冒充成功）', async () => {
    releaseTaskLease.mockResolvedValue({ released: false, promoted_to: null });
    await renderPanel();
    const btn = [...host.querySelectorAll('button')].find((b) => b.textContent?.includes('释放目录'))!;
    await act(async () => { btn.click(); });
    await act(async () => {});
    expect(releaseTaskLease).toHaveBeenCalledWith('s1');
    expect(text()).toContain('未释放');
  });
});

describe('#353 TaskReviewPanel — 只读与关闭', () => {
  it('不提供暂存/提交按钮（首版不做）', async () => {
    getTaskState.mockResolvedValue(taskState());
    await renderPanel();
    const labels = [...host.querySelectorAll('button')].map((b) => b.textContent ?? '');
    expect(labels.some((l) => l.includes('暂存') || l.includes('提交'))).toBe(false);
  });

  it('点击遮罩触发 onClose', async () => {
    await renderPanel();
    const overlay = host.querySelector('.ctx-usage-overlay') as HTMLElement;
    overlay.click();
    expect(onCloseCalls).toBeGreaterThan(0);
  });

  it('Esc 关闭弹层（键盘可达）', async () => {
    await renderPanel();
    await act(async () => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    });
    expect(onCloseCalls).toBeGreaterThan(0);
  });
});

describe('#353 P1-1 — 面板：证据不可得≠缺证据，不触发消歧', () => {
  it('hook 报告 evidenceError → 渲染"证据不可得"，不渲染"缺证据"徽标', async () => {
    // 直接让 hook 走失败分支：getEvidenceState 抛错
    getEvidenceState.mockRejectedValue(new Error('evidence 500'));
    await renderPanel();
    expect(text()).toContain('证据不可得');
    // "缺证据"徽标（.task-review-missing-evidence）不应出现；说明文案里的提及不算
    expect(host.querySelector('.task-review-missing-evidence')).toBeNull();
  });

  it('证据不可得时 deliverable 不叠加消歧警示', async () => {
    getEvidenceState.mockRejectedValue(new Error('evidence 500'));
    await renderPanel();
    // 消歧文案只在 stale/缺证据时出现；不可得时不应出现
    expect(text()).not.toContain('存在缺证据项');
  });
});

describe('#353 P1-2 — 证据来源事件 seq 可点跳转', () => {
  it('source_event_seq 渲染为可点按钮，点击调用 onJumpToEvent(seq)', async () => {
    getEvidenceState.mockResolvedValue({ 'ac-1': [evidenceRecord({ source_event_seq: 42 })] });
    const jumps: number[] = [];
    await act(async () => {
      root.render(<TaskReviewPanel sessionId="s1" open onClose={onClose} onJumpToEvent={(s) => jumps.push(s)} />);
    });
    await act(async () => {});
    const btn = host.querySelector('.task-review-evidence-seq');
    expect(btn).not.toBeNull();
    expect(btn!.textContent).toContain('42');
    await act(async () => { (btn as HTMLButtonElement).click(); });
    expect(jumps).toEqual([42]);
  });

  it('source_event_seq 为 null 时不渲染跳转按钮', async () => {
    getEvidenceState.mockResolvedValue({ 'ac-1': [evidenceRecord({ source_event_seq: null })] });
    await renderPanel();
    expect(host.querySelector('.task-review-evidence-seq')).toBeNull();
  });
});

describe('#353 P2-2/P2-3 — 授权诚实呈现与下一步', () => {
  it('P2-2：原目标节显示真实授权档位（#788 落地后占位符已移除）', async () => {
    getTaskState.mockResolvedValue(taskState());
    await renderPanel();
    expect(text()).toContain('授权档位');
    expect(text()).toContain('未声明');
    expect(text()).not.toContain('服务端投影暂未提供');
  });

  it('P2-3：有未通过项 → 下一步列出"未通过"', async () => {
    getTaskState.mockResolvedValue(
      taskState({
        criteria: [criterion('ac-1', '导入去重'), criterion('ac-2', '导出对账')],
        verification: {
          'ac-1': { value: 'failed', evidence: null },
          'ac-2': { value: 'passed', evidence: 'pytest' },
        },
      }),
    );
    getEvidenceState.mockResolvedValue({ 'ac-2': [evidenceRecord({})] });
    await renderPanel();
    expect(text()).toContain('下一步');
    expect(text()).toContain('处理以下未通过/缺证据项');
    expect(text()).toContain('未通过');
  });

  it('P2-3：无未通过/缺证据 → 诚实显示"暂无下一步建议"', async () => {
    getTaskState.mockResolvedValue(taskState());
    getEvidenceState.mockResolvedValue({ 'ac-1': [evidenceRecord({})] });
    await renderPanel();
    expect(text()).toContain('下一步');
    expect(text()).toContain('暂无下一步建议（服务端未提供）');
  });
});
