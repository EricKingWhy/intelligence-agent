/** 真机 long task 采集器——`docs/PERF_BASELINE.md` §2.1 观感口径（long task 数）的复现脚本。
 *
 *  跑法（后端须已在 :8000 托管 `web/dist`，即生产口径）：
 *    cd web
 *    node scripts/perf-longtask-live.mjs                      # 默认 http://127.0.0.1:8000
 *    node scripts/perf-longtask-live.mjs --base http://localhost:5173   # dev 口径（未压缩）
 *
 *  操作约束（口径）：采集手段是浏览器原生 `PerformanceObserver('longtask')`，阈值 50ms
 *  ——与 Chrome Performance 面板同源判定，**不是** Playwright 帧率采集（§2.1 明令不引入）；
 *  场景 = 打开事件数最多的真实会话 → 12 次 Inspector peek 切换 → 工作区面板往返；
 *  **只报数字、不做阈值断言**（同 `f1-cost-probe`）；dev 与生产两口径**不可混用同一基线**。
 *
 *  完整叙述（为什么这么做、已落的历史数字、跨会话波动声明）在 `docs/PERF_BASELINE.md` F2 节。
 */

import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { chromium } from '@playwright/test';

// 代理会让对 localhost 的请求发不出去（本机代理不一定能转发环回地址），先清掉。
for (const k of ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy']) {
  delete process.env[k];
}

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i !== -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const BASE = arg('base', 'http://127.0.0.1:8000');
const SESSION = arg('session', '');
const SHOTS = arg('shots', join('gui-test-screenshots', 'perf-longtask'));
const PEEK_SWITCHES = Number(arg('switches', '12'));

const sum = (a) => a.reduce((s, x) => s + x.dur, 0);
const worst = (a, n = 5) => [...a].sort((x, y) => y.dur - x.dur).slice(0, n);
const report = (label, tasks) =>
  `${label}: count=${tasks.length} total=${sum(tasks)}ms worst=${JSON.stringify(worst(tasks))}`;

/** 长任务采集：必须在页面加载前注入，否则首帧那几条采不到。 */
const INIT = () => {
  window.__longTasks = [];
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        window.__longTasks.push({ start: Math.round(e.startTime), dur: Math.round(e.duration) });
      }
    }).observe({ entryTypes: ['longtask'] });
  } catch (err) {
    window.__longTaskUnsupported = String(err);
  }
};

/** 事件数最多的真实会话——比硬编码 id 稳：默认场景不依赖某条会话是否还在。 */
async function richestSession(page) {
  return page.evaluate(async () => {
    const rows = await fetch('/api/sessions').then((r) => r.json());
    const top = [...rows].sort((a, b) => (b.event_count ?? 0) - (a.event_count ?? 0))[0];
    return top ? { id: top.session_id, events: top.event_count } : null;
  });
}

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
await ctx.addInitScript(INIT);
const page = await ctx.newPage();
mkdirSync(SHOTS, { recursive: true });

await page.goto(`${BASE}/`);
await page.waitForTimeout(2500);
await page.screenshot({ path: join(SHOTS, '01-shell.png') });
const afterLoad = await page.evaluate(() => window.__longTasks.slice());

const picked = SESSION ? { id: SESSION, events: '(手工指定)' } : await richestSession(page);
if (!picked) throw new Error('后端没有可用会话——先造一条含工具调用的会话再跑本脚本');
console.log(`baseURL=${BASE} 会话=${picked.id}（${picked.events} 事件）截图目录=${SHOTS}`);

const row = page.locator('.session-item').filter({ hasText: picked.id.slice(0, 8) });
await row.first().click();
await page.waitForTimeout(4000);
await page.screenshot({ path: join(SHOTS, '02-session-opened.png') });
const afterOpen = await page.evaluate(() => window.__longTasks.slice());

// 逐个切换 Inspector peek：每次切换都会重渲右栏，是 F2 要优化的那条路径。
const rows = page.locator('.timeline-row');
const nRows = await rows.count();
const steps = Math.min(PEEK_SWITCHES, nRows);
const seen = [];
for (let i = 0; i < steps; i += 1) {
  await rows.nth(i * Math.max(1, Math.floor(nRows / steps))).click().catch(() => {});
  await page.waitForTimeout(220);
  seen.push({
    kind: await page.locator('.detail-peek .detail-peek-kind').first().innerText().catch(() => '(无 peek)'),
    current: await page.locator('.timeline-row[aria-current="true"]').count(),
  });
}
await page.screenshot({ path: join(SHOTS, '03-inspector-peek.png') });

// 工作区面板往返
const panel = page.getByRole('tablist', { name: '工作区面' });
for (const name of ['输出', '改动']) {
  const tab = panel.getByRole('tab', { name });
  if (await tab.count()) await tab.first().click().catch(() => {});
  await page.waitForTimeout(900);
}
await page.screenshot({ path: join(SHOTS, '04-panels.png') });
const afterAll = await page.evaluate(() => window.__longTasks.slice());

console.log(`timeline-row=${nRows}（本脚本切换 ${steps} 次）`);
console.log(`peek 巡检：kind 全部有值=${seen.every((s) => s.kind && s.kind !== '(无 peek)')}；` +
  `aria-current 恒为 1=${seen.every((s) => s.current === 1)}`);
console.log('');
console.log(report('加载 + 初始渲染', afterLoad));
console.log(report(`打开长会话（${picked.events} 事件）`, afterOpen));
console.log(report(`${steps} 次 Inspector 切换 + 工作区面板往返`, afterAll));
console.log(report('打开会话 + 交互（= 扣掉加载段）', afterAll.slice(afterLoad.length)));

await browser.close();
