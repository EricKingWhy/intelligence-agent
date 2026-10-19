/** #1368 临时取证 spec（**不入库**）：改前/改后截图 + DOM 机械测量。
 *  运行：npx playwright test --config playwright.tmp1368.config.ts zz-1368-shots
 *  环境变量 SHOT_TAG=before|after 决定文件名后缀。 */
import { test, type Page } from '@playwright/test';
import {
  AGENT_PROFILES,
  MODELS,
  PERMISSION_MODES,
  REASONING_EFFORTS,
  T,
  routeApi,
  sessionRow,
  type MemoryFixture,
  type ProjectFixture,
} from './fixtures';

const TAG = process.env.SHOT_TAG ?? 'before';
const OUT = '/home/hatch/workspace/overnight-test/shots/1368';

const P1: ProjectFixture = { id: 'p1', path: 'D:/repos/alpha', title: '项目 alpha', session_ids: ['s2', 's1'] };
const P2: ProjectFixture = { id: 'p2', path: 'D:/repos/beta', title: '项目 beta', session_ids: ['s3'] };

const sessions = () => [
  sessionRow('s1', { id: 'p1', title: '项目 alpha' }, { first_user_message: '修复登录页的重定向循环' }),
  sessionRow('s2', { id: 'p1', title: '项目 alpha' }, { first_user_message: '给事件流加上重放能力' }),
  sessionRow('s3', { id: 'p2', title: '项目 beta' }, { first_user_message: '排查 sandbox 超时' }),
  sessionRow('free-1', null, { first_user_message: '未分组的旧任务：整理 ADR' }),
];

const memory = (id: string, content: string): MemoryFixture => ({
  id, root_id: id, version: 1, content, scope: 'user_global', tier: 'profile',
  status: 'active', kind: 'semantic', project_id: null, source_type: 'automatic',
  source_session_id: 's1', source_event_ids: ['e1'],
  payload: { kind: 'semantic', subject: '用户', fact: content, category: 'preference' },
  metadata: {}, created_at: T,
});

/** 通用垂直排版检测：直接持有文本的元素，若「高 ≥ 1.8 行高」且「每行平均不到
 *  1.6 个字」（CJK 逐字堆叠）⇒ 判为竖排。 */
async function verticalTexts(page: Page) {
  return page.evaluate(() => {
    const hits: Array<Record<string, unknown>> = [];
    for (const el of Array.from(document.querySelectorAll<HTMLElement>('body *'))) {
      if (el.children.length > 0) continue;
      const text = (el.textContent ?? '').trim();
      if (text.length < 2) continue;
      const cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') continue;
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      const fs = parseFloat(cs.fontSize) || 12;
      const lh = parseFloat(cs.lineHeight) || fs * 1.4;
      const lines = r.height / lh;
      const perLine = text.length / Math.max(lines, 1);
      if (lines >= 1.8 && perLine < 1.6) {
        hits.push({
          cls: el.className?.toString().slice(0, 60),
          text: text.slice(0, 24),
          w: Math.round(r.width),
          h: Math.round(r.height),
          fs: Math.round(fs),
          lines: Math.round(lines * 10) / 10,
        });
      }
    }
    return hits;
  });
}

async function measure(page: Page, label: string) {
  const data = await page.evaluate(() => {
    const pick = (sel: string) => {
      const el = document.querySelector<HTMLElement>(sel);
      if (!el) return null;
      const r = el.getBoundingClientRect();
      const cs = getComputedStyle(el);
      return {
        x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height),
        scrollW: el.scrollWidth, clientW: el.clientWidth, display: cs.display, lines: 0,
      };
    };
    const appbarTitle = document.querySelector<HTMLElement>('.appbar-title');
    let titleLines = 0;
    if (appbarTitle) {
      const cs = getComputedStyle(appbarTitle);
      const lh = parseFloat(cs.lineHeight) || 16;
      titleLines = Math.round(appbarTitle.getBoundingClientRect().height / lh);
    }
    return {
      rail: pick('.session-rail'),
      appbar: pick('.appbar'),
      appbarTitle: pick('.appbar-title'),
      titleLines,
      composerControls: pick('.composer-controls'),
      composerActions: pick('.composer-actions'),
      firstPill: pick('.composer-controls > *:last-child'),
      sessionItemBody: pick('.session-item-body'),
    };
  });
  console.log(`[MEASURE ${label}] ${JSON.stringify(data)}`);
  return data;
}

test('capture', async ({ page }) => {
  test.setTimeout(180_000);
  await page.setViewportSize({ width: 390, height: 844 });
  await routeApi(page, {
    sessions: sessions(),
    projects: [P1, P2],
    models: MODELS,
    permissionModes: PERMISSION_MODES,
    agentProfiles: AGENT_PROFILES,
    reasoningEfforts: REASONING_EFFORTS,
    memories: [
      memory('m1', '用户偏好：所有 UI 文案用中文，日期用 ISO 格式，不要美式 AM/PM。'),
      memory('m2', '用户在本机跑测试，重活一律进云端 Docker 沙箱。'),
    ],
  });
  await page.goto('/');
  await page.locator('.app-frame').waitFor();
  await page.waitForTimeout(600);

  // ── 1. 390 主界面（rail 收起态）──
  await page.screenshot({ path: `${OUT}/session-list_390-${TAG}.png`, fullPage: false });
  await measure(page, '390-main');
  console.log('[VERTICAL 390-main] ' + JSON.stringify(await verticalTexts(page)));

  // ── 2. 390 composer 重叠检测（逐个 pill 对动作簇求交）──
  const pillOverlaps = await page.evaluate(() => {
    const actions = document.querySelector<HTMLElement>('.composer-actions');
    if (!actions) return null;
    const ra = actions.getBoundingClientRect();
    return Array.from(document.querySelectorAll<HTMLElement>('.composer-controls > *')).map((el) => {
      const r = el.getBoundingClientRect();
      const w = Math.max(0, Math.min(ra.right, r.right) - Math.max(ra.left, r.left));
      const h = Math.max(0, Math.min(ra.bottom, r.bottom) - Math.max(ra.top, r.top));
      return {
        label: el.getAttribute('aria-label') ?? el.textContent?.trim().slice(0, 12) ?? el.className,
        x: Math.round(r.x), right: Math.round(r.right),
        overlapArea: Math.round(w * h),
      };
    });
  });
  console.log('[PILL OVERLAPS 390] ' + JSON.stringify(pillOverlaps));

  // ── 3. 390 记忆面板（按钮在 390 的 appbar 溢出区外，用 JS 触发——
  //      本步只要面板的排版证据，不要"按钮是否可点"的证据）──
  await page.evaluate(() => {
    document.querySelector<HTMLElement>('[aria-label="记忆管理"]')?.click();
  });
  await page.locator('.memory-panel').waitFor();
  await page.waitForTimeout(400);
  await page.screenshot({ path: `${OUT}/memory-tab_390-${TAG}.png` });
  const detailBtn = await page.evaluate(() => {
    const btn = document.querySelector<HTMLElement>('.memory-row-head .memory-expand:last-of-type');
    if (!btn) return null;
    const span = btn.querySelector('span');
    const r = btn.getBoundingClientRect();
    const sr = span?.getBoundingClientRect();
    const cs = span ? getComputedStyle(span) : null;
    const lh = cs ? parseFloat(cs.lineHeight) || 14 : 14;
    return {
      btn: { w: Math.round(r.width), h: Math.round(r.height) },
      span: sr ? { w: Math.round(sr.width), h: Math.round(sr.height) } : null,
      spanLines: sr ? Math.round(sr.height / lh) : null,
      spanText: span?.textContent ?? null,
    };
  });
  console.log('[MEASURE memory-details] ' + JSON.stringify(detailBtn));
  console.log('[VERTICAL 390-memory] ' + JSON.stringify(await verticalTexts(page)));
  await page.keyboard.press('Escape');
  await page.waitForTimeout(300);

  // ── 4. 768 主界面 ──
  await page.setViewportSize({ width: 768, height: 900 });
  await page.waitForTimeout(400);
  await page.screenshot({ path: `${OUT}/session-list_768-${TAG}.png` });
  await measure(page, '768-main');
  console.log('[VERTICAL 768-main] ' + JSON.stringify(await verticalTexts(page)));

  // ── 5. 390 空态 ──
  await page.setViewportSize({ width: 390, height: 844 });
  await page.unrouteAll();
  await routeApi(page, { sessions: [], projects: [] });
  await page.goto('/');
  await page.locator('.app-frame').waitFor();
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${OUT}/empty_390-${TAG}.png` });
  console.log('[VERTICAL 390-empty] ' + JSON.stringify(await verticalTexts(page)));
  console.log('[MEASURE 390-empty] ' + JSON.stringify(await page.evaluate(() => {
    const pick = (sel: string) => {
      const el = document.querySelector<HTMLElement>(sel);
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return { w: Math.round(r.width), h: Math.round(r.height), text: (el.textContent ?? '').slice(0, 12) };
    };
    return {
      rail: pick('.session-rail'),
      primary: pick('.rail-empty-btn-primary'),
      ghost: pick('.rail-empty-btn-ghost'),
      hint: pick('.empty-hint'),
      railScrollW: document.querySelector<HTMLElement>('.session-rail')?.scrollWidth ?? null,
    };
  })));
});

/** #1368 抽屉交互的机械证据：展开 → 名字可读 → 点会话切换并收起 → Escape 收起 →
 *  点外部收起。全部读数打日志，截图另存。 */
test('drawer', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await routeApi(page, { sessions: sessions(), projects: [P1, P2] });
  await page.goto('/');
  await page.locator('.app-frame').waitFor();
  await page.waitForTimeout(400);

  const rail = page.locator('.session-rail');
  const toggle = page.locator('.rail-drawer-btn');
  const railW = async () => Math.round((await rail.boundingBox())!.width);
  const expanded = async () => toggle.getAttribute('aria-expanded');

  console.log(`[DRAWER closed] width=${await railW()} expanded=${await expanded()} visible=${await toggle.isVisible()}`);

  await toggle.click();
  await page.waitForTimeout(350);
  const title = page.locator('.session-row').filter({ has: page.locator('.session-item-id', { hasText: 's2' }) }).locator('.session-item-title');
  console.log(`[DRAWER open] width=${await railW()} expanded=${await expanded()}`);
  console.log(`[DRAWER title] text=${JSON.stringify(await title.innerText())} visible=${await title.isVisible()} w=${Math.round((await title.boundingBox())!.width)} lines=${Math.round((await title.boundingBox())!.height / 19)}`);
  await page.screenshot({ path: `${OUT}/session-list_390-expanded-${TAG}.png` });

  await page.locator('.session-row').filter({ has: page.locator('.session-item-id', { hasText: 's2' }) }).locator('.session-item').click();
  await page.waitForTimeout(400);
  console.log(`[DRAWER after select] width=${await railW()} expanded=${await expanded()} selected=${JSON.stringify(await page.locator('.session-item.selected .session-item-id').innerText())}`);

  await toggle.click();
  await page.waitForTimeout(250);
  await page.keyboard.press('Escape');
  await page.waitForTimeout(250);
  console.log(`[DRAWER after Escape] width=${await railW()} expanded=${await expanded()}`);

  await toggle.click();
  await page.waitForTimeout(250);
  await page.mouse.click(370, 400);
  await page.waitForTimeout(250);
  console.log(`[DRAWER after outside click] width=${await railW()} expanded=${await expanded()}`);
});
