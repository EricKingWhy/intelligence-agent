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
 *  票面症状：亮色下 `.app-error`（--danger）3.53:1、`.app-notice`（--info）3.71:1、
 *  `.recover-btn`（--warning）2.97:1（hover 22% 底 2.71:1）全部 <4.5，而暗色同三项
 *  达标（5.66 / 6.50 / 8.12）——根因是**字色 token 本身**（在纯白底上限就 <4.5），
 *  调染色底 alpha 无解。修法同 #1029 的 ink-mix：`color-mix(in srgb, <sem> 45%,
 *  var(--text-primary))`（亮色 --text-primary 是深墨、混入即压深；暗色是近白、混入
 *  即提亮、对比度只升不降），装饰（描边/底/图标）继续引用原 token。
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
 *  `--danger`/`--info` 直接当**正文色**用，亮色下压在各自染色底上 <4.5（实测 2.9–4.1）。
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

/** 起会话 → 打开「任务审阅」面板（数据走共享的 mockTaskReviewApi，见 fixtures.ts）。 */
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
  // exact:true 是必须的：本用例的任务文案含「任务审阅」子串，`getByRole('button',
  // {name:'任务审阅'})` 走子串匹配会同时命中会话项按钮（其 accessible name 含首条
  // 用户消息）与时间线行（"user/message 对比度任务审阅"）→ strict mode 违规。
  await page.getByRole('button', { name: '任务审阅', exact: true }).click();
  await expect(page.getByRole('dialog', { name: '任务审阅' })).toBeVisible();
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
     （看不清就没人去裁决，会话永远卡着）。这里**真量**而不是信注释里的算术：
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

    // 同色域的另两处：#1030 新增的横幅人话层与「去裁决」按钮用的是**同一条**
    // color-mix 公式，但它们的底不是胶囊体而是页面底——底色不同则比值不同，必须
    // 各量各的（不能靠"公式一样"外推）。两处都是 12px 正文，同样受 AA 约束。
    // 横幅与面板互斥（面板在场时横幅让位，避免同一句话说两遍）⇒ 先关掉面板。
    await page.locator('.recovery-decision-close').click();
    await page.locator('.adjudication-banner').waitFor();
    for (const sel of ['.adjudication-headline', '.adjudication-open-btn']) {
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

    // 项目 alpha 的 kebab →「在此项目中新建任务」→ 弹窗填任务 → 创建任务。
    const alpha = page.locator('.rail-project').filter({ hasText: '项目 alpha' });
    await alpha.locator('.rail-project-head .rail-menu-btn').click();
    await page.getByRole('menuitem', { name: '在此项目中新建任务' }).click();
    const box = page.locator('.project-dialog[aria-label="新建任务"]');
    await expect(box).toBeVisible();
    await box.getByLabel('任务描述').fill(WT_TASK);
    await box.getByRole('button', { name: '创建任务' }).click();

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
    const panel = page.getByRole('dialog', { name: '任务审阅' });
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

  /* #1136 P3-1②：交付态 chip 两态（executing=--info / pending_verification=--warning）。
     chip 底是 `--surface-2`（不染色），但字色本身在亮色下仍 <4.5。product_state 单值，
     故关面板（hook 置 null 清态）改状态后再开重拉，一个用例量两态。 */
  test(`[${theme}] #1136 task-review 交付态 chip（executing / pending_verification）对比度 ≥4.5`, async ({ page }) => {
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
    await expect(page.getByRole('dialog', { name: '任务审阅' })).toBeHidden();
    state.task.product_state = 'pending_verification';
    await page.getByRole('button', { name: '任务审阅', exact: true }).click();
    const pending = page.locator('.task-review-chip.state-pending_verification');
    await expect(pending).toBeVisible();
    const pm = await measure(page, '.task-review-chip.state-pending_verification');
    const pr = contrast(pm.color, pm.bg);
    // eslint-disable-next-line no-console
    console.log(`[${theme}] .task-review-chip.state-pending_verification color=${pm.color} bg=${pm.bg} ratio=${pr.toFixed(4)}`);
    expect(pr, `.task-review-chip.state-pending_verification contrast (${theme})`).toBeGreaterThanOrEqual(4.5);
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
