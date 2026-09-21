// 本地 / 线上 UI 复验：检查会话列表、打开一条会话看时间线及控制台错误。
//
// 两条断言的约束（机制与实测证据见 `docs/SDD_TICKET_TRACKER.md` 「B-23」段）：
//   1. 会话 id 必须从 DOM 节点 `.session-item-id` 读——**不要**用 `body.innerText`
//      全文正则去匹配完整 UUID 前缀：列表渲染的是 session_id 的截断形式，
//      全文匹配会恒为 0，把一次完全正常的渲染判成失败。
//   2. 打开会话时**不要**依赖特定语料文本（那是某个实例的数据，不是本脚本的前提）；
//      语料字符串只作信息性输出，不参与判定。
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

    // 会话条目：id 由 SessionList 渲染为 12 字符前缀（`.session-item-id`），
    // 前 8 位才是稳定寻址特征——直接读节点，避免被渲染截断长度绑架。
    const idTexts = await page.locator('.session-item-id').allInnerTexts();
    const ids = new Set(idTexts.map((text) => text.trim().slice(0, 8)).filter(Boolean));
    console.log('标题:', title);
    console.log('侧栏会话条目数:', ids.size, [...ids].join(' '));
    console.log('（信息性，不参与判定）含"读一下当前工作区"（README 会话）:', body.includes('读一下当前工作区'));
    console.log('（信息性，不参与判定）含"用两个子代理分别调研":', body.includes('用两个子代理分别调研'));

    await page.screenshot({ path: path.join(SCREENSHOT_DIR, 'ui_after.png') });

    // 点开第一条会话，检查时间线是否渲染。
    let foundSession = false;
    try {
      const first = page.locator('.session-item').first();
      if (await first.count()) {
        foundSession = true;
        await first.click({ timeout: 10000 });
        await page.waitForTimeout(4000);
        const after = await page.locator('body').innerText();
        console.log('打开会话后可见文本长度:', after.length);
        console.log('时间线出现"bash"等工具痕迹:', /bash|glob|write/i.test(after));
        await page.screenshot({ path: path.join(SCREENSHOT_DIR, 'ui_after_session.png') });
      } else {
        console.log('侧栏没有任何会话条目');
        errors.push('侧栏没有任何会话条目');
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
