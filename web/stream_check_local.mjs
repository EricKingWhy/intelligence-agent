// 本地流式验证：真浏览器里提交一个长回答任务，按时间采样正文长度。
// 判定标准：正文长度在多个时间点阶梯式增长（而不是最后一次性跳到位）。
import { chromium } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import path from 'node:path';

const URL = process.argv[2] || 'http://127.0.0.1:8010';
const TAG = process.argv[3] || 'local';
const SCREENSHOT_DIR = path.resolve(process.argv[4] || '.workbuddy');

async function main() {
  const browser = await chromium.launch({ channel: 'chrome' });
  try {
    const page = await browser.newPage({ viewport: { width: 1680, height: 1000 } });
    await mkdir(SCREENSHOT_DIR, { recursive: true });

    const errors = [];
    const wsUrls = [];
    page.on('console', (message) => {
      if (message.type() === 'error') errors.push(message.text().slice(0, 220));
    });
    page.on('pageerror', (error) => errors.push(`pageerror: ${String(error).slice(0, 220)}`));
    page.on('websocket', (websocket) => wsUrls.push(websocket.url()));

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
      const text = await page.locator('body').innerText();
      const elapsed = (Date.now() - t0) / 1000;
      samples.push({ t: +elapsed.toFixed(1), len: text.length });
      if (i % 6 === 0) {
        console.log(`  t=${elapsed.toFixed(1)}s  body 文本长度=${text.length}`);
        if (shots < 3) {
          shots += 1;
          await page.screenshot({
            path: path.join(SCREENSHOT_DIR, `stream_${TAG}_${shots}.png`),
          });
        }
      }
      // 连续 3 次长度不变且已跑够 12s：视为收尾
      const n = samples.length;
      if (elapsed > 32 && n >= 4 && samples[n - 1].len === samples[n - 4].len) break;
      await page.waitForTimeout(700);
    }

    console.log('=== 采样序列 ===');
    console.log(samples.map((sample) => `${sample.t}s:${sample.len}`).join('  '));

    const lens = samples.map((sample) => sample.len);
    const firstGrow = samples.findIndex((sample, i) => i > 0 && sample.len > samples[i - 1].len);
    const growthPoints = samples.filter((sample, i) => i > 0 && sample.len > samples[i - 1].len).length;
    const spanS = firstGrow >= 0 ? samples[samples.length - 1].t - samples[firstGrow].t : 0;
    console.log('=== 判定 ===');
    console.log('首次增长于 t =', firstGrow >= 0 ? `${samples[firstGrow].t}s` : '(无增长)');
    console.log('有增长的采样点次数 =', growthPoints, ' 增长跨度 =', `${spanS.toFixed(1)}s`);
    console.log('末长度 =', lens[lens.length - 1], ' 首长度 =', lens[0]);
    console.log('WS 连接:', wsUrls.length ? wsUrls.join(', ') : '(无 —— 说明前端没走 WS！)');

    const passed = growthPoints >= 3 && spanS >= 4 && errors.length === 0;
    console.log('结论:', passed ? '✅ 阶梯式增长 = 真流式' : '❌ 检查失败');
    console.log('=== console 错误 ===');
    console.log(errors.length ? [...new Set(errors)].join('\n') : '(无)');
    if (!passed) process.exitCode = 1;
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
