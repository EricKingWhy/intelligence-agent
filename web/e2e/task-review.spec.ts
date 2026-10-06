/** #353 W-09：任务 diff 与证据审阅界面的端到端契约。
 *
 *  数据层用 `routeApi` 的既有 mock 车道，再叠加**逐字段镜像真实 FastAPI 投影形状**
 *  的 task / evidence / workspace-git / 三操作路由（形状取自
 *  `session/task.py::TaskState.to_payload`、`session/evidence.py`、
 *  `web/task_delivery.py`、`web/workspace_files.py::GitCommandResult`）。
 *
 *  为什么不在标准门禁里起真实 FastAPI：本车道（`playwright.config.ts`）按既有约定
 *  不依赖真后端（真后端联调在 `e2e-live/`，手动/夜间）。本 spec 用与后端同形的
 *  固定投影夹具，锁住界面行为；"后端是否真产出这些形状"由后端侧测试锁。
 *
 *  锁六件事里的界面侧：六态逐项结果、缺证据、服务端 freshness 消歧（chip 文案不被
 *  前端"修正"）、两轴分区、三操作各自后果、刷新后状态同源。 */

import { expect, test, type Page, type Route } from '@playwright/test';
import { SID, T, fulfillSse, routeApi, submitTask, type FrameSpec } from './fixtures';

const json = (route: Route, body: unknown, status = 200) =>
  route.fulfill({ status, body: JSON.stringify(body), contentType: 'application/json' });

const FRAMES: FrameSpec[] = [
  { type: 'session/started', seq: 1, session_id: SID, run_id: 'run-1', time: T },
  { type: 'run/started', seq: 2, session_id: SID, run_id: 'run-1', time: T },
  { type: 'user/message', data: { content: '审阅这个任务' }, seq: 3, session_id: SID, run_id: 'run-1', step_id: 1, time: T },
  { type: 'run/completed', data: {}, seq: 4, session_id: SID, run_id: 'run-1', time: T },
];

interface TaskFixture {
  task: Record<string, unknown>;
  evidence: Record<string, unknown[]>;
  gitStatus?: Record<string, unknown>;
  gitDiff?: Record<string, unknown>;
  leaseReleased?: boolean;
}

function criterion(item_id: string, text: string, origin: 'user' | 'agent' = 'user') {
  return { item_id, text, origin, confirmed: origin === 'user' };
}

function verification(value: string) {
  return { value, evidence: null };
}

function evidenceRecord(over: Record<string, unknown> = {}) {
  return {
    evidence_id: 'ev-1',
    task_session_id: SID,
    run_id: 'run-1',
    criterion_id: 'c3',
    kind: 'test',
    source_event_seq: 3,
    tool_call_id: null,
    captured_at: T,
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

/** 安装 task/evidence/lease/workspace-git 路由（叠加在 routeApi 之上；其余走 fallback）。 */
async function mockTaskApi(page: Page, state: TaskFixture): Promise<void> {
  await page.route('**/api/**', async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    const method = req.method();
    if (path.endsWith('/task/acceptance/release') && method === 'POST') {
      state.task = { ...state.task, acceptance: null, version: (state.task.version as number) + 1, product_state: 'deliverable' };
      return json(route, { status: 'applied', task: state.task });
    }
    if (path.endsWith('/task/acceptance') && method === 'POST') {
      const body = (req.postDataJSON() ?? {}) as { decision?: string; reason?: string; expected_version?: number };
      if (body.expected_version !== state.task.version) {
        return json(route, { detail: `版本冲突：expected_version=${body.expected_version}，当前 version=${state.task.version}` }, 409);
      }
      if (state.task.acceptance) {
        return json(route, { detail: '已存在接受事实（不能双写）' }, 409);
      }
      state.task = {
        ...state.task,
        acceptance: { decision: body.decision ?? 'accepted', reason: body.reason ?? null },
        version: (state.task.version as number) + 1,
        product_state: 'accepted',
      };
      return json(route, { status: 'applied', task: state.task });
    }
    if (path.endsWith('/task/lease/release') && method === 'POST') {
      return json(route, { released: state.leaseReleased ?? false, promoted_to: null });
    }
    if (path.endsWith('/task') && method === 'GET') {
      return json(route, { task: state.task });
    }
    if (path.endsWith('/evidence') && method === 'GET') {
      return json(route, { evidence: state.evidence });
    }
    if (path.endsWith('/workspace/git/status') && method === 'GET') {
      return json(route, state.gitStatus ?? { exit_code: 0, stdout: '', stderr: '', artifact_ref: null });
    }
    if (path.endsWith('/workspace/git/diff') && method === 'GET') {
      return json(route, state.gitDiff ?? { exit_code: 0, stdout: '', stderr: '', artifact_ref: null });
    }
    return route.fallback();
  });
}

async function openReview(page: Page, state: TaskFixture): Promise<void> {
  await routeApi(page, {
    sessions: [{
      session_id: SID, event_count: FRAMES.length, first_event_time: T, last_event_time: T,
      first_user_message: '审阅这个任务', trace_id: null, trace_url: null,
    }],
    events: FRAMES,
    onSessionPost: (route) => fulfillSse(route, FRAMES),
  });
  await mockTaskApi(page, state);
  await page.goto('/');
  await submitTask(page, '审阅这个任务');
  await page.getByRole('button', { name: '任务审阅' }).click();
  await expect(page.getByRole('dialog', { name: '任务审阅' })).toBeVisible();
}

test('六态逐项结果 + 缺证据 + 两轴 + UNKNOWN 留槽', async ({ page }) => {
  const values = ['not_started', 'in_progress', 'passed', 'failed', 'blocked', 'incomplete'];
  const criteria = values.map((v, i) => criterion(`c${i + 1}`, `验收项 ${v}`));
  const state: TaskFixture = {
    task: {
      defined: true, task_text: '把 CSV 导入写对', read_write_intent: '拟写入', cwd: '/repo',
      criteria,
      verification: Object.fromEntries(values.map((v, i) => [`c${i + 1}`, verification(v)])),
      acceptance: null, version: 1, open_run_ids: ['r1'], product_state: 'pending_verification',
    },
    evidence: {
      c3: [evidenceRecord({ criterion_id: 'c3', result: 'pass', freshness: { status: 'fresh', reasons: [] } })],
      c4: [evidenceRecord({ criterion_id: 'c4', result: 'fail', freshness: { status: 'stale', reasons: ['覆盖文件已变动：src/a.py'] } })],
    },
  };
  await openReview(page, state);
  const panel = page.getByRole('dialog', { name: '任务审阅' });

  for (const label of ['未开始', '进行中', '通过', '失败', '受阻', '未完成']) {
    await expect(panel).toContainText(label);
  }
  // 缺证据（c1 无 evidence 记录）与 failed/stale 区分显示
  await expect(panel.locator('.task-review-missing-evidence').first()).toHaveText('缺证据');
  await expect(panel).toContainText('证据新鲜度：已过期');
  await expect(panel).toContainText('覆盖文件已变动：src/a.py');

  // 两轴分区：Run 轴执行中 + 交付态 chip 待验证
  await expect(panel).toContainText('Run / 操作态');
  await expect(panel).toContainText('执行中（1 个在途 run）');
  await expect(panel.locator('.task-review-chip')).toHaveText('待验证');

  // UNKNOWN 留槽不推断
  await expect(panel).toContainText('需 reconcile（来源未接入）');
});

test('可交付 + 证据已过期并存：chip 文案不被前端修正，仅叠加消歧警示', async ({ page }) => {
  const state: TaskFixture = {
    task: {
      defined: true, task_text: 'x', read_write_intent: null, cwd: '/repo',
      criteria: [criterion('c1', '导入去重')],
      verification: { c1: verification('passed') },
      acceptance: null, version: 1, open_run_ids: [], product_state: 'deliverable',
    },
    evidence: { c1: [evidenceRecord({ criterion_id: 'c1', result: 'pass', freshness: { status: 'stale', reasons: ['base_head 已变动'] } })] },
  };
  await openReview(page, state);
  const panel = page.getByRole('dialog', { name: '任务审阅' });
  await expect(panel.locator('.task-review-chip')).toHaveText('可交付');
  const disambiguation = panel.locator('.task-review-disambiguation');
  await expect(disambiguation).toContainText('服务端判定：可交付');
  await expect(disambiguation).toContainText('证据新鲜度：已过期');
});

test('接受 → 交付态变已接受；刷新后同源（状态来自服务端投影，非本地缓存）', async ({ page }) => {
  // `page.reload()` 会重启整个应用（会话/模型/能力/事件流全量重拉）再重开面板，
  // 比同页交互重得多；30s 默认预算在负载机上会让「刷新后同源」这条断言没机会重试到
  // 终态（实测：同一断言在放宽预算后稳定通过）。`slow()` 只放宽上限、不放宽断言。
  test.slow();
  const state: TaskFixture = {
    task: {
      defined: true, task_text: 'x', read_write_intent: null, cwd: '/repo',
      criteria: [criterion('c1', '导入去重')],
      verification: { c1: verification('passed') },
      acceptance: null, version: 1, open_run_ids: [], product_state: 'deliverable',
    },
    evidence: { c1: [evidenceRecord({ criterion_id: 'c1', result: 'pass' })] },
  };
  await openReview(page, state);
  await page.getByRole('button', { name: '接受', exact: true }).click();
  const panel = page.getByRole('dialog', { name: '任务审阅' });
  await expect(panel.locator('.task-review-chip')).toHaveText('已接受');
  await expect(panel).toContainText('已接受：服务端交付状态为「已接受」');

  // 刷新后重开：状态仍来自服务端投影（同源），不是本地缓存推断
  await page.reload();
  await page.getByRole('button', { name: '任务审阅' }).click();
  await expect(page.getByRole('dialog', { name: '任务审阅' }).locator('.task-review-chip')).toHaveText('已接受');
});

test('带原因接受 reason 为空 → 客户端先校验（不发请求）；释放目录 released:false 如实显示', async ({ page }) => {
  const state: TaskFixture = {
    task: {
      defined: true, task_text: 'x', read_write_intent: null, cwd: '/repo',
      criteria: [criterion('c1', '导入去重')],
      verification: { c1: verification('passed') },
      acceptance: null, version: 1, open_run_ids: [], product_state: 'deliverable',
    },
    evidence: {},
    leaseReleased: false,
  };
  await openReview(page, state);
  const panel = page.getByRole('dialog', { name: '任务审阅' });

  await page.getByRole('button', { name: '带原因接受' }).click();
  await expect(panel).toContainText('带原因接受必须填写原因。');

  await page.getByRole('button', { name: '释放目录' }).click();
  await expect(panel).toContainText('未释放：本会话不是该目录的写租约持有者');

  // 三操作后果文案分开（释放目录 ≠ 撤销接受）
  await expect(panel).toContainText('这不是撤销接受');
});

test('文件/diff/快照：git status 文件列表 + unified diff + 快照清单 + 一致性 reasons', async ({ page }) => {
  const state: TaskFixture = {
    task: {
      defined: true, task_text: 'x', read_write_intent: '拟写入', cwd: '/repo',
      criteria: [criterion('c1', '导入去重')],
      verification: { c1: verification('passed') },
      acceptance: null, version: 1, open_run_ids: [], product_state: 'deliverable',
    },
    evidence: { c1: [evidenceRecord({ criterion_id: 'c1', freshness: { status: 'stale', reasons: ['覆盖文件已变动：src/a.py'] } })] },
    gitStatus: { exit_code: 0, stdout: ' M src/a.py\n?? agent-progress/' + SID + '/progress.md', stderr: '', artifact_ref: null },
    gitDiff: { exit_code: 0, stdout: 'diff --git a/src/a.py b/src/a.py\n+added line', stderr: '', artifact_ref: null },
  };
  await openReview(page, state);
  const panel = page.getByRole('dialog', { name: '任务审阅' });
  await expect(panel).toContainText('agent-progress/' + SID + '/progress.md');
  await expect(panel.locator('.task-review-diff')).toContainText('+added line');
  await expect(panel).toContainText('快照（证据记录时的覆盖清单）');
  await expect(panel.locator('.task-review-consistency')).toContainText('覆盖文件已变动：src/a.py');
  // 暂存/提交不做
  await expect(panel.getByRole('button', { name: /暂存|提交/ })).toHaveCount(0);
});
