/** #353 W-09 联调车道：任务审阅面板的真机证据（真实 FastAPI + 固定事件夹具，
 * **无任何 `page.route` mock**）。
 *
 * 运行前提（`playwright.live.config.ts` 的约定）：
 *  1. 后端：本 worktree 的 `src` 起 web app 于 127.0.0.1:8000
 *    （`PYTHONPATH=<worktree>/src`，否则 venv 的 .pth 会把 `agent_harness`
 *     指回 main checkout；`MODEL_API_KEY` 只需过配置校验——本 spec 全程
 *     `launch=false`，不调模型）；
 *  2. 前端：`npm run dev`（5173，vite proxy `/api` → 8000；由 live config 的
 *     webServer 拉起）；
 *  3. `npx playwright test --config playwright.live.config.ts task-review-live.spec.ts --workers=1`。
 *
 * 确定性：夹具是**固定**的（task/defined → verification ×3 → evidence ×2），经真实
 * REST 写入 append-only 事件流；每次运行建**新会话**，零跨 run 污染。不调模型。
 * **不入标准门禁**——主车道（`playwright.config.ts`）只扫 `./e2e`，回归锁是
 * hermetic 的 `e2e/task-review.spec.ts`（mock 车道）。
 *
 * 覆盖（真实投影形状）：
 *  ① 原目标/验收项 ② 两轴状态（Run 轴 / 交付四态 chip）③ 逐项结果（通过+新鲜 /
 *  通过+已过期 / 失败+缺证据）④ 文件与差异 ⑤ 失败尝试 + UNKNOWN 留槽
 *  ⑥ 三操作各自后果；外加「刷新后状态同源」与选项 1-C 消歧横幅
 *  （可交付 + 证据已过期并存时 chip 文案不被前端"修正"）。
 */

import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import { createHash } from 'crypto';

const API = 'http://127.0.0.1:8000';
const T = '2026-10-06T16:00:00Z';
const SHOTS = '/tmp/t353-live/shots';

function manifest(files: Array<[string, string]>) {
  const canon = files
    .map(([p, s]) => `${p}\t${s}`)
    .sort()
    .join('\n');
  return {
    files: files.map(([path, sha256]) => ({ path, sha256 })),
    manifest_hash: createHash('sha256').update(canon).digest('hex'),
    progress_md_sha256: null,
  };
}

/** 固定夹具播种（真实 REST → 真实事件流 → 真实服务端投影）。 */
async function seed(request: APIRequestContext) {
  const post = async (path: string, body: unknown) => {
    const r = await request.post(API + path, { data: body });
    if (!r.ok()) throw new Error(`${path} → ${r.status()}: ${await r.text()}`);
    return r.json();
  };
  const { session_id: sid } = await post('/api/sessions?launch=false', {});
  await post(`/api/sessions/${sid}/task/definition`, {
    task_text: 'T353 live 夹具：实现登录页（固定夹具，不调模型）',
    read_write_intent: 'write',
    cwd: '/tmp/t353-live-ws',
    criteria: [
      { item_id: 'c1', text: '登录页渲染用户名/密码输入框', origin: 'user' },
      { item_id: 'c2', text: '错误密码显示中文报错', origin: 'user' },
      { item_id: 'c3', text: '支持第三方登录', origin: 'agent' },
    ],
  });
  for (const [item_id, value] of [['c1', 'passed'], ['c2', 'passed'], ['c3', 'failed']] as const) {
    await post(`/api/sessions/${sid}/task/verification`, { item_id, value, evidence: null });
  }
  // c1：新鲜证据（空覆盖清单 + base_head=None）
  await post(`/api/sessions/${sid}/evidence`, {
    evidence: {
      evidence_id: 'ev-fresh-1', task_session_id: sid, run_id: 'run-live-1',
      criterion_id: 'c1', kind: 'test', source_event_seq: 2, tool_call_id: null,
      captured_at: T, result: 'pass', command_or_action: 'pytest tests/test_login.py -k render',
      exit_code_or_observation: 0, artifact_ref: null, base_head: null,
      workspace_manifest: manifest([]),
    },
  });
  // c2：过期证据（base_head 固定 40-hex 与当前不符 → 确定性 stale）
  const sha = createHash('sha256').update('v1').digest('hex');
  await post(`/api/sessions/${sid}/evidence`, {
    evidence: {
      evidence_id: 'ev-stale-1', task_session_id: sid, run_id: 'run-live-1',
      criterion_id: 'c2', kind: 'ui', source_event_seq: 3, tool_call_id: null,
      captured_at: T, result: 'pass', command_or_action: '截屏确认中文报错',
      exit_code_or_observation: '报错文案可见', artifact_ref: null, base_head: 'a'.repeat(40),
      workspace_manifest: manifest([['src/login.py', sha]]),
    },
  });
  // c3：故意不播证据 → "缺证据"
  return sid as string;
}

async function openPanel(page: Page, sid: string) {
  await page.goto('/');
  // 会话行：无用户消息时标题回退为短 ID（projection.ts deriveSessionTitle）。
  await page.locator('button:has(.session-item-id)', { hasText: sid.slice(0, 12) }).first().click();
  await page.getByRole('button', { name: '任务审阅' }).click();
  await expect(page.getByRole('dialog', { name: '任务审阅' })).toBeVisible();
}

test('live: 六件事逐一呈现（真实投影形状）', async ({ page, request }) => {
  const sid = await seed(request);
  await openPanel(page, sid);
  const dialog = page.getByRole('dialog', { name: '任务审阅' });

  // ① 原目标 / 验收项
  const s1 = dialog.locator('section[aria-label="原目标与验收项"]');
  await expect(s1.getByText('T353 live 夹具：实现登录页')).toBeVisible();
  await expect(s1.getByText('登录页渲染用户名/密码输入框')).toBeVisible();
  await expect(s1.getByText('支持第三方登录')).toBeVisible();
  await s1.screenshot({ path: `${SHOTS}/live-1-target.png` });

  // ② 两轴：交付态 待验证（c3 失败 → 非全 passed）；Run 轴 无在途 run
  const s2 = dialog.locator('section[aria-label="状态（两轴）"]');
  await expect(s2.getByText('待验证')).toBeVisible();
  await expect(s2.getByText('无在途 run')).toBeVisible();
  await expect(s2.getByText('服务端判定依据：验收项验证值（不依据证据新鲜度）')).toBeVisible();
  await s2.screenshot({ path: `${SHOTS}/live-2-axes.png` });

  // ③ 逐项：c1 通过+新鲜 / c2 通过+已过期(+base_head 原因) / c3 失败+缺证据
  const s3 = dialog.locator('section[aria-label="逐项结果"]');
  await expect(s3.getByText('证据新鲜度：新鲜')).toBeVisible();
  await expect(s3.getByText('证据新鲜度：已过期')).toBeVisible();
  await expect(s3.getByText(/base_head/)).toBeVisible();
  await expect(s3.getByText('缺证据')).toBeVisible();
  await s3.screenshot({ path: `${SHOTS}/live-3-results.png` });

  // ④ 文件与差异（/tmp 非 git 仓库 → 如实呈现不可得，不伪造）
  const s4 = dialog.locator('section[aria-label="文件与差异"]');
  await expect(s4.getByText('改了哪些文件')).toBeVisible();
  await s4.screenshot({ path: `${SHOTS}/live-4-diff.png` });

  // ⑤ 失败尝试 + UNKNOWN 留槽（不推断）
  const s5 = dialog.locator('section[aria-label="失败尝试与待对账"]');
  await expect(s5.getByText('支持第三方登录')).toBeVisible();
  await expect(s5.getByText('需 reconcile（来源未接入）')).toBeVisible();
  await s5.screenshot({ path: `${SHOTS}/live-5-failures.png` });

  // ⑥ 三操作：各自后果文案
  const s6 = dialog.locator('section[aria-label="操作"]');
  await expect(s6.getByRole('button', { name: '接受', exact: true })).toBeVisible();
  await expect(s6.getByRole('button', { name: '带原因接受' })).toBeVisible();
  await expect(s6.getByRole('button', { name: '释放目录' })).toBeVisible();
  await expect(s6.getByText(/这不是撤销接受/)).toBeVisible();
  await s6.screenshot({ path: `${SHOTS}/live-6-ops.png` });
});

test('live: 刷新同源 + 可交付/过期消歧 + 接受操作（真实 CAS）', async ({ page, request }) => {
  const sid = await seed(request);
  await openPanel(page, sid);
  const dialog = page.getByRole('dialog', { name: '任务审阅' });

  // 把 c3 翻成 passed → 全 passed → 服务端 deliverable（c2 证据仍过期）
  const r = await request.post(`${API}/api/sessions/${sid}/task/verification`, {
    data: { item_id: 'c3', value: 'passed', evidence: null },
  });
  expect(r.ok()).toBeTruthy();

  // 刷新：状态必须同源（不变量 #22——重建自服务端投影，不读本地缓存）
  await page.reload();
  await openPanel(page, sid);
  const s2 = dialog.locator('section[aria-label="状态（两轴）"]');
  // 选项 1-C：chip 仍是服务端原文"可交付"，只叠加消歧横幅，不被前端"修正"
  await expect(s2.getByText('可交付', { exact: true }).first()).toBeVisible();
  await expect(s2.getByText('注意：交付判定与证据新鲜度不一致。')).toBeVisible();
  await expect(s2.getByText(/服务端判定：可交付（基于验收项验证）；证据新鲜度：已过期/)).toBeVisible();
  await s2.screenshot({ path: `${SHOTS}/live-7-disambiguation.png` });

  // 真实 CAS 接受：点"接受" → chip 变"已接受"
  await dialog.locator('section[aria-label="操作"]').getByRole('button', { name: '接受', exact: true }).click();
  await expect(s2.getByText('已接受', { exact: true }).first()).toBeVisible({ timeout: 15000 });
  await expect(dialog.getByText(/已接受：服务端交付状态为「已接受」/)).toBeVisible();
  await dialog.screenshot({ path: `${SHOTS}/live-8-accepted.png` });
});
