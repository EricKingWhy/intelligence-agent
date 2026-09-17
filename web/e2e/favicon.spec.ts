/** #224：标签页图标。
 *
 *  真机现象（巡检 round 2）：HTML 没声明任何 icon，浏览器按默认行为去要
 *  `/favicon.ico` ⇒ 每次加载一条 404。噪声之外还有一层：真 404 混在假 404 里
 *  更难发现。
 *
 *  这里锁的是**声明 → 可获取的、真的在画东西的图**这条链，不是"index.html 里
 *  有一行 link"（后者是文本子串断言，改个 href 到不存在的文件它照样绿）。
 */

import { expect, test } from '@playwright/test';
import { routeApi } from './fixtures';

test('#224：声明的 icon 必须能取到，且不是渲染为空白 sprite', async ({ page }) => {
  await routeApi(page, {});
  await page.goto('/');

  // 从**活 DOM** 取声明，不是读 index.html 文本：锁的是浏览器实际看到的那份
  // 文档（构建期/插件改写声明也会在这里露出来）。
  const hrefs = await page.evaluate(() =>
    Array.from(document.querySelectorAll('link[rel~="icon"]')).map(
      (el) => (el as HTMLLinkElement).href,
    ),
  );
  expect(hrefs.length, '必须有 icon 声明，否则浏览器回头去要 /favicon.ico').toBeGreaterThan(0);

  for (const href of hrefs) {
    const res = await page.request.get(href);
    expect(res.status(), `${href} 取不到 ⇒ 声明是假的`).toBe(200);

    const body = await res.text();
    expect(body).toContain('<svg');
    // 「只有 <symbol>/<defs>、没有根图形」= 渲染出来是空白。这是当时否掉
    // `public/icons.svg` 的判据，写成断言：把 href 指回它，这条必须红。
    // 先把**声明性**节点整体剥掉再找图形——直接找 `<path>` 是假绿：sprite 里的
    // 每个 `<symbol>` 内部都是 `<path>`，连空白 sprite 都能"通过"。
    const drawable = body
      .replace(/<(symbol|defs)\b[\s\S]*?<\/\1>/g, '')
      .replace(/<!--[\s\S]*?-->/g, '');
    expect(drawable, '只有 symbol/defs 的 sprite 当 icon 是空白').toMatch(
      /<(path|circle|rect|line|polyline|polygon|g)\b/,
    );
  }
});
