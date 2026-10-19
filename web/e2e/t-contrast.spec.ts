/** UI-02（R2/R3）对比度与排版地板回归锁 + U-1 review 加固项。
 *
 *  锁（暗/亮两主题各一遍）：
 *  1. 会话元数据（.session-item-meta）≥12px 且对比度 ≥4.5:1
 *  2. 时间线序号（.tl-seq）≥12px 且对比度 ≥4.5:1
 *  3. composer placeholder 对比度 ≥4.5:1
 *  4. 时间线事件类型名（.tl-type）== 解析后的 --color-muted-foreground（R1，等值断言）
 *  5. U-1 review 新增 ink 元素：.approval-chip / .approval-deny .approval-kbd /
 *     .composer-locked-hint 对比度 ≥4.5:1（写 UI 时新立的三处，不许回退）
 *  6. #1030 / #1136 的 ink-mix 使用点：.run-pulse.pulse-awaiting-decision、恢复冲突条，
 *     以及 .app-error / .app-notice / .recover-btn / .auth-banner / .task-review-* 族
 *     （语义色当正文色用，亮色下靠 `color-mix(<sem> 45%, --text-primary)` 压到达标）
 *  7. #1190 约束管理页的 `--cn-*` 墨面：.cn-value / .cn-type / .cn-meta / .cn-pill /
 *     .cn-revoke 对比度 ≥4.5，且 .cn-type/.cn-meta/.cn-pill 守 12px 地板
 *  8. #1269 项目语境武装提示（.rail-project-armed-hint）对比度 ≥4.5 且守 12px 地板
 *  9. #1183 新建任务的项目路径入口（.project-path-callout-toggle）对比度 ≥4.5 且守 12px 地板
 * 10. #1312 Composer 会话级目标状态行（.composer-session-target）12px 地板 + 对比度 ≥4.5，
 *     及「改为新会话」入口（.composer-session-target-new-btn，accent 色）对比度 ≥4.5
 *
 *  对比度算法：WCAG 相对亮度；背景取元素到根的祖先 backgroundColor 逐层
 *  alpha 合成（body 的 gradient 不参与——background-color 落在最后一层 canvas）。
 *
 *  ⚠ 已知测量局限（U-1 review 声明）：本 harness 不对 `backdrop-filter`
 *  （玻璃材质的可视底 ≠ backgroundColor）与祖先 `opacity` 建模——被测元素
 *  链上出现这两者时数值会偏离真实观感；当前选择器链均不含。
 *  解析器对未知颜色格式 **throw**（不是回退白底）：Chromium 换计算值格式时
 *  必须显式失败，不许静默量错。 */

import { expect, type Page, test } from '@playwright/test';
import {
  AGENT_PROFILES,
  MODELS,
  PERMISSION_MODES,
  REASONING_EFFORTS,
  SID,
  criterion,
  evidenceRecord,
  fulfillSse,
  mockAuthRequired,
  mockTaskReviewApi,
  routeApi,
  sessionRow,
  sseFrame,
  submitTask,
  verification,
  T,
  type FrameSpec,
  type TaskReviewFixture,
} from './fixtures';

const ROW = {
  session_id: 's-contrast',
  event_count: 5,
  first_event_time: T,
  last_event_time: T,
  first_user_message: '对比度回归',
  trace_id: null,
  trace_url: null,
};

const EVENTS = [
  { type: 'session/started', seq: 1, session_id: ROW.session_id, run_id: 'r1', time: T },
  { type: 'run/started', seq: 2, session_id: ROW.session_id, run_id: 'r1', time: T },
  { type: 'user/message', data: { content: '对比度回归' }, seq: 3, session_id: ROW.session_id, run_id: 'r1', step_id: 1, time: T },
  { type: 'run/completed', data: {}, seq: 4, session_id: ROW.session_id, run_id: 'r1', time: T },
  {
    type: 'tool/approval-requested',
    data: {
      approval_id: 'ap-contrast', tool_name: 'write', tool_call_id: 'tc-1',
      action_type: 'workspace-write', title: 'write (workspace-write)',
      description: '工具授权级别为 workspace-write，但当前策略为只读（read-only）。',
      arguments_preview: { path: 'demo.txt', content: 'HELLO' },
      permission: 'workspace-write', policy: 'read-only',
      reason: '工具授权级别为 workspace-write，但当前策略为只读（read-only）。',
      allowed_decisions: ['deny', 'approve_once'],
    },
    seq: 5, session_id: ROW.session_id, run_id: 'r1', step_id: 1, time: T,
  },
];

async function openWorkspace(page: Page, theme: 'dark' | 'light'): Promise<void> {
  await routeApi(page, {
    sessions: [ROW],
    events: EVENTS,
    models: MODELS,
    permissionModes: PERMISSION_MODES,
    agentProfiles: AGENT_PROFILES,
    reasoningEfforts: REASONING_EFFORTS,
  });
  // 主题必须显式引导（localStorage，paint 前生效）：headless 的
  // prefers-color-scheme 是 light，不引导则 [dark] 用例量到的是亮色主题。
  await page.addInitScript((t) => localStorage.setItem('ahi.theme', t), theme);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/');
  await page.locator('.session-item').first().click();
  await page.locator('.turn').first().waitFor();
  // 审批卡在场（提供 chip / deny kbd / locked-hint 三个被测元素）
  await page.locator('.approval-card').waitFor();
}

interface Measure {
  fontSize: string;
  color: [number, number, number, number];
  bg: [number, number, number];
}

/** 页内测量：computed fontSize/color + 祖先 backgroundColor alpha 合成后的有效底色。 */
function measure(page: Page, selector: string): Promise<Measure> {
  return page.evaluate((sel: string) => {
    const el = document.querySelector(sel);
    if (!el) throw new Error(`no element: ${sel}`);
    const parse = (c: string): [number, number, number, number] => {
      const rgb = c.match(/rgba?\(([^)]+)\)/i);
      if (rgb) {
        const p = rgb[1].split(',').map((s) => parseFloat(s.trim()));
        return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
      }
      // color-mix 的计算值在现代 Chromium 可能是 oklab(...)（如选中项的粉色染底）
      const ok = c.match(/oklab\(([^)]+)\)/i);
      if (ok) {
        const parts = ok[1].split('/').map((s) => s.trim());
        const [L, a, b] = parts[0].split(/\s+/).map(parseFloat);
        const alpha = parts.length > 1 ? parseFloat(parts[1]) : 1;
        const l_ = L + 0.3963377774 * a + 0.2158037573 * b;
        const m_ = L - 0.1055613458 * a - 0.0638541728 * b;
        const s_ = L - 0.0894841775 * a - 1.291485548 * b;
        const l = l_ ** 3;
        const m = m_ ** 3;
        const s = s_ ** 3;
        const lin = [
          4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
          -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
          -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
        ];
        const enc = (v: number) => {
          const x = Math.min(1, Math.max(0, v));
          return x <= 0.0031308 ? 12.92 * x : 1.055 * x ** (1 / 2.4) - 0.055;
        };
        return [enc(lin[0]) * 255, enc(lin[1]) * 255, enc(lin[2]) * 255, alpha];
      }
      // 同一份 color-mix 在该 Chromium 的另一计算路径是 color(srgb r g b / a)
      // （r/g/b 为 0-1 gamma 编码 sRGB——CSS Color 4 规定 srgb 带 transfer function）。
      const csr = c.match(/color\(srgb\s+([^)]+)\)/i);
      if (csr) {
        const parts = csr[1].split('/');
        const comps = parts[0].trim().split(/\s+/).map(parseFloat);
        const alpha = parts.length > 1 ? parseFloat(parts[1]) : 1;
        return [comps[0] * 255, comps[1] * 255, comps[2] * 255, alpha];
      }
      throw new Error(`unparsed color: ${c}`);
    };
    const cs = getComputedStyle(el);
    const stack: [number, number, number, number][] = [];
    let node: Element | null = el;
    while (node) {
      const c = parse(getComputedStyle(node).backgroundColor);
      stack.push(c);
      if (c[3] >= 1) break;
      node = node.parentElement;
    }
    if (stack[stack.length - 1][3] < 1) throw new Error(`no opaque ancestor bg: ${sel}`);
    let out: [number, number, number] = [
      stack[stack.length - 1][0],
      stack[stack.length - 1][1],
      stack[stack.length - 1][2],
    ];
    for (let i = stack.length - 2; i >= 0; i -= 1) {
      const c = stack[i];
      out = [
        c[0] * c[3] + out[0] * (1 - c[3]),
        c[1] * c[3] + out[1] * (1 - c[3]),
        c[2] * c[3] + out[2] * (1 - c[3]),
      ];
    }
    const color = parse(cs.color);
    return { fontSize: cs.fontSize, color, bg: out };
  }, selector);
}

function contrast(fg: [number, number, number, number], bg: [number, number, number]): number {
  const chan = (v: number) => {
    const s = v / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  const lum = (c: number[]) => 0.2126 * chan(c[0]) + 0.7152 * chan(c[1]) + 0.0722 * chan(c[2]);
  const fgLum = lum([fg[0] * fg[3] + bg[0] * (1 - fg[3]), fg[1] * fg[3] + bg[1] * (1 - fg[3]), fg[2] * fg[3] + bg[2] * (1 - fg[3])]);
  const bgLum = lum(bg);
  const [hi, lo] = fgLum >= bgLum ? [fgLum, bgLum] : [bgLum, fgLum];
  return (hi + 0.05) / (lo + 0.05);
}

/* ── #1136：亮色主题语义色「当正文色用」的对比度回归锁 ──
 *
 *  票面症状：亮色下 `.app-error`（--danger）、`.app-notice`（--info）、`.recover-btn`
 *  （--warning，hover 22% 底更低）全部 <4.5，而暗色同三项达标——根因是**字色 token
 *  本身**（在纯白底上限就 <4.5），调染色底 alpha 无解。修法同 #1029 的 ink-mix：
 *  `color-mix(in srgb, <sem> 45%, var(--text-primary))`（亮色 --text-primary 是深墨、
 *  混入即压深；暗色是近白、混入即提亮、对比度只升不降），装饰（描边/底/图标）继续
 *  引用原 token。
 *  浏览器实测（本文件 stdout，Chromium）：修前亮色 .app-error 3.6740 / .app-notice
 *  3.8574 / .recover-btn 2.9375（票面给的 3.53/3.71/2.97 是 12%/14% 染色底的理论复算，
 *  真机略高——以真机为准）；修后亮色 ≥6.05（最紧 .recover-btn:hover）、暗色 ≥7.49。
 *  （注：修前纯白底上限 warning 3.73 / danger 4.45；修后数值见 GREEN 日志。）
 *  三个夹具各自走一条**真实渲染路径**（不是 createElement 影子元素），因此把字色
 *  改回纯 --danger #cf4a46 / --info #4a6fd6 / --warning #b07a10 时对应用例必然变红。 */

/** `.app-error`（分叉失败横幅，App.tsx:1433 forkError）夹具：两轮已完成会话。
 *  为什么是这个夹具：只有「第 2 轮的 .fork-btn + 后端 422」才经 handleFork 的
 *  catch 走到 `setForkError`，渲染出 className="app-error" role="alert"
 *  （App.tsx:1434）——真实路径。user/message 的 seq 与 turn 序号刻意错开
 *  （2 / 30，照 b-fork.spec.ts 同型夹具），避免「锚点误用 turn 序号」的回归把
 *  本用例干扰成另一种失败。 */
const FORK_SID = 's-contrast-fork';
const FORK_ROW = {
  session_id: FORK_SID,
  event_count: 11,
  first_event_time: T,
  last_event_time: T,
  first_user_message: '第一轮问题',
  trace_id: null,
  trace_url: null,
};
function forkTwoTurnEvents(): FrameSpec[] {
  return [
    { type: 'session/started', seq: 1, session_id: FORK_SID, time: T },
    { type: 'user/message', data: { content: '第一轮问题' }, seq: 2, session_id: FORK_SID, time: T },
    { type: 'run/started', data: { turn_index: 1 }, seq: 3, session_id: FORK_SID, run_id: 'rf-1', time: T },
    { type: 'text/delta', data: { delta: '第一轮回答。' }, seq: 4, session_id: FORK_SID, step_id: 1, run_id: 'rf-1', time: T },
    { type: 'model/completed', data: { content: '第一轮回答。' }, seq: 5, session_id: FORK_SID, step_id: 1, run_id: 'rf-1', time: T },
    { type: 'run/completed', data: {}, seq: 6, session_id: FORK_SID, run_id: 'rf-1', time: T },
    { type: 'user/message', data: { content: '第二轮问题' }, seq: 30, session_id: FORK_SID, time: T },
    { type: 'run/started', data: { turn_index: 2 }, seq: 31, session_id: FORK_SID, run_id: 'rf-2', time: T },
    { type: 'text/delta', data: { delta: '第二轮回答。' }, seq: 32, session_id: FORK_SID, step_id: 2, run_id: 'rf-2', time: T },
    { type: 'model/completed', data: { content: '第二轮回答。' }, seq: 33, session_id: FORK_SID, step_id: 2, run_id: 'rf-2', time: T },
    { type: 'run/completed', data: {}, seq: 34, session_id: FORK_SID, run_id: 'rf-2', time: T },
  ];
}

/** `.app-notice`（worktree 隔离横幅，App.tsx:1419 worktreeNotice）夹具：
 *  alpha 空项目 + beta 一条会话（照 u-project-task.spec.ts 的 baseMock），创建会话
 *  的响应带 `X-Worktree-Path` 头。为什么是这个夹具：只有「项目内新建任务」这条
 *  路径经 handleStartTaskInProject 传 onWorktreeCreated 回调，才把该横幅
 *  （role="status"，正文色 --info）渲染出来。响应体必须是**合法 SSE 帧序**：
 *  raceEarlyResponse 在响应头到达时即消费 X-Worktree-Path
 *  （worktreePathFromResponse），随后 attachLiveStream 再消费 body——帧序照默认
 *  mock 的 task 流（session/started → run/started → user/message → model/started
 *  → text/delta → model/completed → run/completed）。 */
const WT_ALPHA = 'D:/repos/alpha';
const WT_BETA = 'D:/repos/beta';
const WT_SID = 's-contrast-worktree';
const WT_TASK = '给登录接口加上限流';
const WT_PATH = '/tmp/e2e-worktree-1';
function worktreeStartFrames(task: string): FrameSpec[] {
  return [
    { type: 'session/started', data: { cwd: WT_ALPHA }, seq: 1, session_id: WT_SID, run_id: 'rw', time: T },
    { type: 'run/started', seq: 2, session_id: WT_SID, run_id: 'rw', time: T },
    { type: 'user/message', data: { content: task }, seq: 3, session_id: WT_SID, run_id: 'rw', step_id: 1, time: T },
    { type: 'model/started', data: { model: 'e2e-model' }, seq: 4, session_id: WT_SID, run_id: 'rw', step_id: 1, time: T },
    { type: 'text/delta', data: { delta: '好，我先看看这个目录。' }, seq: 5, session_id: WT_SID, run_id: 'rw', step_id: 1, time: T },
    { type: 'model/completed', data: { model: 'e2e-model' }, seq: 6, session_id: WT_SID, run_id: 'rw', step_id: 1, time: T },
    { type: 'run/completed', data: {}, seq: 7, session_id: WT_SID, run_id: 'rw', time: T },
  ];
}

/** `.recover-btn`（App.tsx:1447「恢复会话」按钮）夹具：崩溃会话（run 无终态 +
 *  一条未配对 tool/call，isRecoverableRun 为真），照 recovery-w13.spec.ts 的
 *  crashedEvents 同型。为什么是这个夹具：只有 canRecover 为真才渲染该按钮，
 *  它的正文色是 --warning、底色 14% 染色（hover 22%）——两个态都要量，因为 hover
 *  底更浓、对比度更低（票面实测 2.97 → 2.71）。 */
const CRASH_SID = 's-contrast-crash';
const CRASH_ROW = {
  session_id: CRASH_SID,
  event_count: 5,
  first_event_time: T,
  last_event_time: T,
  first_user_message: '崩溃前的问题',
  trace_id: null,
  trace_url: null,
};
function crashedEvents(): FrameSpec[] {
  return [
    { type: 'session/started', seq: 1, session_id: CRASH_SID, time: T },
    { type: 'user/message', data: { content: '崩溃前的问题' }, seq: 2, session_id: CRASH_SID, time: T },
    { type: 'run/started', data: { turn_index: 1 }, seq: 3, session_id: CRASH_SID, run_id: 'rc', time: T },
    { type: 'model/completed', data: { content: '开始执行。' }, seq: 4, session_id: CRASH_SID, step_id: 1, run_id: 'rc', time: T },
    {
      type: 'tool/call',
      data: { tool_call_id: 'call-1', tool_name: 'bash', args: { command: 'ls' } },
      seq: 5, session_id: CRASH_SID, step_id: 1, run_id: 'rc', time: T,
    },
  ];
}

/* ── #1136 P3-1/P3-2：task-review 族与 .auth-banner 的语义色正文对比度回归锁 ──
 *
 *  同 `.app-error` / `.app-notice` / `.recover-btn` 同一根因：这些组件把 `--warning`/
 *  `--danger`/`--info` 直接当**正文色**用，亮色下压在各自染色底上 <4.5（Chromium 实测
 *  3.1817–4.1042：tag.warn 3.18 / auth-banner 3.23 / evidence-unavailable 3.79 /
 *  chip.executing 4.10）。
 *  修法同 #1029 的 ink-mix（`color-mix(in srgb, <sem> 45%, var(--text-primary))`）——
 *  把语义色与 `--text-primary` 混深，装饰（描边/底/图标）继续引用原 token。
 *  本夹具走**真实渲染路径**（打开「任务审阅」面板 / 触发 401），不是造影子元素，
 *  所以把字色改回纯 `--warning`/`--danger`/`--info` 时对应用例必然变红。 */

/** 任务审阅面板的会话帧（与 `task-review.spec.ts` 的 FRAMES 同形）。 */
const TR_ROW = {
  session_id: SID,
  event_count: 4,
  first_event_time: T,
  last_event_time: T,
  first_user_message: '对比度任务审阅',
  trace_id: null,
  trace_url: null,
};
const TR_FRAMES: FrameSpec[] = [
  { type: 'session/started', seq: 1, session_id: SID, run_id: 'run-1', time: T },
  { type: 'run/started', seq: 2, session_id: SID, run_id: 'run-1', time: T },
  { type: 'user/message', data: { content: '对比度任务审阅' }, seq: 3, session_id: SID, run_id: 'run-1', step_id: 1, time: T },
  { type: 'run/completed', data: {}, seq: 4, session_id: SID, run_id: 'run-1', time: T },
];

/** 起会话 → 打开「文件/改动」面，并从按需入口打开详细审阅。 */
async function openTaskReview(
  page: Page,
  theme: 'dark' | 'light',
  state: TaskReviewFixture,
): Promise<void> {
  await routeApi(page, { sessions: [TR_ROW], events: TR_FRAMES, onSessionPost: (route) => fulfillSse(route, TR_FRAMES) });
  await mockTaskReviewApi(page, state);
  await page.addInitScript((t) => localStorage.setItem('ahi.theme', t), theme);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/');
  await submitTask(page, '对比度任务审阅');
  await page
    .getByRole('tablist', { name: '工作区面' })
    .getByRole('tab', { name: /^文件\/改动/ })
    .click();
  const summary = page.locator('.task-review-summary');
  await expect(summary).toBeVisible();
  await summary.getByRole('button', { name: '详细审阅' }).click();
  await expect(page.getByRole('dialog', { name: '详细审阅' })).toBeVisible();
}

for (const theme of ['dark', 'light'] as const) {
  test(`[${theme}] 元数据与时间线序号：12px 地板 + 对比度 ≥4.5（R2/R3）`, async ({ page }) => {
    await openWorkspace(page, theme);
    for (const selector of ['.session-item-meta', '.tl-seq']) {
      const m = await measure(page, selector);
      expect(m.fontSize, `${selector} font-size`).toBe('12px');
      const ratio = contrast(m.color, m.bg);
      expect(ratio, `${selector} contrast`).toBeGreaterThanOrEqual(4.5);
    }
  });

  /* #1269：项目语境武装提示（.rail-project-armed-hint）——本轮新立的行内 ink，按本轮
     双轴审查意见登记进地板锁（沿用 U-1 约定：新增可见文字元素必须在亮/暗双主题下量到
     ≥4.5 且不破 12px 地板，否则将来有人换色不会红）。 */
  test(`[${theme}] #1269 项目语境武装提示（.rail-project-armed-hint）对比度 ≥4.5`, async ({ page }) => {
    await routeApi(page, {
      sessions: [],
      projects: [{ id: 'p-contrast', path: 'D:/repos/contrast', title: '对比度项目', session_ids: [] }],
    });
    await page.addInitScript((t) => localStorage.setItem('ahi.theme', t), theme);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/');
    // 武装项目语境（#1028 的锚定入口）：菜单「新建会话」→ 行上出现显式提示。
    await page.locator('.rail-project-head .rail-menu-btn').first().click();
    await page.getByRole('menuitem', { name: '新建会话' }).click();
    await expect(page.locator('.rail-project-armed-hint')).toBeVisible();
    const m = await measure(page, '.rail-project-armed-hint');
    expect(m.fontSize, 'armed hint font-size').toBe('12px');
    const ratio = contrast(m.color, m.bg);
    expect(ratio, '.rail-project-armed-hint contrast').toBeGreaterThanOrEqual(4.5);
  });

  /* #1183：新建任务路径展开入口是新增的可见 ink；锁住暗/亮主题的可读性，避免 accent 或字号回退。 */
  test(`[${theme}] #1183 新建任务项目路径入口对比度 ≥4.5 且字号 ≥12px`, async ({ page }) => {
    await routeApi(page, {
      sessions: [],
      projects: [{ id: 'p-contrast', path: 'D:/repos/contrast', title: '对比度项目', session_ids: [] }],
    });
    await page.addInitScript((t) => localStorage.setItem('ahi.theme', t), theme);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/');
    await page.locator('.rail-project-head .rail-menu-btn').first().click();
    await page.getByRole('menuitem', { name: '新建任务…' }).click();

    const toggle = page.locator('.project-path-callout-toggle');
    await expect(toggle).toBeVisible();
    const m = await measure(page, '.project-path-callout-toggle');
    expect(Number.parseFloat(m.fontSize), 'project path toggle font-size').toBeGreaterThanOrEqual(12);
    const ratio = contrast(m.color, m.bg);
    expect(ratio, `.project-path-callout-toggle contrast (${theme})`).toBeGreaterThanOrEqual(4.5);
  });

  /* #1312：Composer 会话级目标状态行与「改为新会话」入口是本轮新增的可见 ink。
     选中会话后两者都在真实渲染路径上（openWorkspace 已点会话）——状态行守 12px
     地板（--text-xs）且 --text-secondary ≥4.5；入口按钮是 accent 色、12px 正文，
     亮/暗都不得回退到 <4.5。改字色/字号任一处，本用例立刻变红。 */
  test(`[${theme}] #1312 会话级目标状态行与「改为新会话」对比度 ≥4.5 且 12px 地板`, async ({ page }) => {
    await openWorkspace(page, theme);
    const line = page.locator('.composer-session-target');
    await expect(line).toBeVisible();
    await expect(line).toContainText('正在向会话');

    const m = await measure(page, '.composer-session-target');
    expect(m.fontSize, '.composer-session-target font-size').toBe('12px');
    const ratio = contrast(m.color, m.bg);
    // eslint-disable-next-line no-console
    console.log(`[${theme}] .composer-session-target color=${m.color} bg=${m.bg} ratio=${ratio.toFixed(4)}`);
    expect(ratio, `.composer-session-target contrast (${theme})`).toBeGreaterThanOrEqual(4.5);

    const btn = page.locator('.composer-session-target-new-btn');
    await expect(btn).toBeVisible();
    const bm = await measure(page, '.composer-session-target-new-btn');
    expect(bm.fontSize, '.composer-session-target-new-btn font-size').toBe('12px');
    const bRatio = contrast(bm.color, bm.bg);
    // eslint-disable-next-line no-console
    console.log(`[${theme}] .composer-session-target-new-btn color=${bm.color} bg=${bm.bg} ratio=${bRatio.toFixed(4)}`);
    expect(bRatio, `.composer-session-target-new-btn contrast (${theme})`).toBeGreaterThanOrEqual(4.5);
  });

  test(`[${theme}] composer placeholder 对比度 ≥4.5（R3）`, async ({ page }) => {
    await openWorkspace(page, theme);
    const ratio = await page.evaluate(() => {
      const el = document.querySelector('#composer-input');
      if (!el) throw new Error('no composer');
      const cs = getComputedStyle(el, '::placeholder');
      const parse = (c: string): number[] => {
        const m = c.match(/rgba?\(([^)]+)\)/);
        if (!m) throw new Error(`unparsed color: ${c}`);
        return m[1].split(',').map((s) => parseFloat(s.trim()));
      };
      const chan = (v: number) => {
        const s = v / 255;
        return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
      };
      const lum = (c: number[]) => 0.2126 * chan(c[0]) + 0.7152 * chan(c[1]) + 0.0722 * chan(c[2]);
      // placeholder 的有效底 = composer dock 的解析底色（两主题 --surface-overlay
      // 均不透明；若未来改成半透明玻璃，这里显式失败而不是拿错误底色量）。
      const dock = el.closest('.composer-dock');
      if (!dock) throw new Error('no composer-dock');
      const bg = parse(getComputedStyle(dock).backgroundColor);
      if ((bg[3] ?? 1) < 1) throw new Error('composer-dock bg translucent — measure invalid');
      const fg = parse(cs.color);
      const a = fg[3] ?? 1;
      const fgMix = fg.slice(0, 3).map((v, k) => v * a + bg[k] * (1 - a));
      const f = lum(fgMix);
      const b = lum(bg.slice(0, 3));
      const [hi, lo] = f >= b ? [f, b] : [b, f];
      return (hi + 0.05) / (lo + 0.05);
    });
    expect(ratio).toBeGreaterThanOrEqual(4.5);
  });

  test(`[${theme}] .tl-type == 解析后的 --color-muted-foreground（R1 等值断言）`, async ({ page }) => {
    await openWorkspace(page, theme);
    const [typeColor, expected] = await page.evaluate(() => {
      const el = document.querySelector('.tl-type');
      if (!el) throw new Error('no .tl-type');
      // 用探针元素解析 token 的实际计算值（getPropertyValue 只回 var 引用）
      const probe = document.createElement('div');
      probe.style.color = 'var(--color-muted-foreground)';
      document.body.appendChild(probe);
      const v = getComputedStyle(probe).color;
      probe.remove();
      return [getComputedStyle(el).color, v];
    });
    expect(typeColor).toBe(expected);
  });

  test(`[${theme}] U-1 新增 ink 元素（chip / deny kbd / locked-hint）对比度 ≥4.5`, async ({ page }) => {
    await openWorkspace(page, theme);
    for (const selector of ['.approval-chip', '.approval-deny .approval-kbd', '.composer-locked-hint']) {
      const m = await measure(page, selector);
      const ratio = contrast(m.color, m.bg);
      expect(ratio, `${selector} contrast`).toBeGreaterThanOrEqual(4.5);
    }
  });

  /* 真机验收补锁（docs/LIVE_BROWSER_TEST_20260916.md F2）：上一批把亮色
     `--accent` `#c96990→#b2406e`、`--success` `#2f9e63→#23774a` 压深，
     因为这两个 token 的 12px 文字在亮色下只有 3.15–3.44 / 3.01:1。
     本用例把它们的使用点纳入既有循环——否则改回旧值不会有任何测试变红。 */
  test(`[${theme}] 语义色 ink（run-badge-completed / run-pulse / act-name）对比度 ≥4.5`, async ({ page }) => {
    await routeApi(page, {
      sessions: [ROW],
      events: [
        ...EVENTS,
        // 带 tool_name 的 tool/call → ToolCard 渲染 .act-name（--accent 的使用点）
        { type: 'tool/call', data: { tool_call_id: 'tc-c1', tool_name: 'bash', args: { command: 'echo hi' } }, seq: 6, session_id: ROW.session_id, run_id: 'r1', step_id: 1, time: T },
      ],
      models: MODELS,
      permissionModes: PERMISSION_MODES,
      agentProfiles: AGENT_PROFILES,
      reasoningEfforts: REASONING_EFFORTS,
    });
    await page.addInitScript((t) => localStorage.setItem('ahi.theme', t), theme);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/');
    await page.locator('.session-item').first().click();
    await page.locator('.approval-card').waitFor();
    for (const selector of ['.run-badge-completed', '.run-pulse', '.act-name']) {
      const m = await measure(page, selector);
      const ratio = contrast(m.color, m.bg);
      expect(ratio, `${selector} contrast`).toBeGreaterThanOrEqual(4.5);
    }
  });

  /* #1030：新增的「待裁决」态必须与其余状态同标准——它恰恰是最需要被看清的一态
     （`#1327` 起它是"有欠账、等自动处理"的如实呈现；看不清等于把状态藏起来）。
     这里**真量**而不是信注释里的算术：
     `.pulse-awaiting-decision` 的文字色是 `color-mix(--warning 45%, --text-primary)`
     压在 12% warning 底上。纯 `--warning` 在这个底上亮色实测 **2.91:1**（不达 AA），
     混入 ink 后才够——本用例把这两个读数都现量出来：谁把 ink-mix 改回纯 --warning，
     「≥4.5」那条立刻变红；谁把注释里的数字写成别的值，「<4.5」那条会替我们说话。 */
  test(`[${theme}] #1030 待裁决徽标对比度 ≥4.5（亮/暗两主题）`, async ({ page }) => {
    await routeApi(page, {
      sessions: [ROW],
      // 坏死 run：run/started 之后没有终态 → 徽标落回运行族，再由待裁决压过。
      events: [
        { type: 'session/started', seq: 1, session_id: ROW.session_id, run_id: 'r1', time: T },
        { type: 'run/started', seq: 2, session_id: ROW.session_id, run_id: 'r1', time: T },
        { type: 'user/message', data: { content: '待裁决对比度' }, seq: 3, session_id: ROW.session_id, run_id: 'r1', step_id: 1, time: T },
        {
          type: 'tool/call',
          data: { tool_call_id: 'tc-aw', tool_name: 'bash', args: { command: 'find /' } },
          seq: 4, session_id: ROW.session_id, run_id: 'r1', step_id: 1, time: T,
        },
      ],
      pendingDecisions: [
        {
          tool_call_id: 'tc-aw', tool_name: 'bash', state: 'UNKNOWN',
          default_action: 'RETRY', risk_level: 'high',
          probe: { verifiable: false, suggested_action: null },
        },
      ],
      models: MODELS,
      permissionModes: PERMISSION_MODES,
      agentProfiles: AGENT_PROFILES,
      reasoningEfforts: REASONING_EFFORTS,
    });
    await page.addInitScript((t) => localStorage.setItem('ahi.theme', t), theme);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/');
    await page.locator('.session-item').first().click();

    const pulse = page.locator('.run-pulse');
    await expect(pulse).toContainText('待裁决');
    await expect(pulse).toHaveClass(/pulse-awaiting-decision/);

    const m = await measure(page, '.run-pulse.pulse-awaiting-decision');
    const ratio = contrast(m.color, m.bg);
    // eslint-disable-next-line no-console
    console.log(`[${theme}] pulse-awaiting-decision color=${m.color} bg=${m.bg} ratio=${ratio.toFixed(4)}`);
    expect(ratio, `.pulse-awaiting-decision contrast (${theme})`).toBeGreaterThanOrEqual(4.5);

    // 注释里引用的"纯 --warning 不达标"那个读数也在这里**实测**，不靠算术：克隆同一个
    // 真实节点、只把文字色改回纯 --warning，仍用**同一个 measure 解析器**量它在同一个
    // 底上的比值（不能拿 getComputedStyle 的原始串直接喂 contrast——color-mix 的计算值
    // 可能是 color(srgb …) 形式，只有 measure 内部的解析器认它，直接喂会得到 NaN）。
    // 这样注释里的数字与代码、测试三者同源（审查 P2-2：此前三处写了 2.96 / 3.26 / ~2.8）。
    // 只在亮色主题断言——暗色下 --warning 本身就够亮，那正是"为什么暗色看不出问题、
    // 而 ink-mix 是亮色必需"的对照臂。
    if (theme === 'light') {
      await page.evaluate(() => {
        const src = document.querySelector('.run-pulse.pulse-awaiting-decision') as HTMLElement;
        const probe = src.cloneNode(true) as HTMLElement;
        probe.id = 'tmp-plain-warning';
        probe.style.color = 'var(--warning)';
        src.parentElement!.appendChild(probe);
      });
      const pm = await measure(page, '#tmp-plain-warning');
      const plainRatio = contrast(pm.color, pm.bg);
      // eslint-disable-next-line no-console
      console.log(`[light] plain --warning on same tint ratio=${plainRatio.toFixed(4)}`);
      expect(
        plainRatio,
        '纯 --warning 在亮色下必须不达 AA（这正是 ink-mix 的理由）',
      ).toBeLessThan(4.5);
    }

    // 同色域的另两处：#1030 新增的横幅人话层用的是**同一条**
    // color-mix 公式，但它的底不是胶囊体而是页面底——底色不同则比值不同，必须
    // 各量各的（不能靠"公式一样"外推）。12px 正文，同样受 AA 约束。
    // `#1327`：裁决面板已删——横幅不再与面板互斥，清单非空即直接呈现被动通知；
    // 「去裁决」按钮已删，只量横幅人话层。
    await page.locator('.adjudication-banner').waitFor();
    for (const sel of ['.adjudication-headline']) {
      const mm = await measure(page, sel);
      expect(contrast(mm.color, mm.bg), `${sel} contrast (${theme})`).toBeGreaterThanOrEqual(4.5);
    }

    // 审查 P2-4：Inspector 头标那一处必须量**真实节点**，不能拿 createElement 造的
    // 影子元素顶替——影子只证明 CSS 规则存在，证明不了 `StepDetail` 真的把这个 class
    // 挂上去了（`run-badge-${pulse.state}` 的拼装就是漏点）。Inspector 在 ≥1200px
    // 默认展开（本用例 1440），故直接量 `.detail-header` 里那个真徽标。
    const realBadge = page.locator('.detail-header .run-badge-awaiting-decision');
    await expect(realBadge).toBeVisible();
    const bm = await measure(page, '.detail-header .run-badge-awaiting-decision');
    expect(contrast(bm.color, bm.bg), `.run-badge-awaiting-decision contrast (${theme})`)
      .toBeGreaterThanOrEqual(4.5);
  });

  /* 审查 P2-3：`.recover-conflict` 的文字色在同一次改动里从纯 `--warning` 换成了
     ink-mix，却没进任何对比度守卫——它恰恰是新代码里唯一没被量过的 ink-mix 使用点。
     这里把它纳入（同 `.adjudication-tech-body` 一起），否则"换回纯 --warning"不会有
     任何测试变红。 */
  test(`[${theme}] #1030 恢复冲突条与技术详情层对比度 ≥4.5`, async ({ page }) => {
    // 需要 `isRecoverableRun` 为真才亮出「恢复会话」按钮（同 d-recover.spec.ts 的
    // 崩溃夹具：run 无终态 + 一条未配对 tool/call），才能撞出带 detail 的 409。
    await routeApi(page, {
      sessions: [ROW],
      events: [
        { type: 'session/started', seq: 1, session_id: ROW.session_id, run_id: 'r1', time: T },
        { type: 'user/message', data: { content: '崩溃前的问题' }, seq: 2, session_id: ROW.session_id, run_id: 'r1', time: T },
        { type: 'run/started', data: { turn_index: 1 }, seq: 3, session_id: ROW.session_id, run_id: 'r1', time: T },
        { type: 'model/completed', data: { content: '开始执行。' }, seq: 4, session_id: ROW.session_id, run_id: 'r1', step_id: 1, time: T },
        {
          type: 'tool/call',
          data: { tool_call_id: 'call-1', tool_name: 'bash', args: { command: 'ls' } },
          seq: 5, session_id: ROW.session_id, run_id: 'r1', step_id: 1, time: T,
        },
      ],
      onRecoverPost: async (route) => {
        await route.fulfill({
          status: 409,
          body: JSON.stringify({ detail: '存在需要人工裁决的 UNKNOWN Operation（bash(tool_call_id=tc-x)）：先 POST /api/sessions/{id}/recover 携带 decisions=[{tool_call_id, verdict}] 结清（verdict ∈ CONFIRM_SUCCESS / …）' }),
          contentType: 'application/json',
        });
      },
      models: MODELS,
      permissionModes: PERMISSION_MODES,
      agentProfiles: AGENT_PROFILES,
      reasoningEfforts: REASONING_EFFORTS,
    });
    await page.addInitScript((t) => localStorage.setItem('ahi.theme', t), theme);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/');
    await page.locator('.session-item').first().click();
    await page.locator('.recover-btn').click();
    await expect(page.locator('.recover-conflict')).toBeVisible();

    // 展开折叠层：技术详情是 12px 等宽正文，同样受 AA 约束（它承载协议原文）。
    await page.locator('.recover-conflict details.adjudication-tech > summary').click();
    await expect(page.locator('.recover-conflict .adjudication-tech-body')).toBeVisible();

    for (const sel of ['.recover-conflict', '.recover-conflict .adjudication-tech-body']) {
      const mm = await measure(page, sel);
      expect(contrast(mm.color, mm.bg), `${sel} contrast (${theme})`).toBeGreaterThanOrEqual(4.5);
    }
  });

  /* #1136 AC-3①：`.app-error`（分叉失败横幅）字色 = ink-mix(--danger 45%, --text-primary)，
     压在 12% 染色底上。旧值纯 --danger 亮色实测 3.53:1 < 4.5 ⇒ 本用例在旧 token 下必红；
     把字色改回纯 --danger（#cf4a46）也会立刻变红。 */
  test(`[${theme}] #1136 .app-error（分叉失败横幅）对比度 ≥4.5`, async ({ page }) => {
    await routeApi(page, {
      sessions: [FORK_ROW],
      events: forkTwoTurnEvents(),
      models: MODELS,
      permissionModes: PERMISSION_MODES,
      agentProfiles: AGENT_PROFILES,
      reasoningEfforts: REASONING_EFFORTS,
      onForkPost: async (route) => {
        await route.fulfill({
          status: 422,
          body: JSON.stringify({ detail: 'from_seq 不是合法分叉锚点；可用边界: [2, 30]' }),
          contentType: 'application/json',
        });
      },
    });
    await page.addInitScript((t) => localStorage.setItem('ahi.theme', t), theme);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/');
    await page.locator('.session-item').first().click();
    await expect(page.locator('.turn')).toHaveCount(2);
    await page.locator('.turn').nth(1).locator('.fork-btn').click();

    const banner = page.locator('.app-error[role="alert"]');
    await expect(banner).toBeVisible();
    await expect(banner).toContainText('分叉失败');
    const m = await measure(page, '.app-error[role="alert"]');
    const ratio = contrast(m.color, m.bg);
    // eslint-disable-next-line no-console
    console.log(`[${theme}] .app-error color=${m.color} bg=${m.bg} ratio=${ratio.toFixed(4)}`);
    expect(ratio, `.app-error contrast (${theme})`).toBeGreaterThanOrEqual(4.5);
  });

  /* #1136 AC-3②：`.app-notice`（worktree 隔离横幅）字色 = ink-mix(--info 45%, --text-primary)，
     压在 12% 染色底上。旧值纯 --info 亮色实测 3.71:1 < 4.5 ⇒ 本用例在旧 token 下必红；
     把字色改回纯 --info（#4a6fd6）也会立刻变红。 */
  test(`[${theme}] #1136 .app-notice（worktree 隔离横幅）对比度 ≥4.5`, async ({ page }) => {
    await routeApi(page, {
      sessions: [sessionRow('s3', { id: 'p2', title: '项目 beta' })],
      projects: [
        { id: 'p1', path: WT_ALPHA, title: '项目 alpha', session_ids: [] },
        { id: 'p2', path: WT_BETA, title: '项目 beta', session_ids: ['s3'] },
      ],
      permissionModes: PERMISSION_MODES,
      models: MODELS,
      agentProfiles: AGENT_PROFILES,
      reasoningEfforts: REASONING_EFFORTS,
      onSessionPost: async (route) => {
        await route.fulfill({
          status: 200,
          headers: { 'Content-Type': 'text/event-stream', 'X-Worktree-Path': WT_PATH },
          body: worktreeStartFrames(WT_TASK).map(sseFrame).join(''),
        });
      },
    });
    await page.addInitScript((t) => localStorage.setItem('ahi.theme', t), theme);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/');

    // 项目 alpha 的 kebab →「新建任务…」→ 弹窗填任务 → 创建并启动。
    const alpha = page.locator('.rail-project').filter({ hasText: '项目 alpha' });
    await alpha.locator('.rail-project-head .rail-menu-btn').click();
    await page.getByRole('menuitem', { name: '新建任务…' }).click();
    const box = page.locator('.project-dialog[aria-label="新建任务"]');
    await expect(box).toBeVisible();
    await box.getByLabel('任务描述').fill(WT_TASK);
    await box.getByRole('button', { name: '创建并启动' }).click();

    const notice = page.locator('.app-notice');
    await expect(notice).toBeVisible();
    await expect(notice).toContainText('已自动创建隔离 worktree');
    const m = await measure(page, '.app-notice');
    const ratio = contrast(m.color, m.bg);
    // eslint-disable-next-line no-console
    console.log(`[${theme}] .app-notice color=${m.color} bg=${m.bg} ratio=${ratio.toFixed(4)}`);
    expect(ratio, `.app-notice contrast (${theme})`).toBeGreaterThanOrEqual(4.5);
  });

  /* #1136 AC-3③：`.recover-btn`（「恢复会话」按钮）字色 = ink-mix(--warning 45%, --text-primary)，
     压在 14% 染色底上，hover 时升到 22%（更浓的底 = 更低的对比度）。旧值纯 --warning 亮色实测
     2.97:1、hover 2.71:1，均 < 4.5 ⇒ 本用例在旧 token 下必红；hover 态是票面点名的第二个读数，
     单独量。 */
  test(`[${theme}] #1136 .recover-btn（含 hover 态）对比度 ≥4.5`, async ({ page }) => {
    await routeApi(page, {
      sessions: [CRASH_ROW],
      events: crashedEvents(),
      models: MODELS,
      permissionModes: PERMISSION_MODES,
      agentProfiles: AGENT_PROFILES,
      reasoningEfforts: REASONING_EFFORTS,
    });
    await page.addInitScript((t) => localStorage.setItem('ahi.theme', t), theme);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/');
    await page.locator('.session-item').first().click();

    const btn = page.locator('.recover-btn');
    await expect(btn).toBeVisible();

    const m = await measure(page, '.recover-btn');
    const ratio = contrast(m.color, m.bg);
    // eslint-disable-next-line no-console
    console.log(`[${theme}] .recover-btn color=${m.color} bg=${m.bg} ratio=${ratio.toFixed(4)}`);
    expect(ratio, `.recover-btn contrast (${theme})`).toBeGreaterThanOrEqual(4.5);

    // hover（底色 14% → 22%）：measure 读的是**当前** computed backgroundColor，
    // 移到按钮上再量一次即覆盖 hover 态。
    await btn.hover();
    const hm = await measure(page, '.recover-btn');
    const hRatio = contrast(hm.color, hm.bg);
    // eslint-disable-next-line no-console
    console.log(`[${theme}] .recover-btn:hover color=${hm.color} bg=${hm.bg} ratio=${hRatio.toFixed(4)}`);
    expect(hRatio, `.recover-btn:hover contrast (${theme})`).toBeGreaterThanOrEqual(4.5);
  });

  /* #1136 P3-1①：`.task-review-*` 族把语义色当正文色用，亮色下压在各自染色底上
     <4.5（tag.warn 14% 底 / disambiguation 14% 底 / v-failed·v-blocked 无染色底 /
     missing-evidence 14% 底）。这些用例在旧 token（纯 --warning/--danger/--info）下
     必红——改回旧值也会立刻变红。 */
  test(`[${theme}] #1136 task-review 语义色正文（tag/disambiguation/verification/missing）对比度 ≥4.5`, async ({ page }) => {
    const state: TaskReviewFixture = {
      task: {
        defined: true, task_text: '对比度审阅', read_write_intent: null, cwd: '/repo',
        criteria: [
          criterion('c1', '未确认项', 'agent'), // origin=agent → confirmed=false → .task-review-tag.warn
          criterion('c2', '失败项'), // v-failed + 无证据 → missing-evidence
          criterion('c3', '受阻项'), // v-blocked
        ],
        verification: { c1: verification('passed'), c2: verification('failed'), c3: verification('blocked') },
        acceptance: null, version: 1, open_run_ids: [], product_state: 'deliverable',
      },
      // c1 有 stale 证据 → 触发 disambiguation（deliverable + stale）；c2 无证据；c3 有证据。
      evidence: {
        c1: [evidenceRecord({ criterion_id: 'c1', freshness: { status: 'stale', reasons: ['覆盖文件已变动：src/a.py'] } })],
        c3: [evidenceRecord({ criterion_id: 'c3', result: 'blocked' })],
      },
    };
    await openTaskReview(page, theme, state);
    const panel = page.getByRole('dialog', { name: '详细审阅' });
    // 先确认各选择器都在场（缺席会让 measure throw 而不是静默假绿）
    await expect(panel.locator('.task-review-tag.warn').first()).toBeVisible();
    await expect(panel.locator('.task-review-disambiguation')).toBeVisible();
    await expect(panel.locator('.task-review-verification.v-failed')).toBeVisible();
    await expect(panel.locator('.task-review-verification.v-blocked')).toBeVisible();
    await expect(panel.locator('.task-review-missing-evidence').first()).toBeVisible();

    for (const sel of [
      '.task-review-tag.warn',
      '.task-review-disambiguation',
      '.task-review-verification.v-failed',
      '.task-review-verification.v-blocked',
      '.task-review-missing-evidence',
    ]) {
      const m = await measure(page, sel);
      const ratio = contrast(m.color, m.bg);
      // eslint-disable-next-line no-console
      console.log(`[${theme}] ${sel} color=${m.color} bg=${m.bg} ratio=${ratio.toFixed(4)}`);
      expect(ratio, `${sel} contrast (${theme})`).toBeGreaterThanOrEqual(4.5);
    }
  });

  /* #1136 P3-1②：交付态 chip 三态（executing=--info / pending_verification=--warning /
     accepted=--accent-text，第三态为 #1364 补）。chip 底是 `--surface-2`（不染色），
     但字色本身在亮色下仍 <4.5。product_state 单值，故关面板（hook 置 null 清态）改状态后
     再开重拉，一个用例量三态。 */
  test(`[${theme}] #1136 task-review 交付态 chip（executing / pending_verification / accepted）对比度 ≥4.5`, async ({ page }) => {
    const state: TaskReviewFixture = {
      task: {
        defined: true, task_text: 'chip 对比度', read_write_intent: null, cwd: '/repo',
        criteria: [criterion('c1', 'x')], verification: { c1: verification('passed') },
        acceptance: null, version: 1, open_run_ids: ['r1'], product_state: 'executing',
      },
      evidence: { c1: [evidenceRecord({ criterion_id: 'c1' })] },
    };
    await openTaskReview(page, theme, state);

    const exec = page.locator('.task-review-chip.state-executing');
    await expect(exec).toBeVisible();
    const em = await measure(page, '.task-review-chip.state-executing');
    const er = contrast(em.color, em.bg);
    // eslint-disable-next-line no-console
    console.log(`[${theme}] .task-review-chip.state-executing color=${em.color} bg=${em.bg} ratio=${er.toFixed(4)}`);
    expect(er, `.task-review-chip.state-executing contrast (${theme})`).toBeGreaterThanOrEqual(4.5);

    // 关面板 → 改投影态 → 重开（useTaskReview 以 sessionId 为键，重开即重拉服务端投影）。
    await page.locator('.task-review-panel .ctx-usage-close').click();
    await expect(page.getByRole('dialog', { name: '详细审阅' })).toBeHidden();
    state.task.product_state = 'pending_verification';
    await page.locator('.task-review-summary').getByRole('button', { name: '详细审阅' }).click();
    const pending = page.locator('.task-review-chip.state-pending_verification');
    await expect(pending).toBeVisible();
    const pm = await measure(page, '.task-review-chip.state-pending_verification');
    const pr = contrast(pm.color, pm.bg);
    // eslint-disable-next-line no-console
    console.log(`[${theme}] .task-review-chip.state-pending_verification color=${pm.color} bg=${pm.bg} ratio=${pr.toFixed(4)}`);
    expect(pr, `.task-review-chip.state-pending_verification contrast (${theme})`).toBeGreaterThanOrEqual(4.5);

    // #1364：accepted 态是四态里优先级最高者（`--accent` 换色后最容易掉到 <4.5 的一格），
    // 必须同批量——`#533afd` 在暗底只有 2.5:1，字色走 `--accent-text` 提亮档。
    await page.locator('.task-review-panel .ctx-usage-close').click();
    await expect(page.getByRole('dialog', { name: '详细审阅' })).toBeHidden();
    state.task.product_state = 'accepted';
    await page.locator('.task-review-summary').getByRole('button', { name: '详细审阅' }).click();
    const accepted = page.locator('.task-review-chip.state-accepted');
    await expect(accepted).toBeVisible();
    const am = await measure(page, '.task-review-chip.state-accepted');
    const ar = contrast(am.color, am.bg);
    // eslint-disable-next-line no-console
    console.log(`[${theme}] .task-review-chip.state-accepted color=${am.color} bg=${am.bg} ratio=${ar.toFixed(4)}`);
    expect(ar, `.task-review-chip.state-accepted contrast (${theme})`).toBeGreaterThanOrEqual(4.5);
  });

  /* #1136 P3-1③：`.task-review-evidence-unavailable`（证据不可得条，--danger 压 12% 底）。
     GET /evidence 失败即渲染——`证据不可得 ≠ 缺证据`（P1-1），走独立分支。 */
  test(`[${theme}] #1136 task-review 证据不可得条对比度 ≥4.5`, async ({ page }) => {
    const state: TaskReviewFixture = {
      task: {
        defined: true, task_text: 'x', read_write_intent: null, cwd: '/repo',
        criteria: [criterion('c1', 'x')], verification: { c1: verification('passed') },
        acceptance: null, version: 1, open_run_ids: [], product_state: 'deliverable',
      },
      evidence: {},
      evidenceError: { status: 500, detail: '证据服务暂不可用' },
    };
    await openTaskReview(page, theme, state);
    const el = page.locator('.task-review-evidence-unavailable');
    await expect(el).toBeVisible();
    const m = await measure(page, '.task-review-evidence-unavailable');
    const ratio = contrast(m.color, m.bg);
    // eslint-disable-next-line no-console
    console.log(`[${theme}] .task-review-evidence-unavailable color=${m.color} bg=${m.bg} ratio=${ratio.toFixed(4)}`);
    expect(ratio, `.task-review-evidence-unavailable contrast (${theme})`).toBeGreaterThanOrEqual(4.5);
  });

  /* #1136 P3-1 穷尽：task-review 族里余下 7 处「语义色当正文色用」——.task-review-error（--danger）、
     .task-review-result-tag.r-fail（--danger）/.r-blocked（--warning）、.task-review-freshness.f-stale
     （--warning）、.task-review-freshness-reasons（--warning）、.task-review-diff .diff-del（--danger）
     /.diff-hunk（--info）。同族同根因同手法（ink-mix）；本用例量其中 6 处，.task-review-error 由紧随
     其后的「加载失败条」用例单独量（本族净覆盖仍 7/7），改回纯 `var(--X)` 必红。
     .r-pass/.diff-add/.verification.v-passed（--success）与 .run-axis.running（--info）已达标，
     不在本用例内（不为了凑数改动达标的处）。 */
  test(`[${theme}] #1136 task-review 语义色正文（result-tag/freshness/diff）对比度 ≥4.5`, async ({ page }) => {
    const state: TaskReviewFixture = {
      task: {
        defined: true, task_text: '穷尽对比度', read_write_intent: null, cwd: '/repo',
        criteria: [criterion('c1', '失败项'), criterion('c2', '受阻项'), criterion('c3', '通过项')],
        verification: { c1: verification('failed'), c2: verification('blocked'), c3: verification('passed') },
        acceptance: null, version: 1, open_run_ids: [], product_state: 'deliverable',
      },
      evidence: {
        // c1 result=fail + stale(带 reasons) → r-fail / f-stale / freshness-reasons 三处同时在真实路径上
        c1: [evidenceRecord({ evidence_id: 'ev-c1', criterion_id: 'c1', result: 'fail', freshness: { status: 'stale', reasons: ['覆盖文件已变动：src/a.py'] } })],
        c2: [evidenceRecord({ evidence_id: 'ev-c2', criterion_id: 'c2', result: 'blocked', freshness: { status: 'fresh', reasons: [] } })],
        c3: [evidenceRecord({ evidence_id: 'ev-c3', criterion_id: 'c3', result: 'pass' })],
      },
      // diff 正文含 `@@`（diff-hunk）与 `-`（diff-del）行；`+++`/`---` 头不算（见 UnifiedDiffText）。
      gitDiff: { exit_code: 0, stdout: '@@ -1,2 +1,2 @@\n context line\n-removed line\n+added line', stderr: '', artifact_ref: null },
    };
    await openTaskReview(page, theme, state);
    const panel = page.getByRole('dialog', { name: '详细审阅' });
    const SELECTORS = [
      '.task-review-result-tag.r-fail',
      '.task-review-result-tag.r-blocked',
      '.task-review-freshness.f-stale',
      '.task-review-freshness-reasons',
      '.task-review-diff .diff-del',
      '.task-review-diff .diff-hunk',
    ];
    // 每选择器先 toBeVisible 预检（缺席必须 throw，不许静默假绿）
    for (const sel of SELECTORS) {
      await expect(panel.locator(sel).first()).toBeVisible();
    }
    for (const sel of SELECTORS) {
      const m = await measure(page, sel);
      const ratio = contrast(m.color, m.bg);
      // eslint-disable-next-line no-console
      console.log(`[${theme}] ${sel} color=${m.color} bg=${m.bg} ratio=${ratio.toFixed(4)}`);
      expect(ratio, `${sel} contrast (${theme})`).toBeGreaterThanOrEqual(4.5);
    }
  });

  /* #1136 P3-1 穷尽：`.task-review-error`（--danger 压 12% 底）。走「任务拉取失败」真实渲染路径
     （GET /task 非 404 报错 → review.error → 面板渲染「加载失败：…」；与 review.error 分支同源），
     同 ink-mix 手法，改回纯 --danger 必红。 */
  test(`[${theme}] #1136 task-review 加载失败条（.task-review-error）对比度 ≥4.5`, async ({ page }) => {
    const state: TaskReviewFixture = {
      task: {
        defined: true, task_text: 'x', read_write_intent: null, cwd: '/repo',
        criteria: [], verification: {}, acceptance: null, version: 1, open_run_ids: [], product_state: 'deliverable',
      },
      evidence: {},
      taskError: { status: 500, detail: '任务状态服务暂不可用' },
    };
    await openTaskReview(page, theme, state);
    const el = page.locator('.task-review-error');
    await expect(el).toBeVisible();
    await expect(el).toContainText('加载失败');
    const m = await measure(page, '.task-review-error');
    const ratio = contrast(m.color, m.bg);
    // eslint-disable-next-line no-console
    console.log(`[${theme}] .task-review-error color=${m.color} bg=${m.bg} ratio=${ratio.toFixed(4)}`);
    expect(ratio, `.task-review-error contrast (${theme})`).toBeGreaterThanOrEqual(4.5);
  });

  /* #1136 P3-2：`.auth-banner`（后端 401 引导横幅）把 --warning 当正文色，压在 10% 底上。
     渲染条件是 `authRequired=true`（App.tsx:1401，后端 401 广播触发）——夹具同
     l-auth-banner.spec.ts（共享 mockAuthRequired）。 */
  test(`[${theme}] #1136 .auth-banner（401 引导横幅）对比度 ≥4.5`, async ({ page }) => {
    await mockAuthRequired(page);
    await page.addInitScript((t) => localStorage.setItem('ahi.theme', t), theme);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/');
    const banner = page.locator('.auth-banner');
    await expect(banner).toBeVisible();
    const m = await measure(page, '.auth-banner');
    const ratio = contrast(m.color, m.bg);
    // eslint-disable-next-line no-console
    console.log(`[${theme}] .auth-banner color=${m.color} bg=${m.bg} ratio=${ratio.toFixed(4)}`);
    expect(ratio, `.auth-banner contrast (${theme})`).toBeGreaterThanOrEqual(4.5);
  });
}

/* ── #1171 / #1172：语义色「当正文色用」的余量收口 ──
 *
 *  三处都是 #1136 ink-mix 方案（`color-mix(in srgb, <sem> 45%, var(--text-primary))`）
 *  与 #1029 先例的同类，票面点名时仍 <4.5：
 *  - `.provider-link-btn.danger`：亮 4.45（纯 --danger 压 --surface-1，无自身底）
 *  - `.provider-test-msg`：亮 3.90（纯 --danger 压 10% danger 染底）
 *  - `.notice-strip-hard .notice-strip-badge`：白字压 --danger **实底**，亮 4.45 / 暗 3.01
 *  前两处改 ink-mix；第三处是白字压实底、方向相反（要压深的是底不是字），
 *  按 Primer `--bgColor-danger-emphasis` 的「白字配深色档」做法取
 *  `color-mix(in srgb, var(--danger) 75%, #000)`。
 *  本夹具走真实渲染路径（打开供应商弹层 / 触发 provider 测试失败 / failure-guard 事件条），
 *  把任一处改回原值，对应用例必然变红。 */

/** 供应商弹层夹具（`.provider-dialog`）。`/api/model-providers` 不在 routeApi 的
 *  覆盖清单里（它落到末尾的 404 fallback），所以这里叠加一层 mock。 */
const PROVIDER_ENTRY = {
  id: 'e2e-provider',
  label: '对比度供应商',
  base_url: 'https://example.invalid/v1',
  models: [{ model_id: 'e2e-model', label: 'e2e 模型' }],
  kind: 'custom' as const,
  has_api_key: true,
  is_available: true,
  unavailable_reason: null,
  last_test: null,
};

/** 打开供应商管理弹层并选中唯一条目（右侧表单在场 → 危险区按钮在场）。 */
async function openProviderDialog(page: Page, theme: 'dark' | 'light'): Promise<void> {
  await routeApi(page, {
    sessions: [], events: [], models: MODELS,
    permissionModes: PERMISSION_MODES, agentProfiles: AGENT_PROFILES, reasoningEfforts: REASONING_EFFORTS,
  });
  await page.route('**/api/model-providers**', (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/model-providers' && route.request().method() === 'GET') {
      return route.fulfill({ status: 200, body: JSON.stringify({ providers: [PROVIDER_ENTRY] }), contentType: 'application/json' });
    }
    if (path.endsWith('/test') && route.request().method() === 'POST') {
      return route.fulfill({
        status: 200,
        body: JSON.stringify({ ok: false, message: '连接失败：401 未授权', reason: 'auth', duration_ms: 12 }),
        contentType: 'application/json',
      });
    }
    return route.fulfill({ status: 404, body: '{"detail":"not mocked"}', contentType: 'application/json' });
  });
  await page.addInitScript((t) => localStorage.setItem('ahi.theme', t), theme);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/');
  const trigger = page.locator('.composer-model[aria-label="模型选择"]').first();
  await expect(trigger).toBeVisible();
  await trigger.click();
  await page.locator('.picker-item', { hasText: '管理模型' }).first().click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.locator('.provider-list button').first().click();
  await expect(page.locator('.provider-danger-zone')).toBeVisible();
}

for (const theme of ['dark', 'light'] as const) {
  test(`[${theme}] #1171 供应商弹层两处语义色正文对比度 ≥4.5`, async ({ page }) => {
    await openProviderDialog(page, theme);

    const link = await measure(page, '.provider-link-btn.danger');
    const linkRatio = contrast(link.color, link.bg);
    // eslint-disable-next-line no-console
    console.log(`[${theme}] .provider-link-btn.danger color=${link.color} bg=${link.bg} ratio=${linkRatio.toFixed(4)}`);
    expect(linkRatio, `.provider-link-btn.danger contrast (${theme})`).toBeGreaterThanOrEqual(4.5);

    // 触发「测试连接」失败 → .provider-test-msg 落进 .provider-test-result.fail 的染底里。
    await page.getByRole('button', { name: '测试连接' }).click();
    const msg = page.locator('.provider-test-msg');
    await expect(msg).toBeVisible();
    const m = await measure(page, '.provider-test-msg');
    const ratio = contrast(m.color, m.bg);
    // eslint-disable-next-line no-console
    console.log(`[${theme}] .provider-test-msg color=${m.color} bg=${m.bg} ratio=${ratio.toFixed(4)}`);
    expect(ratio, `.provider-test-msg contrast (${theme})`).toBeGreaterThanOrEqual(4.5);
  });

  /* #1172：白字压 --danger 实心底。与上面两处方向相反——要压深的是**底**。
     夹具走 failure-guard 的 hard 事件条（Conversation.tsx:735），真渲染路径。 */
  test(`[${theme}] #1172 .notice-strip-hard 徽标（白字压实底）对比度 ≥4.5`, async ({ page }) => {
    const sid = 's-contrast-guard';
    const events: FrameSpec[] = [
      { type: 'session/started', seq: 1, session_id: sid, run_id: 'rg', time: T },
      { type: 'run/started', seq: 2, session_id: sid, run_id: 'rg', time: T },
      { type: 'user/message', data: { content: '熔断对比度' }, seq: 3, session_id: sid, run_id: 'rg', step_id: 1, time: T },
      { type: 'tool/failure-guard', data: { level: 'hard', tool_name: 'bash', consecutive_failures: 5 }, seq: 4, session_id: sid, run_id: 'rg', step_id: 1, time: T },
      { type: 'run/completed', data: {}, seq: 5, session_id: sid, run_id: 'rg', time: T },
    ];
    await routeApi(page, {
      sessions: [{ session_id: sid, event_count: 5, first_event_time: T, last_event_time: T,
        first_user_message: '熔断对比度', trace_id: null, trace_url: null }],
      events, models: MODELS, permissionModes: PERMISSION_MODES,
      agentProfiles: AGENT_PROFILES, reasoningEfforts: REASONING_EFFORTS,
    });
    await page.addInitScript((t) => localStorage.setItem('ahi.theme', t), theme);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/');
    await page.locator('.session-item').first().click();
    const badge = page.locator('.notice-strip-hard .notice-strip-badge');
    await expect(badge).toBeVisible();
    const m = await measure(page, '.notice-strip-hard .notice-strip-badge');
    const ratio = contrast(m.color, m.bg);
    // eslint-disable-next-line no-console
    console.log(`[${theme}] .notice-strip-hard .notice-strip-badge color=${m.color} bg=${m.bg} ratio=${ratio.toFixed(4)}`);
    expect(ratio, `.notice-strip-hard .notice-strip-badge contrast (${theme})`).toBeGreaterThanOrEqual(4.5);
  });

/* ── #1208：recovery.css 语义色正文收口（#1175 独立审查 Axis B F1 残余）──
 *
 *  原先两处：`.recovery-decision-error`（裁决面板提交校验失败条）与
 *  `.recovery-list-error`（恢复列表拉取失败条）。`#1327` 起裁决面板已删，
 *  前者随组件一并删除；后者保留，走**真实渲染路径**（恢复列表拉取 500），
 *  不是造影子元素，故把字色改回纯 --danger 时用例必然变红。
 *  （`.recovery-decision-error` 的旧用例见 git 历史，本文件不再保留。）
 */

/** 恢复列表错误态夹具：`GET /api/recovery/interrupted` 返回 500（`listInterruptedRecoveries`
 *  非 2xx 抛错）→ `state.kind='error'` → `.recovery-list-error`（role=alert）落屏。
 *  入口是顶栏「恢复列表」按钮（TopBar aria-label="恢复列表"）。 */
test(`[${theme}] #1208 .recovery-list-error（恢复列表拉取失败条）对比度 ≥4.5`, async ({ page }) => {
  await routeApi(page, {
    sessions: [], events: [], models: MODELS,
    permissionModes: PERMISSION_MODES, agentProfiles: AGENT_PROFILES, reasoningEfforts: REASONING_EFFORTS,
  });
  // 后注册的 route 优先匹配（Playwright 逆序）——覆盖 routeApi 的默认 200 空快照。
  await page.route('**/api/recovery/interrupted', (route) =>
    route.fulfill({ status: 500, body: JSON.stringify({ detail: 'snapshot 不可读' }), contentType: 'application/json' }));
  await page.addInitScript((t) => localStorage.setItem('ahi.theme', t), theme);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/');
  await page.getByRole('button', { name: '恢复列表' }).click();

  const err = page.locator('.recovery-list-error');
  await expect(err).toBeVisible();
  const m = await measure(page, '.recovery-list-error');
  const ratio = contrast(m.color, m.bg);
  // eslint-disable-next-line no-console
  console.log(`[${theme}] .recovery-list-error color=${m.color} bg=${m.bg} ratio=${ratio.toFixed(4)}`);
  expect(ratio, `.recovery-list-error contrast (${theme})`).toBeGreaterThanOrEqual(4.5);
});
}

/* ── #1175：语义色「当正文色用」的余量收口（40+ 处）──
 *
 *  #1171/#1172 收口后，同类规则（`color: var(--danger|warning|info)` 直接当**正文色**
 *  用）在亮色主题下仍有 40+ 处 <4.5（#1136 ink-mix 方案的下一批残余；清单见 #1175）。
 *  修法同 #1171：`color-mix(in srgb, <sem> 45%, var(--text-primary))`——亮色
 *  `--text-primary` 是深墨、混入即压深，暗色是近白、混入即提亮（只升不降）；装饰
 *  （描边/底）与图标保留原 token（#1164 P2-3）。
 *
 *  本块走**真实渲染路径**（run 三态 / 中断条 / 熔断条 / 工具失败 / 改动面 / 模型回退 /
 *  任务审阅在途 run），把任一处字色改回纯 token 对应用例必然变红——不以静态复算充数。
 *  逐处静态复算（含未进本文件的余量点）见 #1175 交付报告与沙箱读数。 */

/** 打开一个会话并等到时间线出现（同 #1136 各夹具的通用姿势）。 */
async function openContrastSession(
  page: Page,
  theme: 'dark' | 'light',
  sid: string,
  frames: FrameSpec[],
  firstMessage: string,
): Promise<void> {
  await routeApi(page, {
    sessions: [{
      session_id: sid, event_count: frames.length, first_event_time: T, last_event_time: T,
      first_user_message: firstMessage, trace_id: null, trace_url: null,
    }],
    events: frames,
    models: MODELS, permissionModes: PERMISSION_MODES,
    agentProfiles: AGENT_PROFILES, reasoningEfforts: REASONING_EFFORTS,
  });
  await page.addInitScript((t) => localStorage.setItem('ahi.theme', t), theme);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/');
  await page.locator('.session-item').first().click();
  await page.locator('.turn').first().waitFor();
}

/** 逐选择器：真机量对比度 + ≥4.5 断言（先 toBeVisible 预检，缺席必须 throw 不许静默假绿）。 */
async function expectEachContrast(
  page: Page,
  theme: 'dark' | 'light',
  selectors: string[],
): Promise<void> {
  for (const sel of selectors) {
    await expect(page.locator(sel).first(), `${sel} present (${theme})`).toBeVisible();
    const m = await measure(page, sel);
    const ratio = contrast(m.color, m.bg);
    // eslint-disable-next-line no-console
    console.log(`[${theme}] #1175 ${sel} color=${m.color} bg=${m.bg} ratio=${ratio.toFixed(4)}`);
    expect(ratio, `${sel} contrast (${theme})`).toBeGreaterThanOrEqual(4.5);
  }
}

for (const theme of ['dark', 'light'] as const) {
  /* pulse-tool / run-badge-tool：run 运行中且末个 tool 未收口。 */
  test(`[${theme}] #1175 run 运行态（pulse-tool / run-badge-tool）对比度 ≥4.5`, async ({ page }) => {
    const sid = 's-contrast-tool';
    const frames: FrameSpec[] = [
      { type: 'session/started', seq: 1, session_id: sid, run_id: 'rt', time: T },
      { type: 'run/started', seq: 2, session_id: sid, run_id: 'rt', step_id: 1, time: T },
      { type: 'user/message', data: { content: '跑一个工具' }, seq: 3, session_id: sid, run_id: 'rt', step_id: 1, time: T },
      /* model/started 是关键：deriveRunPulse 在 run_status==='running' 时靠
         active_step_id 定位当轮，才把「末个 tool 未收口」判成 pulse-tool（缺它落到 thinking）。 */
      { type: 'model/started', seq: 4, session_id: sid, run_id: 'rt', step_id: 1, time: T },
      { type: 'tool/call', data: { tool_call_id: 'tc-t', tool_name: 'bash', args: { command: 'sleep 1' } }, seq: 5, session_id: sid, run_id: 'rt', step_id: 1, time: T },
    ];
    await openContrastSession(page, theme, sid, frames, '跑一个工具');
    await expectEachContrast(page, theme, ['.run-pulse.pulse-tool', '.detail-header .run-badge-tool']);
  });

  /* pulse-failed / run-badge-failed：run/failed 终态。 */
  test(`[${theme}] #1175 run 失败态（pulse-failed / run-badge-failed）对比度 ≥4.5`, async ({ page }) => {
    const sid = 's-contrast-failed';
    const frames: FrameSpec[] = [
      { type: 'session/started', seq: 1, session_id: sid, run_id: 'rf', time: T },
      { type: 'run/started', seq: 2, session_id: sid, run_id: 'rf', step_id: 1, time: T },
      { type: 'user/message', data: { content: '跑到失败' }, seq: 3, session_id: sid, run_id: 'rf', step_id: 1, time: T },
      { type: 'run/failed', data: { reason: 'model_error' }, seq: 4, session_id: sid, run_id: 'rf', step_id: 1, time: T },
    ];
    await openContrastSession(page, theme, sid, frames, '跑到失败');
    await expectEachContrast(page, theme, ['.run-pulse.pulse-failed', '.detail-header .run-badge-failed']);
  });

  /* pulse-paused + 暂停面板（.pause-panel-head / .pause-resume-btn）：预算到顶暂停（非终态）。 */
  test(`[${theme}] #1175 预算暂停态（pulse-paused / pause-panel / pause-resume-btn）对比度 ≥4.5`, async ({ page }) => {
    const sid = 's-contrast-paused';
    const frames: FrameSpec[] = [
      { type: 'session/started', seq: 1, session_id: sid, run_id: 'rp', time: T },
      { type: 'run/started', seq: 2, session_id: sid, run_id: 'rp', step_id: 1, time: T },
      { type: 'user/message', data: { content: '跑到预算顶' }, seq: 3, session_id: sid, run_id: 'rp', step_id: 1, time: T },
      {
        type: 'run/paused', seq: 4, session_id: sid, run_id: 'rp', step_id: 1, time: T,
        data: {
          reason: 'budget_exhausted', trigger_dimension: 'run.max_total_tokens', budget_version: 1,
          consumed: { agent_turns: 2, total_tokens: 1234 },
          limits: { run: { max_agent_turns_total: 3, max_total_tokens: 1234 } },
          resume_requirements: ['抬高 run.max_total_tokens 的绝对 ceiling 后同 run 恢复'],
        },
      },
    ];
    await openContrastSession(page, theme, sid, frames, '跑到预算顶');
    await expect(page.locator('.pause-panel')).toBeVisible();
    await expectEachContrast(page, theme, [
      '.run-pulse.pulse-paused', '.pause-panel-head', '.pause-resume-btn',
    ]);
  });

  /* 中断条（.interrupt-banner）：进程重启打断的 run（run_interrupted && !streaming）。 */
  test(`[${theme}] #1175 上次中断条（interrupt-banner）对比度 ≥4.5`, async ({ page }) => {
    const sid = 's-contrast-interrupt';
    const frames: FrameSpec[] = [
      { type: 'session/started', seq: 1, session_id: sid, run_id: 'ri', time: T },
      { type: 'run/started', seq: 2, session_id: sid, run_id: 'ri', step_id: 1, time: T },
      { type: 'user/message', data: { content: '被打断了' }, seq: 3, session_id: sid, run_id: 'ri', step_id: 1, time: T },
      { type: 'run/interrupted', data: { reason: 'process_restart' }, seq: 4, session_id: sid, run_id: 'ri', step_id: 1, time: T },
    ];
    await openContrastSession(page, theme, sid, frames, '被打断了');
    await expectEachContrast(page, theme, ['.interrupt-banner']);
  });

  /* 熔断条 soft 徽标（.notice-strip-soft .notice-strip-badge）：soft failure-guard 事件。 */
  test(`[${theme}] #1175 熔断条 soft 徽标（notice-strip-soft badge）对比度 ≥4.5`, async ({ page }) => {
    const sid = 's-contrast-soft';
    const frames: FrameSpec[] = [
      { type: 'session/started', seq: 1, session_id: sid, run_id: 'rs', time: T },
      { type: 'run/started', seq: 2, session_id: sid, run_id: 'rs', step_id: 1, time: T },
      { type: 'user/message', data: { content: '熔断 soft' }, seq: 3, session_id: sid, run_id: 'rs', step_id: 1, time: T },
      { type: 'tool/failure-guard', data: { level: 'soft', tool_name: 'bash', consecutive_failures: 3 }, seq: 4, session_id: sid, run_id: 'rs', step_id: 1, time: T },
      { type: 'run/completed', data: {}, seq: 5, session_id: sid, run_id: 'rs', time: T },
    ];
    await openContrastSession(page, theme, sid, frames, '熔断 soft');
    await expectEachContrast(page, theme, ['.notice-strip-soft .notice-strip-badge']);
  });

  /* 工具失败摘要（.act-error-code）：tool/result ok=false + error_code。 */
  test(`[${theme}] #1175 工具失败摘要（act-error-code）对比度 ≥4.5`, async ({ page }) => {
    const sid = 's-contrast-err';
    const frames: FrameSpec[] = [
      { type: 'session/started', seq: 1, session_id: sid, run_id: 're', time: T },
      { type: 'run/started', seq: 2, session_id: sid, run_id: 're', step_id: 1, time: T },
      { type: 'user/message', data: { content: '跑失败的工具' }, seq: 3, session_id: sid, run_id: 're', step_id: 1, time: T },
      { type: 'tool/call', data: { tool_call_id: 'tc-e', tool_name: 'bash', args: { command: 'false' } }, seq: 4, session_id: sid, run_id: 're', step_id: 1, time: T },
      /* 错误信封要嵌在 data 里：投影把 tool.result 落成 parsed.data（projection.ts
         `tool.result = parsedData ?? message`），而 ToolCard 的 parseErrorShape 只认
         {ok:false,error_code} 对象——扁平放顶层会被落成 message 字符串、摘要行不渲染。 */
      { type: 'tool/result', data: { tool_call_id: 'tc-e', content: JSON.stringify({ ok: false, message: '命令超时', data: { ok: false, error_code: 'TIMEOUT', message: '命令超时' } }) }, seq: 5, session_id: sid, run_id: 're', step_id: 1, time: T },
      { type: 'run/completed', data: {}, seq: 6, session_id: sid, run_id: 're', time: T },
    ];
    await openContrastSession(page, theme, sid, frames, '跑失败的工具');
    await expectEachContrast(page, theme, ['.act-error-code']);
  });

  /* 模型回退值（.model-fallback-val）：model/fallback 事件，Inspector 步骤详情呈现。 */
  test(`[${theme}] #1175 模型回退值（model-fallback-val）对比度 ≥4.5`, async ({ page }) => {
    const sid = 's-contrast-fb';
    const frames: FrameSpec[] = [
      { type: 'session/started', seq: 1, session_id: sid, run_id: 'rfb', time: T },
      { type: 'run/started', seq: 2, session_id: sid, run_id: 'rfb', step_id: 1, time: T },
      { type: 'user/message', data: { content: '触发回退' }, seq: 3, session_id: sid, run_id: 'rfb', step_id: 1, time: T },
      { type: 'model/fallback', data: { from_model: 'primary', to_model: 'fallback', reason: 'TimeoutError' }, seq: 4, session_id: sid, run_id: 'rfb', step_id: 1, time: T },
      { type: 'run/completed', data: {}, seq: 5, session_id: sid, run_id: 'rfb', time: T },
    ];
    await openContrastSession(page, theme, sid, frames, '触发回退');
    /* .model-fallback-val 在 Inspector 的 Overview（id=chat）页 MODEL 区块里；默认 tab 是
       timeline，先切页再量（否则元素不在 DOM，属夹具问题而非样式问题）。 */
    await page.getByRole('tablist', { name: 'Inspector 视图' }).getByRole('tab', { name: 'Overview' }).click();
    await expectEachContrast(page, theme, ['.model-fallback-val']);
  });

  /* 改动面删除计数（.changes-removed）：文件净删除 >0。走「文件/改动」面真实渲染路径。 */
  test(`[${theme}] #1175 改动面删除计数（changes-removed）对比度 ≥4.5`, async ({ page }) => {
    const sid = 's-contrast-chg';
    const frames: FrameSpec[] = [
      { type: 'session/started', seq: 1, session_id: sid, run_id: 'rc', step_id: 1, time: T },
      { type: 'run/started', seq: 2, session_id: sid, run_id: 'rc', step_id: 1, time: T },
      { type: 'user/message', data: { content: '删几行' }, seq: 3, session_id: sid, run_id: 'rc', step_id: 1, time: T },
      { type: 'tool/call', data: { tool_call_id: 'tc-c', tool_name: 'write', args: { path: 'src/a.ts' } }, seq: 4, session_id: sid, run_id: 'rc', step_id: 1, time: T },
      {
        type: 'tool/result', seq: 5, session_id: sid, run_id: 'rc', step_id: 1, time: T,
        data: { tool_call_id: 'tc-c', content: JSON.stringify({ ok: true, message: 'ok', data: { path: 'src/a.ts', before: 'l1\nl2\nl3\n', after: 'l1\n', truncated: false } }) },
      },
      { type: 'run/completed', data: {}, seq: 6, session_id: sid, run_id: 'rc', time: T },
    ];
    await routeApi(page, {
      sessions: [{
        session_id: sid, event_count: frames.length, first_event_time: T, last_event_time: T,
        first_user_message: '删几行', trace_id: null, trace_url: null,
      }],
      events: frames,
      models: MODELS, permissionModes: PERMISSION_MODES,
      agentProfiles: AGENT_PROFILES, reasoningEfforts: REASONING_EFFORTS,
    });
    await page.addInitScript((t) => localStorage.setItem('ahi.theme', t), theme);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/');
    await page.locator('.session-item').first().click();
    await page.getByRole('tablist', { name: '工作区面' }).getByRole('tab', { name: '文件/改动' }).click();
    await expect(page.locator('.changes-panel')).toBeVisible();
    await expectEachContrast(page, theme, ['.changes-removed']);
  });

  /* 任务审阅在途 run 轴（.task-review-run-axis.running）：open_run_ids 非空。 */
  test(`[${theme}] #1175 任务审阅在途 run 轴（task-review-run-axis.running）对比度 ≥4.5`, async ({ page }) => {
    const state: TaskReviewFixture = {
      task: {
        defined: true, task_text: '在途对比度', read_write_intent: null, cwd: '/repo',
        criteria: [criterion('c1', 'x')], verification: { c1: verification('passed') },
        acceptance: null, version: 1, open_run_ids: ['r1'], product_state: 'executing',
      },
      evidence: { c1: [evidenceRecord({ criterion_id: 'c1' })] },
    };
    await openTaskReview(page, theme, state);
    await expectEachContrast(page, theme, ['.task-review-run-axis.running']);
  });

  /* #1190 约束管理页：新墨面（值 14px / 类型名 / 12px 元信息 / 状态 pill / 撤销按钮）
   * 的对比度与 12px 地板。元信息刻意用 ink-subtle 而非票面 #62666d（对比度取舍，见
   * constraints.css 头注释）——本用例把那句口头承诺变成机械守卫（PRODUCT.md:59/72）。 */
  test(`[${theme}] #1190 约束管理（值/类型/元信息/状态 pill/撤销）对比度 ≥4.5 且 12px 地板`, async ({ page }) => {
    const sid = 's-contrast-cn';
    const facts = [{
      fact_id: 'f-cn', type: 'constraint', value: '不要改动生产环境配置', status: 'active',
      source_event_id: 'ev-3', source_seq: 3, registered_at: T, revoked_at: null, revoked_by: null,
    }];
    const frames: FrameSpec[] = [
      { type: 'session/started', seq: 1, session_id: sid, run_id: 'rcn', time: T },
      { type: 'run/started', seq: 2, session_id: sid, run_id: 'rcn', step_id: 1, time: T },
      { type: 'user/message', data: { content: '约束对比度' }, seq: 3, session_id: sid, run_id: 'rcn', step_id: 1, time: T },
      { type: 'model/completed', data: { text: '好' }, seq: 4, session_id: sid, run_id: 'rcn', step_id: 1, time: T },
      { type: 'run/completed', data: {}, seq: 5, session_id: sid, run_id: 'rcn', time: T },
    ];
    await routeApi(page, {
      sessions: [{
        session_id: sid, event_count: frames.length, first_event_time: T, last_event_time: T,
        first_user_message: '约束对比度', trace_id: null, trace_url: null,
      }],
      events: frames,
      models: MODELS, permissionModes: PERMISSION_MODES,
      agentProfiles: AGENT_PROFILES, reasoningEfforts: REASONING_EFFORTS,
      protectedFacts: facts,
    });
    await page.addInitScript((t) => localStorage.setItem('ahi.theme', t), theme);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/');
    await page.locator('.session-item').first().click();
    await page
      .getByRole('tablist', { name: 'Inspector 视图' })
      .getByRole('tab', { name: 'Constraints' })
      .click();
    await expect(page.getByTestId('constraints-panel')).toBeVisible();
    await expectEachContrast(page, theme, ['.cn-value', '.cn-type', '.cn-meta', '.cn-pill', '.cn-revoke']);
    // 12px 地板：元信息行是面板里最小的字（效果图给 12px）。
    for (const sel of ['.cn-type', '.cn-meta', '.cn-pill']) {
      const m = await measure(page, sel);
      expect(m.fontSize, `${sel} font-size (${theme})`).toBe('12px');
    }
  });

  /* #1200 插件管理窗口：面板新立的全部可见文字（名称 / 元信息 / 来源标签 / 状态 pill /
     工具 chip / 错误条）在暗亮两主题下 ≥4.5 且守 12px 地板。状态 pill 与错误条是
     **语义色当正文色**的又一例（亮色纯 --warning 在白底/12% 染底上不达 AA），
     修法同 #1175/#1190 的 ink-mix；把 color 改回纯 token 时本用例必红。 */
  test(`[${theme}] #1200 插件管理窗口对比度 ≥4.5 且 12px 地板`, async ({ page }) => {
    await routeApi(page, {
      sessions: [],
      mcpServers: [
        {
          name: 'playwright', connected: true, enabled: true, needs_auth: false,
          tool_count: 2, require_confirm: false, transport: 'stdio',
          command: 'npx @playwright/mcp', url: '',
        },
        {
          name: 'github', connected: false, enabled: true, needs_auth: true,
          tool_count: 0, require_confirm: true, transport: 'http', command: '',
          url: 'https://example.test/mcp',
        },
      ],
      mcpTools: {
        playwright: [
          { name: 'browser_navigate', description: '打开页面', enabled: true },
          { name: 'browser_upload', description: '上传文件', enabled: false },
        ],
      },
      skills: [{
        name: 'code-review', description: '双轴代码审查', when_to_use: '',
        source: '/ws/skills/code-review/SKILL.md', scope: 'workspace',
        origin: 'project', enabled: true,
      }],
      mcpErrors: ['plugin-settings.json 不可读：示例'],
    });
    await page.addInitScript((t) => localStorage.setItem('ahi.theme', t), theme);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/');
    await page.getByRole('button', { name: '插件' }).click();
    await expect(page.getByTestId('plugin-panel')).toBeVisible();
    // 首行（playwright）默认选中 → 详情区与工具 chips 落屏；再点 github 看 amber pill。
    await expect(page.locator('.plugin-dialog .pill.green').first()).toBeVisible();
    await expectEachContrast(page, theme, [
      '.plugin-name',
      '.plugin-meta',
      '.plugin-tag',
      '.plugin-dialog .pill.green',
      '.plugin-dialog .tool-chip',
      '.plugin-errors',
    ]);
    await page.getByTestId('plugin-row-github').click();
    await expect(page.locator('.plugin-dialog .pill.amber').first()).toBeVisible();
    await expectEachContrast(page, theme, ['.plugin-dialog .pill.amber']);
    // 12px 地板：面板内所有可读文本（10/11px 在本轮审查里被清掉）。
    for (const sel of ['.plugin-meta', '.plugin-tag', '.plugin-dialog .pill.amber', '.plugin-errors']) {
      const m = await measure(page, sel);
      expect(m.fontSize, `${sel} font-size (${theme})`).toBe('12px');
    }
    // 名称是 13px（--text-sm，面板里的主标识）；只守地板，不钉具体档位。
    const nameSize = await measure(page, '.plugin-name');
    expect(parseFloat(nameSize.fontSize), `.plugin-name font-size (${theme})`).toBeGreaterThanOrEqual(12);
  });
}

/* ── #1204：`.ask-user-error`（提问卡片恢复失败条）语义色正文对比度回归锁 ──
 *
 *  内嵌 `AskUserCard` 的恢复失败条把 `--danger` 直接当**正文色**用，压在
 *  `--surface-danger`（8% 染底）上——亮色实测 4.0020 < 4.5（不达 AA）。修法同
 *  #1175/#1208 的 ink-mix：`color-mix(in srgb, var(--danger) 45%, var(--text-primary))`
 *  （亮色压深、暗色提亮，只升不降）；本元素仅纯文字，无图标需钉回原 token。
 *  夹具走**真实渲染路径**：耐久事件里 `user/input-requested(kind=ask_user)` 与
 *  `run/paused(reason=user_input)` 对齐 ⇒ 卡片落屏；点「提交」→ resume POST 落 404
 *  fallback ⇒ App 会话错误 ⇒ `.ask-user-error`。把字色改回纯 `--danger` 时本用例必红。 */
const AU_SID = 's-contrast-ask-user';
const AU_REQ = 'au-contrast-req';
const AU_ROW = {
  session_id: AU_SID,
  event_count: 5,
  first_event_time: T,
  last_event_time: T,
  first_user_message: '对比度提问',
  trace_id: null,
  trace_url: null,
};
function askUserContrastEvents(): FrameSpec[] {
  return [
    { type: 'session/started', seq: 1, session_id: AU_SID, run_id: 'ru', time: T },
    { type: 'run/started', seq: 2, session_id: AU_SID, run_id: 'ru', time: T },
    { type: 'user/message', data: { content: '对比度提问' }, seq: 3, session_id: AU_SID, run_id: 'ru', step_id: 1, time: T },
    {
      type: 'user/input-requested',
      data: {
        request_id: AU_REQ, kind: 'ask_user', run_id: 'ru',
        questions: [{
          question: '什么时候出发？', header: '出发',
          options: [{ label: '今晚' }, { label: '明早' }],
        }],
      },
      seq: 4, session_id: AU_SID, run_id: 'ru', step_id: 1, time: T,
    },
    {
      type: 'run/paused',
      data: { reason: 'user_input', input_request_id: AU_REQ, budget_version: 1 },
      seq: 5, session_id: AU_SID, run_id: 'ru', step_id: 1, time: T,
    },
  ];
}

for (const theme of ['dark', 'light'] as const) {
  test(`[${theme}] #1204 .ask-user-error（提问卡片恢复失败条）对比度 ≥4.5`, async ({ page }) => {
    await routeApi(page, {
      sessions: [AU_ROW],
      events: askUserContrastEvents(),
      models: MODELS, permissionModes: PERMISSION_MODES,
      agentProfiles: AGENT_PROFILES, reasoningEfforts: REASONING_EFFORTS,
    });
    await page.addInitScript((t) => localStorage.setItem('ahi.theme', t), theme);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/');
    await page.locator('.session-item').first().click();

    const card = page.locator('.ask-user-card');
    await expect(card).toBeVisible();
    // 提交 → resume POST 落 404 fallback（fixtures 默认）→ App 会话错误 → 失败条落屏。
    await page.locator('.ask-user-submit').click();

    const err = page.locator('.ask-user-error');
    await expect(err).toBeVisible();
    const m = await measure(page, '.ask-user-error');
    const ratio = contrast(m.color, m.bg);
    // eslint-disable-next-line no-console
    console.log(`[${theme}] .ask-user-error color=${m.color} bg=${m.bg} ratio=${ratio.toFixed(4)}`);
    expect(ratio, `.ask-user-error contrast (${theme})`).toBeGreaterThanOrEqual(4.5);
  });
}
