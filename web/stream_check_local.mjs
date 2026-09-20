// 本地流式验证：真浏览器里提交一个长回答任务，按时间采样正文长度。
// 判定标准：正文长度在多个时间点**阶梯式增长**（而不是最后一次性跳到位）。
import { chromium } from '@playwright/test';

const URL = process.argv[2] || 'http://127.0.0.1:8010';
const TAG = process.argv[3] || 'local';

const browser = await chromium.launch({ channel: 'chrome' });
const page = await browser.newPage({ viewport: { width: 1680, height: 1000 } });

const errors = [];
const wsUrls = [];
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text().slice(0, 220)); });
page.on('pageerror', (e) => errors.push('pageerror: ' + String(e).slice(0, 220)));
page.on('websocket', (ws) => wsUrls.push(ws.url()));

await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
await page.waitForTimeout(3000);

const input = page.locator('textarea[aria-label="Agent 任务"]');
await input.waitFor({ timeout: 30000 });
await input.fill(
  '请写一篇大约 600 字的中文短文，主题：为什么事件溯源适合 Agent 运行时。只输出正文，不要调用任何工具，不要分点。'
);
await page.locator('button[aria-label="发送"]').click();

const t0 = Date.now();
const samples = [];
let shots = 0;
for (let i = 0; i < 70; i++) {
  const txt = await page.locator('body').innerText();
  const el = (Date.now() - t0) / 1000;
  samples.push({ t: +el.toFixed(1), len: txt.length });
  if (i % 6 === 0) {
    console.log(`  t=${el.toFixed(1)}s  body 文本长度=${txt.length}`);
    if (shots < 3) {
      shots += 1;
      await page.screenshot({ path: `D:/intelligence-agent/.workbuddy/stream_${TAG}_${shots}.png` });
    }
  }
  // 连续 3 次长度不变且已跑够 12s：视为收尾
  const n = samples.length;
  if (el > 32 && n >= 4 && samples[n - 1].len === samples[n - 4].len) break;
  await page.waitForTimeout(700);
}

console.log('=== 采样序列 ===');
console.log(samples.map((s) => `${s.t}s:${s.len}`).join('  '));

const lens = samples.map((s) => s.len);
const firstGrow = samples.findIndex((s, i) => i > 0 && s.len > samples[i - 1].len);
const growthPoints = samples.filter((s, i) => i > 0 && s.len > samples[i - 1].len).length;
const spanS = firstGrow >= 0 ? samples[samples.length - 1].t - samples[firstGrow].t : 0;
console.log('=== 判定 ===');
console.log('首次增长于 t =', firstGrow >= 0 ? samples[firstGrow].t + 's' : '(无增长)');
console.log('有增长的采样点次数 =', growthPoints, ' 增长跨度 =', spanS.toFixed(1) + 's');
console.log('末长度 =', lens[lens.length - 1], ' 首长度 =', lens[0]);
console.log('WS 连接:', wsUrls.length ? wsUrls.join(', ') : '(无 —— 说明前端没走 WS！)');
console.log('结论:', growthPoints >= 3 && spanS >= 4 ? '✅ 阶梯式增长 = 真流式' : '❌ 一次性出现（非流式）');

console.log('=== console 错误 ===');
console.log(errors.length ? [...new Set(errors)].join('\n') : '(无)');
await browser.close();
