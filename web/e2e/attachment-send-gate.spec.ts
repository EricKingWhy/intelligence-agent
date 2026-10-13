/** #1027（[Web 附图] 附件未就绪时发送被放行）：发送门禁 + 剥离兜底的端到端判定。
 *
 * 为什么必须 e2e（组件测试覆盖不到的部分）：
 *  - 「上传 POST 返回前发送」这个时序只能真走 XHR：组件测试里上传是 mock promise，
 *    拿不到"请求已发出、响应还没回"的真实在途态；
 *  - 断言"无 /messages 落盘"要在真路由层钉——组件测试的 onSubmit mock 只能证明
 *    "没调回调"，不能证明"没发请求"。
 *
 * 手法：`onAttachmentPost` 里**扣住 route 不 fulfill**（= 上传 POST 在途），此时
 * 发送 ⇒ 门禁必须拦下；放行 route（= 200 回来）⇒ chip 变 ready，门禁解除。
 * 这是把票面复现步骤（"在上传 POST 完成前 Ctrl+Enter 发送"）做成了确定性时序，
 * 不是 sleep 赌博。
 *
 * 会话前置与 image-attachments.spec.ts 同形（localStorage 预置选中会话 + 列表行）。
 */

import { expect, test, type Page, type Route } from '@playwright/test';
import { fulfillSse, routeApi, type ApiMock, type FrameSpec } from './fixtures';

const SID = 'g1027-session-0001';
const RUN1 = 'g1027-run-0001';
const RUN2 = 'g1027-run-0002';
const T = '2026-10-13T00:00:00Z';
const SELECTED_KEY = 'ahi.selectedSession';

/** 3×3 真实 PNG（与 image-attachments.spec.ts 同一份字节：浏览器真解得了）。 */
const PNG_3PX = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAMAAAADCAIAAADZSiLoAAAAEElEQVR42mPQb3gLQQxYWADStA59LlJElwAAAABJRU5ErkJggg==',
  'base64',
);

const FIRST_FRAMES: FrameSpec[] = [
  { type: 'session/started', seq: 1, session_id: SID, run_id: RUN1, time: T },
  { type: 'run/started', seq: 2, session_id: SID, run_id: RUN1, time: T },
  { type: 'user/message', data: { content: '先建个会话' }, seq: 3, session_id: SID, run_id: RUN1, step_id: 1, time: T },
  { type: 'model/completed', data: { content: '好的' }, seq: 4, session_id: SID, run_id: RUN1, step_id: 1, time: T },
  { type: 'run/completed', data: {}, seq: 5, session_id: SID, run_id: RUN1, time: T },
];

const sessionRow = {
  session_id: SID,
  event_count: FIRST_FRAMES.length,
  first_event_time: T,
  last_event_time: T,
  first_user_message: '先建个会话',
  trace_id: null,
  trace_url: null,
};

async function openSession(page: Page, mock: Omit<ApiMock, 'sessions'>): Promise<void> {
  await routeApi(page, { sessions: [sessionRow], events: [...FIRST_FRAMES], ...mock });
  await page.addInitScript(([key, value]) => localStorage.setItem(key, value), [SELECTED_KEY, SID]);
  await page.goto('/');
  await expect(page.locator('.session-item.selected')).toHaveCount(1);
  await expect(page.getByLabel('Agent 任务')).toBeEnabled({ timeout: 10_000 });
}

/** 在页面里构造一次**真**粘贴（Node 侧造不出真 DataTransfer）。 */
async function pastePng(page: Page, name: string): Promise<void> {
  await page.evaluate(
    ({ fileName, base64 }) => {
      const dt = new DataTransfer();
      const bytes = Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
      dt.items.add(new File([bytes], fileName, { type: 'image/png' }));
      const target = document.querySelector<HTMLTextAreaElement>('#composer-input')!;
      target.focus();
      target.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
    },
    { fileName: name, base64: PNG_3PX.toString('base64') },
  );
}

function receiptBody(attachmentId: string): string {
  return JSON.stringify({
    attachment_id: attachmentId,
    media_type: 'image/png',
    bytes: PNG_3PX.length,
    width: 3,
    height: 3,
    name: 'shot.png',
  });
}

test('#1027-1：上传 POST 返回前发送被拦截——无 /messages 落盘 + 阻断提示；ready 后发送带完整 attachments', async ({
  page,
}) => {
  const attachmentId = `sha256:${'e'.repeat(64)}`;
  let messagesBody: Record<string, unknown> | null = null;
  let messagesCount = 0;
  /** 扣住的上传 route（= 上传 POST 在途）：测试主动放行前不 fulfill。 */
  let heldUpload: Route | null = null;

  await openSession(page, {
    onAttachmentPost: async (route) => {
      heldUpload = route;
    },
    onMessagesPost: (route) => {
      messagesCount += 1;
      messagesBody = (route.request().postDataJSON() ?? {}) as Record<string, unknown>;
      const later: FrameSpec[] = [
        { type: 'run/started', seq: 6, session_id: SID, run_id: RUN2, time: T },
        {
          type: 'user/message',
          data: {
            content: '看这张图',
            attachments: [
              { kind: 'image', attachment_id: attachmentId, media_type: 'image/png', bytes: PNG_3PX.length, width: 3, height: 3 },
            ],
          },
          seq: 7,
          session_id: SID,
          run_id: RUN2,
          step_id: 2,
          time: T,
        },
        { type: 'model/completed', data: { content: '看到了' }, seq: 8, session_id: SID, run_id: RUN2, step_id: 2, time: T },
        { type: 'run/completed', data: {}, seq: 9, session_id: SID, run_id: RUN2, time: T },
      ];
      return fulfillSse(route, later);
    },
  });

  // 附图 → 上传 POST 已发出、响应还没回（chip 在途态）。
  await pastePng(page, 'shot.png');
  const card = page.locator('.composer-attach-card');
  await expect(card).toHaveCount(1);
  await expect(card).toHaveAttribute('data-status', 'uploading');
  await expect.poll(() => heldUpload !== null).toBe(true);

  // ── 票面复现：在上传 POST 返回前发送 ──
  await page.getByLabel('Agent 任务').fill('看这张图');
  const sendButton = page.getByRole('button', { name: '发送', exact: true });
  await expect(sendButton).toBeDisabled();
  await expect(sendButton).toHaveAttribute('title', /附件上传中，请等待完成/);
  // 阻断提示可见（不只藏在 title 里）。
  await expect(page.locator('.composer-send-blocked')).toContainText('附件上传中，请等待完成');

  // Ctrl+Enter 走另一条路：同样被拦截。给 1s 让任何漏网的请求有机会发出。
  await page.keyboard.press('Control+Enter');
  await page.waitForTimeout(1000);
  // 无 /messages 落盘 = 票面 seq 82 那种"纯文本 user/message"根本没产生。
  expect(messagesCount).toBe(0);
  expect(messagesBody).toBeNull();

  // ── 上传 200 回来 → chip ready → 门禁自动解除 ──
  await heldUpload!.fulfill({
    status: 200,
    contentType: 'application/json',
    body: receiptBody(attachmentId),
  });
  await expect(card).toHaveAttribute('data-status', 'ready');
  await expect(page.locator('.composer-send-blocked')).toHaveCount(0);
  await expect(sendButton).toBeEnabled();

  // ── ready 后发送：请求体带完整 attachments（对照票面 seq 25 正例）──
  await sendButton.click();
  await expect.poll(() => messagesBody, { timeout: 10_000 }).not.toBeNull();
  expect(messagesCount).toBe(1);
  expect(messagesBody?.['attachments']).toEqual([attachmentId]);
  // 全就绪发送 ⇒ 兜底提示不得出现。
  await expect(page.locator('.composer-attach-dropped')).toHaveCount(0);
});

test('#1027-2：上传失败（413）→ chip error 态；发送不被禁用，失败项被显式剔除并提示', async ({
  page,
}) => {
  let messagesBody: Record<string, unknown> | null = null;

  await openSession(page, {
    onAttachmentPost: async (route) => {
      await route.fulfill({
        status: 413,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'AttachmentMessageTooLarge: 图片超过上限' }),
      });
    },
    onMessagesPost: (route) => {
      messagesBody = (route.request().postDataJSON() ?? {}) as Record<string, unknown>;
      return fulfillSse(route, [
        { type: 'run/started', seq: 6, session_id: SID, run_id: RUN2, time: T },
        { type: 'user/message', data: { content: '没有图也照发' }, seq: 7, session_id: SID, run_id: RUN2, step_id: 2, time: T },
        { type: 'model/completed', data: { content: '收到' }, seq: 8, session_id: SID, run_id: RUN2, step_id: 2, time: T },
        { type: 'run/completed', data: {}, seq: 9, session_id: SID, run_id: RUN2, time: T },
      ]);
    },
  });

  await pastePng(page, 'too-big.png');
  const card = page.locator('.composer-attach-card');
  await expect(card).toHaveCount(1);
  // chip 显式 error 态（复用 #825 既有的三态：uploading/ready/failed，本票未新增
  // 状态机，只把"失败不阻塞发送 + 显式剔除 + 可见提示"的语义钉死）。
  await expect(card).toHaveAttribute('data-status', 'failed');
  await expect(page.locator('.composer-attach-failed')).toContainText('AttachmentMessageTooLarge');

  // #825 AC4 保留：失败不阻塞文本发送。
  await page.getByLabel('Agent 任务').fill('没有图也照发');
  const sendButton = page.getByRole('button', { name: '发送', exact: true });
  await expect(sendButton).toBeEnabled();
  await sendButton.click();

  // 请求发出但**没有** attachments 键（失败项被剔除，不是静默混入）。
  await expect.poll(() => messagesBody, { timeout: 10_000 }).not.toBeNull();
  expect('attachments' in (messagesBody ?? {})).toBe(false);
  // 兜底：一次性可见提示「N 个附件未随消息发出」——全程无静默丢弃路径。
  const dropped = page.locator('.composer-attach-dropped');
  await expect(dropped).toContainText('1 个附件未随消息发出');
  // 失败草稿留在栏里供重试（既有行为不变）。
  await expect(card).toHaveCount(1);
  // 提示可关闭。
  await dropped.getByRole('button', { name: '关闭附件未发出提示' }).click();
  await expect(page.locator('.composer-attach-dropped')).toHaveCount(0);
});
