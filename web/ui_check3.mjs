// 线上 Demo UI 复验（数据目录变更后）：会话列表条数 + 打开一条会话看时间线 + 控制台零错误
import { chromium } from '@playwright/test';

const URL = process.argv[2];
const browser = await chromium.launch({ channel: 'chrome' });
const page = await browser.newPage({ viewport: { width: 1680, height: 1000 } });

const errors = [];
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text().slice(0, 240)); });
page.on('pageerror', (e) => errors.push('pageerror: ' + String(e).slice(0, 240)));
page.on('requestfailed', (r) => errors.push('reqfail: ' + r.url().slice(0, 140) + ' ' + (r.failure()?.errorText || '')));

await page.goto(URL, { waitUntil: 'networkidle', timeout: 90000 });
await page.waitForTimeout(5000);

const title = await page.title();
const body = await page.locator('body').innerText();

// 会话条目：列表里的会话 id 前 8 位是稳定特征（UUID）
const ids = new Set((body.match(/[0-9a-f]{8}-[0-9a-f]{4}/g) || []).map((s) => s.slice(0, 8)));
console.log('标题:', title);
console.log('页面上出现的会话 id 数:', ids.size, [...ids].join(' '));
console.log('含"读一下当前工作区…"（README 会话）:', body.includes('读一下当前工作区'));
console.log('含"用两个子代理分别调研":', body.includes('用两个子代理分别调研'));

await page.screenshot({ path: 'D:/intelligence-agent/.workbuddy/ui_after.png' });

// 点开第一条会话，看时间线是否渲染出事件
try {
  const first = page.locator('text=读一下当前工作区').first();
  if (await first.count()) {
    await first.click({ timeout: 10000 });
    await page.waitForTimeout(4000);
    const after = await page.locator('body').innerText();
    console.log('打开会话后可见文本长度:', after.length);
    console.log('时间线出现"bash"等工具痕迹:', /bash|glob|write/i.test(after));
    await page.screenshot({ path: 'D:/intelligence-agent/.workbuddy/ui_after_session.png' });
  } else {
    console.log('未找到可点击的会话条目');
  }
} catch (e) {
  console.log('打开会话失败:', String(e).slice(0, 200));
}

console.log('=== console 错误 ===');
console.log(errors.length ? [...new Set(errors)].join('\n') : '(无)');
await browser.close();
