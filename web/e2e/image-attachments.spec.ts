/** #825（MM-04）AC12：图片附件的端到端闭环。
 *
 * 为什么必须 e2e（组件测试覆盖不到的部分，全在这里）：
 *   - 粘贴/拖放的 `DataTransfer` 形状（jsdom 没有可构造的真 DataTransfer）；
 *   - 上传走 `XMLHttpRequest.upload.onprogress`（真进度只能真发）；
 *   - 缩略图 `createImageBitmap` + canvas 编码（jsdom 无这两个 API ⇒ 组件测试里
 *     永远走"缩略图生成失败"的兜底分支，`data:` URL 那条路压根没被执行过）；
 *   - 受控读回的**授权闸门**（未被本会话事件引用的 id 一律 404 ⇒ 发送前只能本地
 *     预览、发送后才从端点取字节）与"刷新后图仍在"（GET /events 重放 + `img` 真的
 *     解码出非零尺寸）。
 *
 * mock 与真后端同形（`web/attachments.py`）：上传回 `sha256:<64 hex>`；读回只在 id
 * 被本会话某条 `user/message` 引用时给字节（见 `fixtures.ts::attachmentState`）——
 * 所以"发送前的草稿拿不到受控读回"这条在 e2e 里也是真的，不是靠断言绕过去。
 *
 * 会话前置：`localStorage` 预置选中的会话 id（`k-refresh-restore.spec.ts` 的同一手法，
 * 也是刷新后必然走的那条代码路径）+ 列表里有对应行 ⇒ 进站即绑定该会话，Composer 的
 * 附图入口直接可用；发送走 `/messages`（空闲会话 → 同形 SSE）。
 */

import { expect, test, type Page } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import { MODELS, fulfillSse, routeApi, type ApiMock, type FrameSpec } from './fixtures';

const SID = 'mm04-session-0001';
const RUN1 = 'mm04-run-0001';
const RUN2 = 'mm04-run-0002';
const T = '2026-10-08T00:00:00Z';
const SELECTED_KEY = 'ahi.selectedSession';

/** 3×3 真实 PNG。
 *
 *  **必须是浏览器真的解得了的字节**：本 spec 的两条核心断言（草稿缩略图是 `data:`
 *  URL、消息内 `naturalWidth > 0`）都要求这张图能被解码；随手写的 base64 很容易
 *  得到 `createImageBitmap` 的 `InvalidStateError`（实测：常见的 1×1 占位串就解不了，
 *  于是缩略图静默走文件名兜底、消息图变成坏图——断言会红，但红在一个与真实行为无关的
 *  夹具问题上）。生成方式：Python `zlib`+`struct` 手写 IHDR/IDAT/IEND，3×3 RGB。 */
const PNG_3PX = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAMAAAADCAIAAADZSiLoAAAAEElEQVR42mPQb3gLQQxYWADStA59LlJElwAAAABJRU5ErkJggg==',
  'base64',
);

/** 已收口的首轮（会话空闲 ⇒ Composer 可发、附图入口可用）。 */
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

/** 粘贴用的一张图：`bytes` 给真实字节（Buffer）或一个长度（只关心大小的用例）。 */
interface IntakeFile {
  name: string;
  type: string;
  bytes: number | Buffer;
}

async function openSession(page: Page, mock: Omit<ApiMock, 'sessions'>): Promise<void> {
  await routeApi(page, { sessions: [sessionRow], events: [...FIRST_FRAMES], ...mock });
  await page.addInitScript(([key, value]) => localStorage.setItem(key, value), [SELECTED_KEY, SID]);
  await page.goto('/');
  await expect(page.locator('.session-item.selected')).toHaveCount(1);
  await expect(page.getByLabel('Agent 任务')).toBeEnabled({ timeout: 10_000 });
}

/** 在页面里构造一次**真**粘贴（Node 侧造不出真 DataTransfer）。 */
async function pasteFiles(page: Page, files: IntakeFile[]): Promise<void> {
  await page.evaluate((specs) => {
    const dt = new DataTransfer();
    for (const spec of specs) {
      const bytes =
        spec.base64 !== null
          ? Uint8Array.from(atob(spec.base64), (c) => c.charCodeAt(0))
          : new Uint8Array(spec.size ?? 0);
      // M-20 起前端做字节探测（detectImageMediaType，fail-closed）：只关心大小的图片
      // 替身若为零字节，会在格式检查就被"不支持的图片格式"拒收。前 8 字节写真实 PNG
      // 魔数（其余保持零），让尺寸检查（AC3 的超限路径）真正走到——这是探测正确工作
      // 的证据，不是绕过。
      if (spec.zeroFilledImage && bytes.length >= 8) {
        bytes.set(Uint8Array.of(0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a));
      }
      dt.items.add(new File([bytes], spec.name, { type: spec.type }));
    }
    const target = document.querySelector<HTMLTextAreaElement>('#composer-input')!;
    target.focus();
    target.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
  }, files.map((f) => ({
    name: f.name,
    type: f.type,
    size: Buffer.isBuffer(f.bytes) ? null : f.bytes,
    base64: Buffer.isBuffer(f.bytes) ? f.bytes.toString('base64') : null,
    zeroFilledImage: !Buffer.isBuffer(f.bytes) && f.type.startsWith('image/'),
  })));
}

/** 在页面里构造一次**真**拖放（dragenter 只需 `types` 含 'Files'，drop 才带文件）。 */
async function dragInPage(page: Page, phase: 'enter' | 'drop', bytes = 1): Promise<void> {
  await page.evaluate(
    ({ phase, bytes }) => {
      // M-20 起前端按字节探测图片格式（fail-closed）：图片替身必须带真实 PNG 魔数，
      // 否则 drop 的文件在 intake 就被"不支持的图片格式"拒收（enter 只看 types，不
      // 走 intake，行为不变）。魔数计入总长度：前 8 字节为签名，其余保持零。
      const payload = new Uint8Array(bytes);
      payload.set(Uint8Array.of(0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a).subarray(0, Math.min(8, bytes)));
      const dt = new DataTransfer();
      dt.items.add(new File([payload], phase === 'enter' ? 'probe.png' : 'dropped.png', { type: 'image/png' }));
      document.dispatchEvent(
        new DragEvent(phase === 'enter' ? 'dragenter' : 'drop', { dataTransfer: dt, bubbles: true, cancelable: true }),
      );
    },
    { phase, bytes },
  );
}

/** 新开一轮 run 的帧（本轮用户消息带附件引用——真后端在投递时 append 这条事件）。 */
function laterFrames(attachmentId: string, content: string): FrameSpec[] {
  return [
    { type: 'run/started', seq: 6, session_id: SID, run_id: RUN2, time: T },
    {
      type: 'user/message',
      data: {
        content,
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
}

test('AC1/AC2/AC4/AC5/AC6：粘贴 → 缩略图 → 发送带 attachments → 消息内渲染 → 刷新仍在 → 看大图', async ({ page }) => {
  // 可变引用：投递时补入本轮故事（真后端 append 在投递时），收尾回读与刷新都拿它。
  const durableLog: FrameSpec[] = [...FIRST_FRAMES];
  const attachmentId = `sha256:${'a'.repeat(64)}`;
  /** 每条上传请求的可观测形状（真后端按字节 + 声明的 Content-Type 判定，这里钉请求侧）。 */
  const uploads: Array<{ name: string | null; contentType: string | undefined; bytes: Buffer | null }> = [];
  let messagesBody: Record<string, unknown> | null = null;

  await openSession(page, {
    onAttachmentPost: async (route) => {
      uploads.push({
        name: new URL(route.request().url()).searchParams.get('name'),
        contentType: route.request().headers()['content-type'],
        bytes: route.request().postDataBuffer(),
      });
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          attachment_id: attachmentId,
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
      const later = laterFrames(attachmentId, '看这张图');
      durableLog.push(...later);
      return fulfillSse(route, later);
    },
    // 读回必须与上面的自定义上传口**配对**：自定义 `onAttachmentPost` 不走 fixtures 的
    // 字节仓库（那份仓库只服务缺省上传分支），所以受控读回也得在这里显式应答——
    // 否则请求落到缺省闸门（未登记 id ⇒ 404），界面上就是「加载失败」。
    onAttachmentContentGet: async (route, attachmentId_) => {
      if (attachmentId_ !== attachmentId) return false;
      await route.fulfill({ status: 200, contentType: 'image/png', body: PNG_3PX });
      return true;
    },
    // 事件端点读耐久日志的**同一引用**：投递后重读才看得到附件引用（AC5 的刷新）。
    events: durableLog,
  });

  // ── AC1 粘贴 ──
  await pasteFiles(page, [{ name: 'shot.png', type: 'image/png', bytes: PNG_3PX }]);

  // ── AC2 预发送缩略图 + 预算 ──
  const card = page.locator('.composer-attach-card');
  await expect(card).toHaveCount(1);
  await expect(card).toHaveAttribute('data-status', 'ready');
  // 缩略图是真 `data:` URL 且真解码（AC9：CSP 只放行 `img-src 'self' data:`，
  // `blob:` 会被浏览器拦掉——这两条就是"没走 blob、也不是"文件名兜底"的机器证据）。
  const thumb = page.locator('img.composer-attach-thumb');
  await expect(thumb).toHaveCount(1);
  expect((await thumb.getAttribute('src'))?.startsWith('data:image/')).toBe(true);
  await expect.poll(() => thumb.evaluate((el) => (el as HTMLImageElement).naturalWidth)).toBe(3);
  // 注意：附图栏自己带 `aria-label="待发送图片"`，`getByLabel('发送')` 会同时命中它
  // （非精确匹配）——所以本 spec 一律用 role + exact 定位发送按钮。
  await expect(page.locator('.composer-attach-budget')).toContainText('已附 1/20 张');
  // 上传请求的形状（`?name=` + 如实转述的 Content-Type + 原始字节）——真后端按字节判定，
  // 客户端不许"帮忙修正"声明，这条钉住的就是它。
  expect(uploads).toEqual([{ name: 'shot.png', contentType: 'image/png', bytes: PNG_3PX }]);

  // ── AC1 拖放通道：遮罩在场 → drop 入栏 ──
  await dragInPage(page, 'enter');
  await expect(page.locator('.drop-mask')).toBeVisible();
  await expect(page.locator('.drop-title')).toContainText('松开鼠标');
  await dragInPage(page, 'drop', 16);
  await expect(page.locator('.drop-mask')).toHaveCount(0);
  await expect(card).toHaveCount(2);
  // 拖放那张只作"通道可用"的证据，移除掉，保证下面的发送只带粘贴那张。
  await card.nth(1).locator('.composer-attach-remove').click();
  await expect(card).toHaveCount(1);

  // ── AC1 文件选择器通道 ──
  await page.locator('input[type="file"]').setInputFiles({ name: 'picked.png', mimeType: 'image/png', buffer: PNG_3PX });
  await expect(card).toHaveCount(2);
  // 三条通道（粘贴 / 拖放 / 选择器）都真的走到了上传端点，且每条都用**自己的**文件名。
  await expect.poll(() => uploads.map((u) => u.name)).toEqual(['shot.png', 'dropped.png', 'picked.png']);
  await card.nth(1).locator('.composer-attach-remove').click();
  await expect(card).toHaveCount(1);

  // ── AC4 发送：附件引用进请求体 ──
  await page.getByLabel('Agent 任务').fill('看这张图');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await expect.poll(() => messagesBody, { timeout: 10_000 }).not.toBeNull();
  expect(messagesBody?.['attachments']).toEqual([attachmentId]);
  // 已发送的草稿离开栏（下一轮不会重复带同一张）。
  await expect(card).toHaveCount(0);

  // ── AC5 消息内渲染（走受控端点，不是 data: 拷贝）──
  const messageImage = page.locator('.msg-images .msg-image-img').first();
  await expect(messageImage).toBeVisible({ timeout: 10_000 });
  // id 含 `:`，URL 里必然是 `%3A` 转义形态（`attachmentContentUrl` 走 encodeURIComponent）。
  const contentPath = `/api/sessions/${SID}/attachments/${encodeURIComponent(attachmentId)}/content`;
  expect(await messageImage.getAttribute('src')).toContain(contentPath);
  // 真解码（naturalWidth > 0）= 字节真的从端点回来了，不是坏图占位。
  await expect
    .poll(() => messageImage.evaluate((el) => (el as HTMLImageElement).naturalWidth > 0))
    .toBe(true);

  // ── AC5 刷新后仍在（事件重放 + 端点读回）──
  await page.reload();
  const afterReload = page.locator('.msg-images .msg-image-img').first();
  await expect(afterReload).toBeVisible({ timeout: 10_000 });
  await expect
    .poll(() => afterReload.evaluate((el) => (el as HTMLImageElement).naturalWidth > 0))
    .toBe(true);

  // ── AC6 点开看大图 + 复制/下载原图 ──
  await page.locator('.msg-image-btn').first().click();
  const lightbox = page.locator('.image-lightbox');
  await expect(lightbox).toBeVisible();
  await expect(lightbox.locator('.image-lightbox-img')).toBeVisible();
  await expect
    .poll(() => lightbox.locator('.image-lightbox-img').evaluate((el) => (el as HTMLImageElement).naturalWidth > 0))
    .toBe(true);
  await expect(lightbox.getByRole('button', { name: '复制原图' })).toBeVisible();
  const download = lightbox.locator('a.image-lightbox-btn');
  // 展示名缺省（写入路径不持久化 name）⇒ 回落「图片」，不伪造文件名。
  await expect(download).toHaveAttribute('download', '图片');
  expect(await download.getAttribute('href')).toContain(contentPath);
  // 遮挡检查（独立审查 P1 的回归闸门）：Radix 把 Overlay 与 Content 渲染成 body 下的
  // 兄弟节点，两者都 fixed + 显式 z-index ⇒ 遮罩一旦被抬到内容之上，`inset: 0` 会把
  // 指针事件全接走：按钮"可见"（`toBeVisible` 不查遮挡）但点不到。这里直接问浏览器
  // "这个坐标上最顶层的元素是谁"，并让 Playwright 的 hit-target 检查去点一次真实下载。
  for (const selector of ['.image-lightbox-btn', 'a.image-lightbox-btn']) {
    const topmost = await lightbox.locator(selector).first().evaluate((el) => {
      const rect = el.getBoundingClientRect();
      const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
      return hit !== null && hit.closest('.image-lightbox') !== null;
    });
    expect(topmost).toBe(true);
  }
  const cdp = await page.context().newCDPSession(page);
  let cdpDownloadRequest: { url: string; method: string } | undefined;
  cdp.on('Fetch.requestPaused', async ({ requestId, request }) => {
    cdpDownloadRequest = { url: request.url, method: request.method };
    await cdp.send('Fetch.fulfillRequest', {
      requestId,
      responseCode: 200,
      responseHeaders: [
        { name: 'Content-Type', value: 'image/png' },
        { name: 'Content-Disposition', value: 'attachment; filename="shot.png"' },
      ],
      body: PNG_3PX.toString('base64'),
    });
  });
  await cdp.send('Fetch.enable', { patterns: [{ urlPattern: `*${contentPath}`, requestStage: 'Request' }] });
  const downloadStart = page.waitForEvent('download');
  await download.click();
  // Chromium 的原生 <a download> 请求绕过 Playwright route；用 CDP 只拦截这次真实点击的 GET，
  // 回同一份 PNG 字节，再核对下载成功且字节未变。Playwright 点击命中检查仍覆盖遮罩遮挡。
  const downloaded = await downloadStart;
  expect(downloaded.url()).toContain(contentPath);
  expect(cdpDownloadRequest?.method).toBe('GET');
  expect(cdpDownloadRequest?.url).toContain(contentPath);
  expect(await downloaded.failure()).toBeNull();
  expect(await readFile(await downloaded.path())).toEqual(PNG_3PX);
  await cdp.send('Fetch.disable');
  await cdp.detach();
  await page.keyboard.press('Escape');
  await expect(lightbox).toHaveCount(0);
});

test('AC3：超限整批被拒，就地给出文件名与具体上限，且一个字节都没上传', async ({ page }) => {
  let uploadCount = 0;
  await openSession(page, {
    onAttachmentPost: async (route) => {
      uploadCount += 1;
      await route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"本用例不应发生上传"}' });
    },
  });

  // 21 MiB > 单张上限 20 MiB（`IMAGE_LIMITS.maxImageBytes`，与后端默认值同源）。
  await pasteFiles(page, [
    { name: 'huge.png', type: 'image/png', bytes: 21 * 1024 * 1024 },
    { name: 'ok.png', type: 'image/png', bytes: 16 },
  ]);

  const alert = page.locator('.composer-attach-error[role="alert"]');
  await expect(alert).toBeVisible();
  await expect(alert).toContainText('huge.png');
  await expect(alert).toContainText('超过单张上限 20 MiB');
  // 整批被拒：合法的那张也不许偷偷进去（预检在 intake 之前，用户看到的与实际发生的同形）。
  await expect(page.locator('.composer-attach-card')).toHaveCount(0);
  expect(uploadCount).toBe(0);
  // 常驻：只有用户关掉才消失。
  await page.locator('button[aria-label="关闭错误提示"]').click();
  await expect(alert).toHaveCount(0);
});

test('AC7：supports_vision=false 禁用附图入口并可见说明；粘贴/拖放都不入栏，纯文本通道不受影响', async ({ page }) => {
  await openSession(page, {
    // 能力位在目录条目上（`supports_vision`）；前端三态解析：只有显式 false 才禁用。
    models: [{ name: 'text-only', provider: 'local', model: 'text-only', default: true, supports_vision: false }],
  });

  const attachButton = page.getByRole('button', { name: '添加图片' });
  await expect(attachButton).toBeDisabled();
  await expect(page.locator('.composer-attach-hint')).toContainText('不支持视觉');
  await expect(attachButton).toHaveAttribute('title', /不支持视觉/);

  // 拖放：遮罩给的是**禁止** + 原因（不是"松开即可"，也不静默）。
  await dragInPage(page, 'enter', 16);
  await expect(page.locator('.drop-title')).toContainText('不支持视觉');
  await dragInPage(page, 'drop', 16);
  await expect(page.locator('.composer-attach-card')).toHaveCount(0);

  // 粘贴同样不入栏（入口禁用 = 三条通道一起关，不留后门）。
  await pasteFiles(page, [{ name: 'a.png', type: 'image/png', bytes: 16 }]);
  await expect(page.locator('.composer-attach-card')).toHaveCount(0);

  // 纯文本通道不受影响：空文本时发送不可用，输入后可发（AC7 只关附图）。
  await expect(page.getByRole('button', { name: '发送', exact: true })).toBeDisabled();
  await page.getByLabel('Agent 任务').fill('文本照样能发');
  await expect(page.getByRole('button', { name: '发送', exact: true })).toBeEnabled();
});

test('AC7（后半）：已附图的历史轮在非视觉模型下标注「图已被省略」', async ({ page }) => {
  // 历史轮直接预置在事件流里（本次不发消息）：数据源是**事件的引用**，与是否刚从
  // Composer 发出来无关——这正是"刷新/重入后仍在"的同一份事实。
  const historical = `sha256:${'b'.repeat(64)}`;
  await openSession(page, {
    models: [{ name: 'text-only', provider: 'local', model: 'text-only', default: true, supports_vision: false }],
    events: [
      ...FIRST_FRAMES,
      { type: 'run/started', seq: 6, session_id: SID, run_id: RUN2, time: T },
      {
        type: 'user/message',
        data: {
          content: '这是带图的历史消息',
          attachments: [
            { kind: 'image', attachment_id: historical, media_type: 'image/png', bytes: PNG_3PX.length, width: 3, height: 3 },
          ],
        },
        seq: 7,
        session_id: SID,
        run_id: RUN2,
        step_id: 1,
        time: T,
      },
      { type: 'model/completed', data: { content: '好' }, seq: 8, session_id: SID, run_id: RUN2, step_id: 1, time: T },
      { type: 'run/completed', data: {}, seq: 9, session_id: SID, run_id: RUN2, time: T },
    ],
    // 该 id 已被 `user/message` 引用 ⇒ 授权闸门本身会放行；这里仍显式提供真字节，
    // 让"图与标注同轮共存"这条断言落在真解码上（不是坏图占位）。
    onAttachmentContentGet: async (route) => {
      await route.fulfill({ status: 200, contentType: 'image/png', body: PNG_3PX });
      return true; // 契约：返回假值 = 未处理，会落回**默认授权闸门**（对本用例是 404）。
    },
  });

  const omitted = page.locator('.msg-images-omitted');
  await expect(omitted).toHaveCount(1);
  await expect(omitted).toContainText('图已被省略');
  // 图**没有**被删掉：历史附图照常显示（标注是附加事实，不是替换）。
  const image = page.locator('.msg-images .msg-image-img').first();
  await expect(image).toBeVisible({ timeout: 10_000 });
  await expect
    .poll(() => image.evaluate((el) => (el as HTMLImageElement).naturalWidth > 0))
    .toBe(true);
});

test('AC7：supports_vision 缺席（后端沉默）不得被当成"不支持"', async ({ page }) => {
  await openSession(page, { models: MODELS });
  await expect(page.getByRole('button', { name: '添加图片' })).toBeEnabled();
  await expect(page.locator('.composer-attach-hint')).toHaveCount(0);
});

/** AC8 前置：队列里有一条**带图**的排队项。
 *
 *  图片引用来自事件流（`message/queued.data.attachments`），队列内容来自 `GET /queue`
 *  补齐——真后端的 `/queue` **不下发**附件，于是"补齐不得抹掉事件流已知的引用"这条
 *  也就落在了真实路径上（`restoreUndeliveredFromQueue` 按 id 保留）。
 */
const QUEUED_ID = 'mm04-queued-0001';
const QUEUED_IMAGE = `sha256:${'c'.repeat(64)}`;

async function openQueuedWithImage(page: Page): Promise<{ bodies: Record<string, unknown>[] }> {
  const bodies: Record<string, unknown>[] = [];
  await openSession(page, {
    events: [
      ...FIRST_FRAMES,
      {
        type: 'message/queued',
        data: {
          queue_id: QUEUED_ID,
          content: '排队里的图',
          attachments: [
            { kind: 'image', attachment_id: QUEUED_IMAGE, media_type: 'image/png', bytes: PNG_3PX.length, width: 3, height: 3 },
          ],
        },
        seq: 6,
        session_id: SID,
        run_id: RUN1,
        time: T,
      },
    ],
    onQueueGet: (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: [{ queue_id: QUEUED_ID, content: '排队里的图', created_at: T }],
          steers: [],
        }),
      }),
    onMessagesPost: (route) => {
      bodies.push(JSON.parse(route.request().postData() ?? '{}'));
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'steered', mode: 'steer' }),
      });
    },
  });
  await expect(page.locator('.queue-item')).toHaveCount(1);
  return { bodies };
}

test('AC8：队列条「立即」重投递带回复图引用（否则图静默消失）', async ({ page }) => {
  const { bodies } = await openQueuedWithImage(page);
  await page.locator('.queue-item').getByRole('button', { name: '立即发送' }).click();
  await expect.poll(() => bodies.length).toBe(1);
  // 不带 attachments = 后端按本请求重建 user/message ⇒ 队列项里的图静默消失。
  expect(bodies[0]).toMatchObject({
    content: '排队里的图',
    mode: 'steer',
    queue_id: QUEUED_ID,
    attachments: [QUEUED_IMAGE],
  });
});

test('AC8：队列条「编辑」重投递同样带回复图引用', async ({ page }) => {
  const { bodies } = await openQueuedWithImage(page);
  await page.locator('.queue-item').getByRole('button', { name: '编辑排队消息' }).click();
  await page.getByLabel('编辑排队消息内容').fill('排队里的图（改过文案）');
  await page.getByRole('button', { name: '保存排队消息' }).click();
  await expect.poll(() => bodies.length).toBe(1);
  expect(bodies[0]).toMatchObject({
    content: '排队里的图（改过文案）',
    mode: 'queue',
    queue_id: QUEUED_ID,
    attachments: [QUEUED_IMAGE],
  });
});

// ── #826（MM-05）：桌面宿主路径桥在场时的拖入分流 ────────────────────────────
//
// AC7「复用同一份 spec，不另开测试接缝」在这里落地：桌面加载的就是这一份 React 应用，
// 与 Web 的唯一差别是 preload 往页面多写了一个全局桥（`desktop/src/preload.cts` 的
// `window.__IA_HOST_PATHS__`）。所以桌面语境 = 本 spec + 注入那一个全局；那个字面量的
// 跨包守卫在 `desktop/test/preload-host-paths.test.ts`，桥自身的收窄语义（谁拿到它、
// `pathFor` 只对真从磁盘来的 File 回路径）由 desktop 侧的 vm 用例负责，这里不重复造。
// 真 Electron 与 Chromium 的 `DataTransfer`/File 语义同源，分流判据完全落在前端这一层。

/** 拖放用的真宿主路径：**故意含空白**，顺带钉住引号形态。 */
const HOST_DROP_PATH = 'C:\\Users\\tester\\My Documents\\notes.pdf';

/** 页面里的 preload 桥替身：只对指定文件名的文件返回宿主路径（其余一律空串，与 Electron 同形）。 */
async function installHostPathBridge(page: Page, fileName: string, realPath: string): Promise<void> {
  const args: [string, string] = [fileName, realPath];
  await page.addInitScript((arg: [string, string]) => {
    Object.defineProperty(globalThis, '__IA_HOST_PATHS__', {
      configurable: true,
      value: { pathFor: (file: File) => (file.name === arg[0] ? arg[1] : '') },
    });
  }, args);
}

/** 在页面里构造一次真拖放并投递任意文件（`dragInPage` 只投图片，分流要看非图片）。 */
async function dropFiles(page: Page, files: Array<{ name: string; type: string }>): Promise<void> {
  await page.evaluate((specs) => {
    const dataTransfer = new DataTransfer();
    for (const spec of specs) {
      // M-20 起前端按字节探测图片格式（fail-closed）：image/* 替身须带真实 PNG 魔数
      // 才能走到上传路径；非图片保持无签名（它们本就该被格式检查拒收）。
      const content = spec.type.startsWith('image/')
        ? Uint8Array.of(0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a)
        : new Uint8Array(1);
      dataTransfer.items.add(new File([content], spec.name, { type: spec.type }));
    }
    document.dispatchEvent(new DragEvent('drop', { dataTransfer, bubbles: true, cancelable: true }));
  }, files);
}

test('#826 AC1/AC2：桌面桥在场 → 非图片拖入变 @path 引用（不上传字节），图片仍走上传', async ({ page }) => {
  const uploads: string[] = [];
  await installHostPathBridge(page, 'notes.pdf', HOST_DROP_PATH);
  await openSession(page, {
    onAttachmentPost: async (route) => {
      uploads.push(new URL(route.request().url()).searchParams.get('name') ?? '');
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          attachment_id: `sha256:${'d'.repeat(64)}`,
          media_type: 'image/png',
          bytes: PNG_3PX.length,
          width: 3,
          height: 3,
        }),
      });
    },
  });

  // ① 非图片 + 有真实路径 → `@path` 引用进输入框（含空白 ⇒ 引号形态），一个字节都没上传。
  await dropFiles(page, [{ name: 'notes.pdf', type: 'application/pdf' }]);
  const input = page.getByLabel('Agent 任务');
  await expect(input).toHaveValue(`@"${HOST_DROP_PATH}" `);
  await expect(page.locator('.composer-attach-card')).toHaveCount(0);
  expect(uploads).toEqual([]);

  // ② 图片（即使有真实路径）→ 照旧上传：AC2 的另一半。
  await input.fill('');
  await dropFiles(page, [{ name: 'shot.png', type: 'image/png' }]);
  await expect(page.locator('.composer-attach-card')).toHaveCount(1);
  await expect.poll(() => uploads).toEqual(['shot.png']);

  // ③ 桥回空串的非图片（未主动选择 / 无磁盘后端）→ 原样进既有上传入口，得到既有拒绝文案：
  //    桥不给路径时前端**不会凭空造一个**，也就绕不过既有的预检（AC3 的负向面）。
  await dropFiles(page, [{ name: 'other.pdf', type: 'application/pdf' }]);
  await expect(page.locator('.composer-attach-error[role="alert"]')).toContainText('other.pdf');
  await expect(input).toHaveValue('');
});

test('#826 AC2（Web 侧不变）：无桥 → 非图片仍是既有拒绝，绝不产生 @path 引用', async ({ page }) => {
  await openSession(page, {
    onAttachmentPost: async (route) => {
      await route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"本用例不应发生上传"}' });
    },
  });
  await dropFiles(page, [{ name: 'notes.pdf', type: 'application/pdf' }]);
  await expect(page.locator('.composer-attach-error[role="alert"]')).toContainText('notes.pdf');
  await expect(page.getByLabel('Agent 任务')).toHaveValue('');
});
