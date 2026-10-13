/** #1015（MM-04 缺口）：新建态懒建会话 —— 新建态 → 贴图 → 发送 → 渲染。
 *
 * 为什么必须 e2e（组件测试覆盖不到的部分）：
 *   - `ensureSession` 的真实现活在 `App.tsx`（`createEmptySession` + `selectSession` +
 *     `refreshSessions` 的串联），组件测试里它只是个 mock；
 *   - 「只建出**一个**会话」「上传打到新会话的 per-session 端点上」「发送走
 *     `/messages`（空闲会话 → launched 直驱新 run）」这三条都是跨 App/hook/后端的
 *     协作语义；
 *   - 粘贴的真 `DataTransfer` 与 XHR 上传（jsdom 侧已在组件测试里钉住）。
 *
 * mock 与真后端同形：`POST /api/sessions?launch=false` 按 #204 裁定 §2（只建会话、
 * 不启动 run、无 run 帧）；`onSessionPost` 自铺 launch=false 分支是因为 fixture 的
 * 默认分支要求 `cwd`（`project-context.spec.ts` 的同一手法），而本 spec 恰要覆盖
 * 「无项目语境的新建态」（不发 cwd 键——真后端 `cwd` 可缺省，`web/app.py:591`）。
 */

import { expect, test, type Page } from '@playwright/test';
import {
  fulfillSse,
  routeApi,
  sessionRow,
  type ApiMock,
  type FrameSpec,
} from './fixtures';

const T = '2026-10-13T00:00:00Z';
const SID = 'empty-1';
const RUN1 = 'lazy-run-0001';
const ATTACHMENT_ID = `sha256:${'b'.repeat(64)}`;

/** 3×3 真实 PNG（`image-attachments.spec.ts` 同款：浏览器真解得了，缩略图与消息图
 *  的 `naturalWidth` 断言才有意义）。 */
const PNG_3PX = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAMAAAADCAIAAADZSiLoAAAAEElEQVR42mPQb3gLQQxYWADStA59LlJElwAAAABJRU5ErkJggg==',
  'base64',
);

/** 在页面里构造一次**真**粘贴（Node 侧造不出真 DataTransfer）。
 *  拷贝自 `image-attachments.spec.ts::pasteFiles`（同一手法，避免各自复制后漂移）。 */
async function pasteFiles(
  page: Page,
  files: Array<{ name: string; type: string; bytes: Buffer }>,
): Promise<void> {
  await page.evaluate((specs) => {
    const dt = new DataTransfer();
    for (const spec of specs) {
      const bytes = Uint8Array.from(atob(spec.base64), (c) => c.charCodeAt(0));
      dt.items.add(new File([bytes], spec.name, { type: spec.type }));
    }
    const target = document.querySelector<HTMLTextAreaElement>('#composer-input')!;
    target.focus();
    target.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
  }, files.map((f) => ({ name: f.name, type: f.type, base64: f.bytes.toString('base64') })));
}

/** 新开一轮 run 的帧（空闲会话 → launched 直驱新 run，ADR-0030 §5.1）。 */
function laterFrames(): FrameSpec[] {
  return [
    { type: 'run/started', seq: 2, session_id: SID, run_id: RUN1, time: T },
    {
      type: 'user/message',
      data: {
        content: '看这张图',
        attachments: [
          { kind: 'image', attachment_id: ATTACHMENT_ID, media_type: 'image/png', bytes: PNG_3PX.length, width: 3, height: 3 },
        ],
      },
      seq: 3,
      session_id: SID,
      run_id: RUN1,
      step_id: 1,
      time: T,
    },
    { type: 'model/completed', data: { content: '看到了' }, seq: 4, session_id: SID, run_id: RUN1, step_id: 1, time: T },
    { type: 'run/completed', data: {}, seq: 5, session_id: SID, run_id: RUN1, step_id: 1, time: T },
  ];
}

test('新建态粘贴 → 懒建恰好一个会话 → 上传打到新会话 → 图文同发 → 消息内渲染', async ({
  page,
}) => {
  const sessions: Record<string, unknown>[] = [];
  const durableLog: FrameSpec[] = [];
  /** `POST /api/sessions` 的可观测形状（断言"只建一次"与"launch=false"）。 */
  const sessionPosts: string[] = [];
  /** 上传请求的 URL（断言"打到新会话的 per-session 端点上"）。 */
  const uploadUrls: string[] = [];
  let messagesBody: Record<string, unknown> | null = null;

  const mock: ApiMock = {
    sessions,
    projects: [],
    events: durableLog,
    onSessionPost: async (route) => {
      const url = new URL(route.request().url());
      sessionPosts.push(route.request().url());
      if (url.searchParams.get('launch') !== 'false') {
        throw new Error(`本 spec 只走懒建路径，意外请求：${route.request().url()}`);
      }
      // 懒建：按真后端语义（#204 裁定 §2）——只建会话不启动 run；无项目语境时不带
      // cwd（真后端 `cwd` 可缺省）。sid 命名沿 fixture 默认分支的 `empty-N` 口径。
      const sid = `empty-${sessions.length + 1}`;
      sessions.unshift(sessionRow(sid, null, { event_count: 1, first_user_message: null }));
      durableLog.push({ type: 'session/started', seq: 1, session_id: sid, time: T });
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ session_id: sid, permission_mode: 'workspace-write' }),
      });
    },
    onAttachmentPost: async (route) => {
      uploadUrls.push(route.request().url());
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          attachment_id: ATTACHMENT_ID,
          media_type: 'image/png',
          bytes: PNG_3PX.length,
          width: 3,
          height: 3,
          name: 'shot.png',
        }),
      });
    },
    onMessagesPost: (route) => {
      messagesBody = (route.request().postDataJSON() ?? {}) as Record<string, unknown>;
      const later = laterFrames();
      durableLog.push(...later);
      return fulfillSse(route, later);
    },
    // 受控读回：引用已随 user/message 落盘（`durableLog`），直接给字节。
    onAttachmentContentGet: async (route, id) => {
      if (id !== ATTACHMENT_ID) return false;
      await route.fulfill({ status: 200, contentType: 'image/png', body: PNG_3PX });
      return true;
    },
  };
  await routeApi(page, mock);
  // 不预置选中会话 → 新建态（`暂无对话`空态）。
  await page.goto('/');

  // ── 新建态：空态 + 附图入口可用（#1015 新语义：不再禁用）──
  await expect(page.locator('.empty-hero')).toContainText('暂无对话');
  await expect(page.getByRole('button', { name: '添加图片' })).toBeEnabled();

  // ── 贴图 → 懒建 → 上传 ──
  await pasteFiles(page, [{ name: 'shot.png', type: 'image/png', bytes: PNG_3PX }]);
  const card = page.locator('.composer-attach-card');
  await expect(card).toHaveCount(1);
  await expect(card).toHaveAttribute('data-status', 'ready');

  // 会话只在首次投喂时创建一次。
  await expect.poll(() => sessionPosts.length).toBe(1);
  expect(sessionPosts[0]).toContain('launch=false');
  // 上传打到新会话的 per-session 端点上（session_id == 懒建回执的 id）。
  expect(uploadUrls).toHaveLength(1);
  expect(uploadUrls[0]).toContain(`/api/sessions/${SID}/attachments`);
  // 侧栏出现新会话（`refreshSessions` 后）。
  await expect(page.locator('.session-item')).toHaveCount(1);

  // ── 图文同发：走 /messages（空闲会话 → launched 直驱新 run），不另造会话 ──
  await page.getByLabel('Agent 任务').fill('看这张图');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await expect.poll(() => messagesBody, { timeout: 10_000 }).not.toBeNull();
  expect(messagesBody?.['attachments']).toEqual([ATTACHMENT_ID]);
  expect(sessionPosts).toHaveLength(1);
  await expect(card).toHaveCount(0);

  // ── 消息内渲染（走受控端点）──
  const messageImage = page.locator('.msg-images .msg-image-img').first();
  await expect(messageImage).toBeVisible({ timeout: 10_000 });
  const contentPath = `/api/sessions/${SID}/attachments/${encodeURIComponent(ATTACHMENT_ID)}/content`;
  expect(await messageImage.getAttribute('src')).toContain(contentPath);
  await expect
    .poll(() => messageImage.evaluate((el) => (el as HTMLImageElement).naturalWidth > 0))
    .toBe(true);
});

test('懒建失败：可见错误、无上传、纯文本仍可发（#1015 消融 #4 的 e2e 版）', async ({
  page,
}) => {
  const sessions: Record<string, unknown>[] = [];
  const durableLog: FrameSpec[] = [];
  const uploadUrls: string[] = [];

  const mock: ApiMock = {
    sessions,
    projects: [],
    events: durableLog,
    onSessionPost: async (route) => {
      const url = new URL(route.request().url());
      if (url.searchParams.get('launch') === 'false') {
        // 懒建 500（真后端的非 2xx 形态之一）。
        await route.fulfill({
          status: 500,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'boom' }),
        });
        return;
      }
      // 纯文本发送走既有 submitTask 新建路径（本票未改）：按真后端语义建会话 + 起流。
      const body = (route.request().postDataJSON() ?? {}) as Record<string, unknown>;
      const sid = `plain-${sessions.length + 1}`;
      const content = typeof body.task === 'string' ? body.task : '新任务';
      sessions.unshift(sessionRow(sid, null, { first_user_message: content }));
      const frames: FrameSpec[] = [
        { type: 'session/started', seq: 1, session_id: sid, run_id: 'plain-run-1', time: T },
        { type: 'run/started', seq: 2, session_id: sid, run_id: 'plain-run-1', time: T },
        {
          type: 'user/message',
          data: { content },
          seq: 3,
          session_id: sid,
          run_id: 'plain-run-1',
          step_id: 1,
          time: T,
        },
        { type: 'model/completed', data: { content: '收到' }, seq: 4, session_id: sid, run_id: 'plain-run-1', step_id: 1, time: T },
        { type: 'run/completed', data: {}, seq: 5, session_id: sid, run_id: 'plain-run-1', step_id: 1, time: T },
      ];
      durableLog.push(...frames);
      await fulfillSse(route, frames);
    },
    onAttachmentPost: async (route) => {
      uploadUrls.push(route.request().url());
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
    },
  };
  await routeApi(page, mock);
  await page.goto('/');

  await expect(page.locator('.empty-hero')).toContainText('暂无对话');
  await pasteFiles(page, [{ name: 'shot.png', type: 'image/png', bytes: PNG_3PX }]);

  // 可见错误（AC3 同一通道），无"永远上传中"卡片，零上传。
  const alert = page.locator('.composer-attach-error[role="alert"]');
  await expect(alert).toContainText('会话创建失败');
  await expect(page.locator('.composer-attach-card')).toHaveCount(0);
  expect(uploadUrls).toHaveLength(0);

  // 纯文本仍可发（守住 AC4）：走既有 submitTask 新建路径，不受懒建失败影响。
  await page.getByLabel('Agent 任务').fill('纯文本');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  // 新会话建出来、回答渲染出来 = 发送链路没被懒建失败卡死。
  await expect(page.locator('.session-item')).toHaveCount(1);
  await expect(page.getByText('收到')).toBeVisible({ timeout: 10_000 });
});
