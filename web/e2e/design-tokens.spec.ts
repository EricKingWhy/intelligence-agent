/** #1364 设计 token 的**像素**证据（P0-1 主色替换 + P0-5 双主题）。
 *
 *  为什么是像素而不是 computedStyle：`getComputedStyle` 只能证明"声明里写着 #533afd"，
 *  证明不了落屏的那一格真的是那个颜色（层叠、覆盖、`color-mix`、伪元素都能把它盖掉）。
 *  本票的验收原文是「判据是截图 CTA 像素取色断言等于 #533afd 的 RGB(83,58,253)，非肉眼」。
 *
 *  三件事：
 *   1. 会话列表 + 顶栏（dark/light）截图 —— 改前/改后同场景对比的"改后"那一半；
 *   2. accent 链接 + focus ring（dark/light）截图 —— 主色在文字/焦点上的落点；
 *   3. 主 CTA（`.plan-approval-btn.primary`，`background: var(--accent)`）元素截图，
 *      取**众数像素**断言 RGB(83,58,253)。取众数而不是取中心点：按钮中央压着文字，
 *      中心点会取到字形或抗锯齿边；填充色在整块里是压倒性的多数。
 *
 *  截图落 `docs/gate/<HEAD>/screens/`（票面要求）。`GATE_SCREENS_DIR` /
 *  `GATE_SCREENS_PREFIX` 可覆盖落点与前缀：origin/main 干净构建产"改前"基线时用
 *  `GATE_SCREENS_PREFIX=before-` 把同一组场景写进同一个目录。
 */

import { execSync } from 'node:child_process';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import { RUN, SID, T, fulfillSse, routeApi, sessionRow, submitTask, type FrameSpec } from './fixtures';

/** Playwright 的 cwd 是 `web/`（config 所在目录），仓库根是它上一级。 */
const REPO_ROOT = path.resolve(process.cwd(), '..');

function headSha(): string {
  return process.env.GATE_SHA ?? execSync('git rev-parse HEAD', { cwd: REPO_ROOT }).toString().trim();
}

const SCREENS_DIR = process.env.GATE_SCREENS_DIR ?? path.join(REPO_ROOT, 'docs', 'gate', headSha(), 'screens');
const PREFIX = process.env.GATE_SCREENS_PREFIX ?? '';

function screensPath(name: string): string {
  mkdirSync(SCREENS_DIR, { recursive: true });
  return path.join(SCREENS_DIR, `${PREFIX}${name}`);
}

/** 主题走 localStorage `ahi.theme`（`App` 启动时读它写 `html[data-theme]`）。 */
async function setTheme(page: Page, theme: 'dark' | 'light'): Promise<void> {
  await page.evaluate((value) => localStorage.setItem('ahi.theme', value), theme);
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
}

/** 截图前把动画停掉：入口动画/呼吸态会让两次运行落在不同帧，像素众数被过渡色污染。 */
async function freeze(page: Page): Promise<void> {
  await page.emulateMedia({ reducedMotion: 'reduce' });
}

const SESSIONS = [
  sessionRow(SID, { id: 'ws-1', title: 'intelligence-agent' }),
  sessionRow('e2e-session-0002', { id: 'ws-1', title: 'intelligence-agent' }, { first_user_message: '把 token 收敛到 8 档' }),
  sessionRow('e2e-session-0003', null, { first_user_message: '审查 e2e 的截图基线' }),
];

test('#1364 截图：会话列表 + 顶栏（dark/light）', async ({ page }) => {
  await freeze(page);
  await routeApi(page, { sessions: SESSIONS, events: [] });
  await page.goto('/');
  await expect(page.locator('.session-item').first()).toBeVisible();

  for (const theme of ['dark', 'light'] as const) {
    await setTheme(page, theme);
    await expect(page.locator('.appbar')).toBeVisible();
    await page.screenshot({ path: screensPath(`sessions-topbar-${theme}.png`), fullPage: true });
  }
});

test('#1364 截图：accent 链接 + focus ring（dark/light）', async ({ page }) => {
  await freeze(page);
  const text = '见 [文档](https://example.com/docs)，正文一行。';
  const frames: FrameSpec[] = [
    { type: 'session/started', seq: 1, session_id: SID, run_id: RUN, time: T },
    { type: 'run/started', seq: 2, session_id: SID, run_id: RUN, time: T },
    { type: 'user/message', data: { content: '给个出处' }, seq: 3, session_id: SID, run_id: RUN, step_id: 1, time: T },
    { type: 'text/delta', data: { delta: text }, seq: 4, session_id: SID, run_id: RUN, step_id: 1, time: T },
    { type: 'model/completed', data: { content: text }, seq: 5, session_id: SID, run_id: RUN, step_id: 1, time: T },
    { type: 'run/completed', data: {}, seq: 6, session_id: SID, run_id: RUN, time: T },
  ];
  await routeApi(page, { sessions: SESSIONS, events: frames, onSessionPost: (route) => fulfillSse(route, frames) });
  await page.goto('/');
  await submitTask(page, '给个出处');
  const link = page.locator('.model-output .md-link').first();
  await expect(link).toBeVisible();

  for (const theme of ['dark', 'light'] as const) {
    await setTheme(page, theme);
    await page.locator('.session-item').first().click();
    await expect(page.locator('.model-output .md-link').first()).toBeVisible();
    // 链接色必须是 accent 的**文字**档：暗色 #9a8bff、亮色 #533afd（index.css 注释里的实测值）。
    await expect(page.locator('.model-output .md-link').first()).toHaveCSS(
      'color',
      theme === 'dark' ? 'rgb(154, 139, 255)' : 'rgb(83, 58, 253)',
    );
    await page.locator('.model-output').last().screenshot({ path: screensPath(`accent-link-${theme}.png`) });

    // focus ring：先 `.focus()` 再走一次键盘往返，把 focus-visible 的启发式打开
    // （程序化 focus 不算键盘交互，Chromium 不会画 `:focus-visible` 环）。
    // 焦点落在链接上而不是发送键：run 已完成，`.composer-send` 是 disabled、永远拿不到焦点。
    const target = page.locator('.model-output .md-link').first();
    await target.focus();
    await page.keyboard.press('Shift+Tab');
    await page.keyboard.press('Tab');
    await expect(target).toBeFocused();
    await page.locator('.model-output').last().screenshot({ path: screensPath(`focus-ring-${theme}.png`) });
  }
});

/** 元素截图的众数不透明像素。PNG 解码交给浏览器自己（canvas），不引新依赖。 */
async function dominantColor(page: Page, png: Buffer): Promise<[number, number, number]> {
  const dataUrl = `data:image/png;base64,${png.toString('base64')}`;
  return page.evaluate(async (url) => {
    const image = new Image();
    image.src = url;
    await image.decode();
    const canvas = document.createElement('canvas');
    canvas.width = image.naturalWidth;
    canvas.height = image.naturalHeight;
    const context = canvas.getContext('2d');
    if (!context) throw new Error('canvas 2d context unavailable');
    context.drawImage(image, 0, 0);
    const { data } = context.getImageData(0, 0, canvas.width, canvas.height);
    const counts = new Map<string, number>();
    for (let index = 0; index < data.length; index += 4) {
      if (data[index + 3] < 255) continue;
      const key = `${data[index]},${data[index + 1]},${data[index + 2]}`;
      counts.set(key, (counts.get(key) ?? 0) + 1);
    }
    let best = '';
    let bestCount = -1;
    for (const [key, count] of counts) {
      if (count > bestCount) {
        best = key;
        bestCount = count;
      }
    }
    return best.split(',').map(Number) as [number, number, number];
  }, dataUrl);
}

/** 暂停在计划审批的帧剧本（与 plan-approval.spec.ts 同形：卡片落屏的最小充分集）。 */
function planPausedFrames(): FrameSpec[] {
  const request = 'design-tokens-plan-1';
  return [
    { type: 'session/started', seq: 1, session_id: SID, run_id: RUN, time: T },
    { type: 'run/started', seq: 2, session_id: SID, run_id: RUN, time: T },
    { type: 'user/message', data: { content: '先给个计划' }, seq: 3, session_id: SID, run_id: RUN, step_id: 1, time: T },
    {
      type: 'user/input-requested',
      seq: 4,
      session_id: SID,
      run_id: RUN,
      step_id: 1,
      time: T,
      data: {
        request_id: request,
        kind: 'plan_approval',
        run_id: RUN,
        plan_markdown: '# 计划\n\n1. 换 accent\n2. 跑门禁',
        touched_files: ['web/src/index.css'],
        artifact_id: 'art-plan-token',
        plan_sha256: 'deadbeef',
      },
    },
    {
      type: 'run/paused',
      seq: 5,
      session_id: SID,
      run_id: RUN,
      step_id: 1,
      time: T,
      data: { reason: 'user_input', input_request_id: request, budget_version: 1 },
    },
  ];
}

test('#1364 主 CTA 像素 = RGB(83,58,253)（dark/light）', async ({ page }) => {
  await freeze(page);
  const frames = planPausedFrames();
  await routeApi(page, {
    sessions: SESSIONS,
    events: frames,
    // 暂停 run 的传输语义同 plan-approval.spec.ts：POST 流干净收尾、重连快照
    // `has_active_run: false` ⇒ 客户端收流后退回 viewing，卡片才落屏。
    onWs: () => ({ events: frames, hasActiveRun: false }),
  });
  await page.goto('/');

  for (const theme of ['dark', 'light'] as const) {
    await setTheme(page, theme);
    await page.locator('.session-item').first().click();
    const cta = page.locator('.plan-approval-btn.primary');
    await expect(cta).toBeVisible();

    const png = await cta.screenshot({ path: screensPath(`cta-${theme}.png`) });
    const [red, green, blue] = await dominantColor(page, png);
    console.log(`[${theme}] CTA dominant pixel = rgb(${red}, ${green}, ${blue})`);
    expect([red, green, blue], `${theme} 主 CTA 的众数像素必须是 Stripe Indigo #533afd`).toEqual([83, 58, 253]);
  }
});
