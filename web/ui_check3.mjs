// 线上 Demo UI 复验：检查会话列表、打开一条会话看时间线及控制台错误。
import { chromium } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import path from 'node:path';

const URL = process.argv[2];
const SCREENSHOT_DIR = path.resolve(process.argv[3] || '.workbuddy');

async function main() {
  if (!URL) throw new Error('Usage: node web/ui_check3.mjs <url> [screenshot-dir]');

  const browser = await chromium.launch({ channel: 'chrome' });
  try {
    const page = await browser.newPage({ viewport: { width: 1680, height: 1000 } });
    await mkdir(SCREENSHOT_DIR, { recursive: true });

    const errors = [];
    page.on('console', (message) => {
      if (message.type() === 'error') errors.push(message.text().slice(0, 240));
    });
    page.on('pageerror', (error) => errors.push(`pageerror: ${String(error).slice(0, 240)}`));
    page.on('requestfailed', (request) => {
      errors.push(
        `reqfail: ${request.url().slice(0, 140)} ${request.failure()?.errorText || ''}`
      );
    });

    await page.goto(URL, { waitUntil: 'networkidle', timeout: 90000 });
    await page.waitForTimeout(5000);

    const title = await page.title();
    const body = await page.locator('body').innerText();

    // 会话条目：列表里的会话 id 前 8 位是稳定特征（UUID）
    const ids = new Set((body.match(/[0-9a-f]{8}-[0-9a-f]{4}/g) || []).map((id) => id.slice(0, 8)));
    console.log('标题:', title);
    console.log('页面上出现的会话 id 数:', ids.size, [...ids].join(' '));
    console.log('含"读一下当前工作区…"（README 会话）:', body.includes('读一下当前工作区'));
    console.log('含"用两个子代理分别调研":', body.includes('用两个子代理分别调研'));

    await page.screenshot({ path: path.join(SCREENSHOT_DIR, 'ui_after.png') });

    // 点开预期的会话，检查时间线是否渲染。
    let foundSession = false;
    try {
      const first = page.locator('text=读一下当前工作区').first();
      if (await first.count()) {
        foundSession = true;
        await first.click({ timeout: 10000 });
        await page.waitForTimeout(4000);
        const after = await page.locator('body').innerText();
        console.log('打开会话后可见文本长度:', after.length);
        console.log('时间线出现"bash"等工具痕迹:', /bash|glob|write/i.test(after));
        await page.screenshot({ path: path.join(SCREENSHOT_DIR, 'ui_after_session.png') });
      } else {
        console.log('未找到可点击的会话条目');
        errors.push('未找到可点击的预期会话条目');
      }
    } catch (error) {
      console.log('打开会话失败:', String(error).slice(0, 200));
      errors.push('打开会话失败');
    }

    console.log('=== console 错误 ===');
    console.log(errors.length ? [...new Set(errors)].join('\n') : '(无)');
    if (ids.size === 0 || !foundSession || errors.length > 0) process.exitCode = 1;
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
