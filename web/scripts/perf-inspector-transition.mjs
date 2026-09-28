/** F7(#278) —— Inspector 过渡编排的测量工具（真机 Chromium + **生产构建**）。
 *
 *  三件事，一件一个模式（可组合）：
 *    1. `--mode probe`   **可插值性探针**：票面「必做 1 / 必做 2」要求「`1fr` 与 `0px`
 *       之间是否可插值必须实测」。做法是往**真实 App 的真实 DOM**（dist 生产 CSS +
 *       mock API 喂出的 200 行 Timeline）注入候选 CSS，然后**逐帧**读**计算值**时序。
 *       不猜引擎行为，直接看 `getComputedStyle(.app-regions).gridTemplateColumns`
 *       在过渡期间出现了几个不同取值。
 *       ⚠ **结论必须落到「每一列」上，不能只看整串**（2026-09-19 实测纠正）：
 *       三列里只要有一列在连续插值（这里是 rail），整串取值个数就会很好看，
 *       从而**掩盖**另两列在过渡中点的一次性跳变。判据见 `trackStats()` 的逐帧跳幅。
 *    2. `--mode segments` G4 四段：打开 → 关闭 → 整页 → 退出整页，每段取
 *       **long task 数 + 最长单帧**（`docs/PERF_BASELINE.md` §2.1 口径，不是 FPS 均值）。
 *    3. `--mode shots`   过渡的**逐帧录屏**（CDP `Page.startScreencast`）——票面 AC1
 *       「至少 3 个中间态」的可看证据；截图的**捕获时刻**一并落 JSON，不靠"看起来像"。
 *
 *  **为什么用 `--inject` 而不是先改代码**（G3 要求「没落可复核基线数字不许动代码」）：
 *  `--inject` 把候选 CSS 以 `<style>` 注入 **unmodified 生产构建** ⇒ 在动一行源码之前
 *  就能拿到「改成这样要付多少代价」，并据此决定要不要收窄轨道/时长。
 *  ⚠ **候选声明与交付物已经不是同一份了**（F7 落地后）：`CANDIDATE_RULES` 记的是**加入
 *  `minmax(0, 0fr)` 修正之前**那一版，交付物里已含该修正 ⇒ `--mode probe` 的读数里，
 *  「交付物长什么样」看 **V3**（候选 + 0fr）或 **V5**（不注入、直接量 dist），不要拿 V1 当交付物。
 *  落地前两版曾逐值一致，那句「复采应与注入一致」自本条起不再通用。
 *
 *  **为什么不用 Playwright 的 `test()` 车道**（G4 明文「不引入 Playwright 帧率自动采集」）：
 *  本脚本用 `playwright` 库**直接**驱动 Chromium，不进 `e2e/`、不注册 use case、
 *  不被 `npm run test:e2e` 收集，与那条「有收尾挂死历史」的车道无关。
 *  与 F6 的 `perf-pulse-cost.mjs`、F2 的 `perf-longtask-live.mjs` 同形态。
 *
 *  **仪器自证**（防「测不出 ⇒ 误判」的假绿，同 F6 的纪律）：
 *    ① 正对照：同一个「数不同取值」的口径对准**确定可插值**的属性（`width: 100px → 0`）
 *       必须读出 ≥3 个取值；
 *    ② 负对照：对准**离散**属性（`visibility`）必须读出恰好 2 个取值；
 *    ③ 候选 CSS 被解析（读回 `transitionProperty` 断言含目标属性）+ 运行中的过渡被看到；
 *    ④ 采样密度自证（翻转后帧数），防止「只有 2 个取值」其实是采样太稀；
 *    ⑤ long task 探针的正对照：页面内 120ms 忙等必须被读到（1 条 / ≈120ms）。
 *       ⚠ 忙等必须写在**页面里**：实测 CDP 注入的 `page.evaluate` 忙等**不产生** longtask
 *       条目 —— 用错写法会得到「控制组也是 0」的自证失败（本票真撞上了，见 `_f7_lt_probe.mjs`）。
 *  ①②一起才让「不同取值个数」有意义：它既能看见插值，也能认出离散。
 *  ⑤ 不通过则四段的「long task 数 = 0」全部作废（不得当成绩效结论）。
 *
 *  跑法（先 `npm run build`，脚本读 `web/dist`）：
 *    cd web
 *    node scripts/perf-inspector-transition.mjs --mode probe               # V0/V1/V2 三臂，答可插值性 + AC1 红证
 *    node scripts/perf-inspector-transition.mjs --mode segments            # 改造前（shipped）四段基线
 *    node scripts/perf-inspector-transition.mjs --mode segments --inject   # 受控臂：改成这样要付多少
 *    node scripts/perf-inspector-transition.mjs --mode segments --shipped  # 改造后复采（与上面等价）
 *    node scripts/perf-inspector-transition.mjs --mode shots               # 过渡逐帧录屏
 *    node scripts/perf-inspector-transition.mjs --mode all --json %TEMP%/f7.json
 *
 *  ⚠ **AC8 的对照必须交替跑**（本机噪声非平稳，跑一次 before、再跑一次 after 会得出反的结论）：
 *    node scripts/perf-inspector-transition.mjs --mode segments --before --reps 8 --json %TEMP%/f7-ab-b1.json
 *    node scripts/perf-inspector-transition.mjs --mode segments --inject --reps 8 --json %TEMP%/f7-ab-a1.json
 *    node scripts/perf-inspector-transition.mjs --mode segments --before --reps 8 --json %TEMP%/f7-ab-b2.json
 *    node scripts/perf-inspector-transition.mjs --mode segments --inject --reps 8 --json %TEMP%/f7-ab-a2.json
 *    ⇒ 两个臂**都注入**样式表（对称），只有「有无过渡」这一个变量；四块合起来看。
 */

import { createServer } from 'node:http';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { extname, join, resolve, sep } from 'node:path';
import { chromium } from '@playwright/test';

// 代理会让对 127.0.0.1 的请求发不出去，先清掉（同 perf-pulse-cost.mjs）。
for (const k of ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy']) {
  delete process.env[k];
}

const arg = (name, fallback) => {
  const i = process.argv.indexOf(`--${name}`);
  return i !== -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
};

const DIST = resolve('dist');
const MODE = arg('mode', 'all'); // probe | segments | shots | all
const MS = Number(arg('ms', '700')); // 每段测量窗口（须 > 最长过渡 240ms + 收尾）
const REPS = Number(arg('reps', '3'));
const SHOTS = arg('shots', join('gui-test-screenshots', 'perf-inspector-transition'));
const JSON_OUT = arg('json', '');
const HEADED = process.argv.includes('--headed');
const INJECT = process.argv.includes('--inject');
const SHIPPED = process.argv.includes('--shipped');
/** **归因臂，不是候选实现**：把 Inspector 的内容从布局/绘制里摘掉，用来回答
 *  「过渡期间那几次 33ms 帧间隔，是不是面板内容逐帧重排造成的」。
 *  它**不随票进生产**（`contain` / `content-visibility` 的归属是 F5）。 */
const ATTR = process.argv.includes('--attr');
/** **A/B 的「前」臂**：在同一个交付物上注入改造前语义（`transition:none` + `width:0`）。
 *  为什么需要它：`shipped` 与 `--inject`（候选 CSS）**其实是同一份声明**，两次运行却量到
 *  10% vs 32.5% 的 33ms 帧率 ⇒ 本机噪声**非平稳**，「跑一次 before、再跑一次 after」的
 *  对照**不成立**。要下 AC8 的结论只能**交替**跑同一个臂序（见本文件 DoD 里的四块 A/B/A/B）。 */
const BEFORE = process.argv.includes('--before');
const SEG_FILTER = arg('segs', '').split(',').map((s) => s.trim()).filter(Boolean);
const STEPS = Number(arg('steps', '40')); // 会话规模：40 步 ⇒ 243 事件 ⇒ Timeline 尾窗满 200 行

if ([INJECT, SHIPPED, BEFORE, ATTR].filter(Boolean).length > 1)
  throw new Error('--inject / --shipped / --before / --attr 互斥（它们是同一根「被测 CSS」旋钮的四个档位）');

// ─── 候选 CSS（F7 形态之**加入 `minmax(0, 0fr)` 修正之前**的那一版；探针注入的就是这一段） ───
/* 方向差异是这张票的技术核心，写法说明：
 *  · CSS 过渡取的是**变化后**（after-change style）那条规则上的 `transition-*`
 *    ⇒ 把「关闭方向」写在 `.inspector-closed` / `.inspector-fullpage` 自己身上、
 *      「打开方向」写在 `.app-regions` 基线上，方向就自然分开了，**不需要** JS 状态。
 *  · 打开方向不给 `.step-detail` / rail / workspace 写 transition：后态是基线规则，
 *    基线没声明 `transition-duration`（初值 `0s`）⇒ 可见性**立即**切换，正是要的行为。
 *  · 关闭方向用 `visibility 0s linear var(--dur-out)`：时长 0 + 延迟 = 过渡时长
 *    ⇒ 先保持可见、过渡**结束后**才 hidden。`visibility` 本身可插值（visible↔hidden
 *    按离散插值），**不需要** `transition-behavior: allow-discrete`——那条只对真·离散
 *    属性（如 `display`）起作用；探针的 V2 臂就是来证伪/证实这一点的。
 *  · `:has(.step-detail[data-resizing='true'])`：拖宽手柄（#197）每帧改 `--inspector-w`，
 *    而 `--inspector-w` 正是第三列的轨道宽度 ⇒ 不加这条，拖拽会带上 240ms 滞后。
 *  · ⚠ 这一段**不等于**交付物：交付物的 `.inspector-fullpage` 中间轨道已改写成
 *    `minmax(0, 0fr)`（V3 臂就是这一处差异，也是最终落盘形态）。
 */
const CANDIDATE_RULES = `
.app-regions {
  transition: grid-template-columns var(--dur-in) var(--ease-apple);
}
.inspector-closed {
  transition: grid-template-columns var(--dur-out) var(--ease-apple);
}
.inspector-closed .step-detail {
  /* ★ 这一行是探针量出来的，不是审美：app.css 现状的 .inspector-closed .step-detail
     带着 width: 0，面板盒**一帧内**收成 0 —— 于是栅格列在平滑插值、面板内容却已经
     没了（正是票面 AC2 禁止的「内容先没了、列再收」）。实测证据见本文件 close 段。 */
  width: auto;
  transition: visibility 0s linear var(--dur-out);
}
.app-regions.inspector-fullpage {
  transition: grid-template-columns var(--dur-out) var(--ease-apple);
}
.app-regions.inspector-fullpage .session-rail,
.app-regions.inspector-fullpage .app-workspace {
  transition: visibility 0s linear var(--dur-out);
}
`;

/** reduce 下把**延迟**也复位。必要性由测量给出，不是"保险起见"：全局块
 *  （`index.css:368-376`）只把 `transition-duration` 压成 0.01ms，**不碰
 *  `transition-delay`** ⇒ 不写这一段，关闭态的 visibility 仍会被推迟 150ms。 */
const REDUCED_DELAY_RESET = `
@media (prefers-reduced-motion: reduce) {
  .app-regions,
  .inspector-closed,
  .inspector-closed .step-detail,
  .app-regions.inspector-fullpage,
  .app-regions.inspector-fullpage .session-rail,
  .app-regions.inspector-fullpage .app-workspace {
    transition-delay: 0s;
  }
}
`;

const CANDIDATE_BASE = CANDIDATE_RULES + REDUCED_DELAY_RESET;

/** 拖宽守卫（#197）：手柄每帧改 `--inspector-w`，而它正是第三列的轨道宽度 ⇒
 *  不加这条，拖拽会带上 240ms 滞后。单列出来是为了能做 A/B（G1 有守卫 / G0 没守卫）。 */
const GUARD_RULE = `
.app-regions:has(.step-detail[data-resizing='true']) {
  transition: none;
}
`;

const CANDIDATE_CSS = CANDIDATE_BASE + GUARD_RULE;

/** **G0 对照臂的必需补丁**（2026-09-19 实测撞到，别删）：守卫**已经进了交付物**，
 *  而它的优先级是 `(0,3,0)`（`:has()` 取最内层选择器 `.step-detail[data-resizing='true']`）。
 *  ⇒ 单靠「注入表里不写守卫」**造不出**对照臂：交付物那份仍然生效（实测 G0 也报「1 帧」，
 *  与 G1 无区别 —— 一条失效的对照臂会把「守卫必要」这个结论悄悄变成不可复现）。
 *  正确做法：用**同优先级、后出现**的一条把守卫盖回成正常过渡，且时长取 **`--dur-in`**——
 *  那才是忠实的反事实：守卫不存在时，拖拽期间生效的就是 `.app-regions` 基线上那条 240ms 过渡。 */
const GUARD_NEUTRALIZED = `
.app-regions:has(.step-detail[data-resizing='true']) {
  transition: grid-template-columns var(--dur-in) var(--ease-apple);
}
`;
const CANDIDATE_CSS_NO_GUARD = CANDIDATE_BASE + GUARD_NEUTRALIZED;

/** 对照臂：去掉 reduce 的延迟复位（证明上面那一段不是摆设）。 */
const CANDIDATE_CSS_NO_DELAY_RESET = CANDIDATE_RULES + GUARD_RULE;

/** **归因臂专用，不进生产**：把 Inspector 的**滚动内容**（200 行 Timeline）从布局/绘制里摘掉。
 *  问的是「过渡期间偶发的 33ms 帧间隔是不是面板内容逐帧重排造成的」。
 *  `contain` / `content-visibility` 的归属是 F5，本票只用它做因果归因。
 *  ⚠ 只摘 `.detail-body`，**不能**摘 `.detail-header`：关闭按钮就在头部，
 *    `content-visibility: hidden` 会让它不可点，直接卡死前置态收敛（实测踩过）。 */
const ATTR_CSS = `${CANDIDATE_CSS}
.detail-body {
  content-visibility: hidden;
}
`;

/** **V3：把整页态的中间轨道从 `0px` 写成 `minmax(0, 0fr)`** —— 只为一个问题：
 *  「`1fr ↔ 0px` 不可插值，那 `1fr ↔ 0fr` 行不行」。
 *  两者**静态解一样（0px）**，不是改布局模型（票面 Scope lock 允许范围内的一次实测）；
 *  若行，整页段就只剩 inspector 那条轨道仍然离散。 */
const CANDIDATE_CSS_WORKSPACE_0FR = `${CANDIDATE_CSS}
.app-regions.inspector-fullpage {
  grid-template-columns: 0px minmax(0, 0fr) minmax(0, 1fr);
}
`;

/** **V4：慢速放大臂（只是一个仪器）**。真实时长只有 150/240ms ⇒ 每段 ~9-15 帧；
 *  「某条轨道到底有没有插值」用逐帧跳幅判更稳：把两段时长一起放大到 2000ms，
 *  得到 ~120 帧，插值轨道的相邻帧变化会小到 1/10 以下，离散翻转则仍是「一帧吃掉全幅」。
 *  ⚠ 它**只用于回答可插值性**，不能用来报时长/成本数字（那不是交付物的时序）。 */
const CANDIDATE_CSS_SLOW = `${CANDIDATE_CSS}
:root { --dur-in: 2000ms; --dur-out: 2000ms; }
`;

/** 探针的 V2 对照臂：额外声明 `transition-behavior: allow-discrete`。
 *  用途**只是**回答「票面推荐 A 里那个 allow-discrete 有没有作用」——两臂读数若一致，
 *  就证明它对本票无用（也证明我们对它的理解成立，而不是抄一条看不懂的声明进生产 CSS）。 */
const CANDIDATE_CSS_ALLOW_DISCRETE = `${CANDIDATE_CSS}
.app-regions,
.inspector-closed,
.app-regions.inspector-fullpage {
  transition-behavior: allow-discrete;
}
.inspector-closed .step-detail,
.app-regions.inspector-fullpage .session-rail,
.app-regions.inspector-fullpage .app-workspace {
  transition-behavior: allow-discrete;
}
`;

// ─────────────────────────────── mock API（同源起在静态服务上） ───────────────────────────────
/** **红证臂（不注入就不叫红证）**：把改造前的语义在同一个交付物上「还原」出来——
 *  `transition: none` + `width: 0`，正是改造前 app.css 的实际行为（一帧跳变）。
 *  用途：给 AC1 提供 A/B 的**前**半边（改造前轨道只有 1 种取值）。
 *  为什么不用「拿改造前的 dist 单独跑一次」：那样两次运行的机器状态、mock 事件、
 *  视口都可能不同，A/B 会掺进环境变量；同一次运行内注入，被测 CSS 是唯一变量。 */
const BEFORE_CSS = `
.app-regions {
  transition: none;
}
.inspector-closed {
  transition: none;
}
.inspector-closed .step-detail {
  width: 0;
  transition: none;
}
.app-regions.inspector-fullpage {
  transition: none;
}
.app-regions.inspector-fullpage .session-rail,
.app-regions.inspector-fullpage .app-workspace {
  transition: none;
}
`;

const SID = 'f7-session-0001';
const RUN = 'f7-run-0001';
const T = '2026-09-18T00:00:00Z';

/** 会话内容：N 步 × 6 事件。规模刻意顶到 Timeline 尾窗（200 行）——
 *  票面 Risks 担心的正是「三列同时过渡 + 内部**大量节点**重排」，取小样会把风险测没了。 */
function makeEvents(steps) {
  const out = [];
  let seq = 0;
  const push = (type, data, extra = {}) =>
    out.push({ type, data, seq: (seq += 1), session_id: SID, run_id: RUN, time: T, ...extra });
  push('session/started', { cwd: 'D:\\work\\f7' });
  push('run/started', { agent_profile: 'main' });
  for (let s = 1; s <= steps; s += 1) {
    push('user/message', { content: `第 ${s} 步：查看 src 模块 ${s} 并给出结论` }, { step_id: s });
    push('model/started', { model: 'f7-model' }, { step_id: s });
    push('text/delta', { delta: `第 ${s} 步推理：先看目录，再读 module_${s}.py。` }, { step_id: s });
    push('tool/call', { tool_call_id: `tc-${s}`, tool_name: 'bash', args: { command: `ls -la src/module_${s}` } }, { step_id: s });
    push('tool/result', { tool_call_id: `tc-${s}`, content: JSON.stringify({ ok: true, step: s, files: 3 }) }, { step_id: s });
    push('model/completed', { model: 'f7-model' }, { step_id: s });
  }
  push('run/completed', {});
  return out;
}

const EVENTS = makeEvents(STEPS);

const sessionsRow = {
  session_id: SID,
  event_count: EVENTS.length,
  first_event_time: T,
  last_event_time: T,
  first_user_message: 'F7 过渡编排的载体会话',
  trace_id: null,
  trace_url: null,
  archived: false,
};

/** 与 `e2e/fixtures.ts` 缺省值同形的那几条（不 mock 会让界面进错误态、多挂一堆横幅，
 *  那些横幅本身就是 long task 来源）。少而准：App 首屏不炸 + Inspector 有内容即可。 */
const CORE_CAPABILITY = {
  id: 'core',
  display_name: '内置工具',
  version: '1.0.0',
  provider_name: 'builtin',
  surfaces: { chat: true, timeline: true, changes: true, terminal: true, artifacts: false },
  actions: { permissions: true, stop: true, retry: false, resume: true },
};

function apiResponse(pathname, method) {
  const json = (body, status = 200) => ({ status, body: JSON.stringify(body), type: 'application/json' });
  if (pathname === '/api/health') return json({ status: 'ok' });
  if (pathname === '/api/sessions' && method === 'GET') return json([sessionsRow]);
  if (/^\/api\/sessions\/[^/]+\/events$/.test(pathname)) return json(EVENTS);
  if (pathname === '/api/projects') return json([]);
  if (pathname === '/api/models') return json({ models: [] });
  if (pathname === '/api/permission-modes') return json({ modes: [] });
  if (pathname === '/api/agent-profiles') return json({ profiles: [] });
  if (pathname === '/api/reasoning-efforts') return json({ efforts: [] });
  if (pathname === '/api/context-providers') return json({ providers: [] });
  if (pathname === '/api/capabilities') return json({ capabilities: [CORE_CAPABILITY] });
  if (pathname === '/api/memories') return json([]);
  if (/^\/api\/sessions\/[^/]+\/queue$/.test(pathname)) return json({ items: [], steers: [] });
  if (/^\/api\/sessions\/[^/]+\/context-usage$/.test(pathname)) {
    return json({
      estimated: true,
      window_tokens: 200000,
      used_tokens: 0,
      thresholds: { auto_compact: 0.7, hard_guard: 0.85 },
      breakdown: { messages: 0, system_prompt: 0, skills: 0, other: 0, tools: { system: 0, mcp: 0 } },
      cache: { state: 'not_collected', reported_calls: 0, total_calls: 0, avg_hit_rate: null },
      state: 'no_data',
    });
  }
  return null;
}

// ───────────────────────────── 静态服务（dist + mock API） ─────────────────────────────
const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.woff2': 'font/woff2',
  '.woff': 'font/woff',
};

async function serveDist() {
  const index = await readFile(join(DIST, 'index.html'));
  const server = createServer(async (req, res) => {
    const url = new URL(req.url ?? '/', 'http://127.0.0.1');
    const pathname = decodeURIComponent(url.pathname);
    if (pathname.startsWith('/api/')) {
      const hit = apiResponse(pathname, req.method ?? 'GET');
      if (hit) {
        res.writeHead(hit.status, { 'content-type': hit.type });
        res.end(hit.body);
      } else {
        // 与 e2e fixture 同形：没 mock 的端点回 404 而不是静默 200。
        res.writeHead(404, { 'content-type': 'application/json' });
        res.end('{"detail":"not mocked in perf-inspector-transition"}');
      }
      return;
    }
    try {
      const file = join(DIST, pathname);
      const resolved = resolve(file);
      // 前缀判断必须带分隔符（否则同级目录 `dist2/` 会被放通）；但根路径 `/` 解析出来
      // **正好等于** DIST（没有尾分隔符），所以它要单独放行 —— 漏掉这一步会把
      // index.html 自己 403 掉，页面白屏（F7 实测踩过）。
      if (resolved !== DIST && !resolved.startsWith(DIST + sep)) {
        res.writeHead(403);
        res.end();
        return;
      }
      const body = await readFile(file); // 目录会抛 EISDIR ⇒ 走下面的 SPA 兜底
      res.writeHead(200, { 'content-type': MIME[extname(file)] ?? 'application/octet-stream' });
      res.end(body);
    } catch {
      // SPA 兜底：非静态资源（含 `/`）一律给 index.html
      res.writeHead(200, { 'content-type': MIME['.html'] });
      res.end(index);
    }
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  return { server, port: server.address().port };
}

// ───────────────────────────── 页面侧仪器 ─────────────────────────────

/** 探针采样器：**逐帧**读计算值。刻意读计算值而不是 `getBoundingClientRect`——
 *  本票问的就是「轨道宽有没有插值」，那是 `grid-template-columns` 的计算值本身。 */
const SAMPLER = () => {
  const pick = () => {
    const regions = document.querySelector('.app-regions');
    const detail = document.querySelector('.step-detail');
    const rail = document.querySelector('.session-rail');
    const ws = document.querySelector('.app-workspace');
    const cs = (el) => (el ? getComputedStyle(el) : null);
    const r = cs(regions);
    const d = cs(detail);
    const ra = cs(rail);
    const w = cs(ws);
    return {
      t: performance.now(),
      cls: regions?.className ?? null,
      gtc: r ? r.gridTemplateColumns : null,
      resizing: detail?.getAttribute('data-resizing') ?? null,
      dW: d ? d.width : null,
      dVis: d ? d.visibility : null,
      dPadL: d ? d.paddingLeft : null,
      railW: ra ? ra.width : null,
      railVis: ra ? ra.visibility : null,
      wsW: w ? w.width : null,
      wsVis: w ? w.visibility : null,
      // 运行中的过渡：`transitionProperty:durationMs` —— 证明「真的开了过渡」而不是没生效
      anims: document
        .getAnimations()
        .filter((a) => a.playState === 'running')
        .map((a) => `${a.transitionProperty ?? a.animationName ?? '?'}:${Math.round(a.effect?.getTiming?.().duration ?? 0)}`),
    };
  };
  window.__s = { list: [], on: false };
  const tick = () => {
    if (window.__s.on) window.__s.list.push(pick());
    window.__raf = requestAnimationFrame(tick);
  };
  window.__raf = requestAnimationFrame(tick);
};

/** 仪器自证的①②：同一个「数不同取值」的口径，分别对准**可插值**与**离散**的属性。
 *  正对照必须 ≥3 个取值、负对照必须恰好 2 个 —— 两者一起才让后面的读数有意义。 */
const CONTROL = async () => {
  const a = document.createElement('div');
  a.style.cssText = 'position:fixed;left:-9999px;top:0;height:10px;width:100px;transition:width 240ms linear;';
  const b = document.createElement('div');
  b.style.cssText = 'position:fixed;left:-9999px;top:0;height:10px;width:100px;visibility:visible;transition:visibility 240ms linear;';
  document.body.append(a, b);
  await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  const series = { w: [], v: [] };
  a.style.width = '0px';
  b.style.visibility = 'hidden';
  const t0 = performance.now();
  await new Promise((done) => {
    const tick = () => {
      series.w.push(getComputedStyle(a).width);
      series.v.push(getComputedStyle(b).visibility);
      if (performance.now() - t0 < 420) requestAnimationFrame(tick);
      else done();
    };
    requestAnimationFrame(tick);
  });
  a.remove();
  b.remove();
  const uniq = (xs) => [...new Set(xs)];
  return {
    interpolable: { samples: series.w.length, distinct: uniq(series.w).length, values: uniq(series.w).slice(0, 8) },
    discrete: { samples: series.v.length, distinct: uniq(series.v).length, values: uniq(series.v) },
  };
};

/** G4 采集器（**不读计算值**，只数 long task + 数帧）——读数开销必须接近零，
 *  否则量到的是仪器自己的成本。与 F6 的 INIT 同口径。 */
const INIT_METRICS = () => {
  window.__lt = [];
  window.__ltErr = null;
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        window.__lt.push({ start: Math.round(e.startTime), dur: Math.round(e.duration) });
      }
    }).observe({ entryTypes: ['longtask'] });
  } catch (err) {
    window.__ltErr = String(err);
  }
  window.__frames = [];
  window.__sampling = false;
  const tick = (t) => {
    if (window.__sampling) window.__frames.push(t);
    window.__raf = requestAnimationFrame(tick);
  };
  window.__raf = requestAnimationFrame(tick);
};

const frameStats = (frames) => {
  if (frames.length < 3) return { frameCount: frames.length, frameMaxMs: null, dropped: null, severe: null };
  const d = [];
  for (let i = 1; i < frames.length; i += 1) d.push(frames[i] - frames[i - 1]);
  return {
    frameCount: frames.length,
    frameMaxMs: +Math.max(...d).toFixed(1),
    dropped: d.filter((x) => x > 20).length, // 掉一帧（vsync 周期 ~16.7ms）
    severe: d.filter((x) => x > 33).length, // 掉两帧以上 = 可感卡顿
  };
};

const median = (xs) => {
  const s = [...xs].filter((v) => typeof v === 'number').sort((a, b) => a - b);
  if (!s.length) return null;
  const m = s.length % 2 ? s[(s.length - 1) / 2] : (s[s.length / 2 - 1] + s[s.length / 2]) / 2;
  return +m.toFixed(2);
};

// ───────────────────────────── 四段操作 ─────────────────────────────

/** 四段的定义。前置一律先做到位并**静置**（见 `reachPrecondition`），
 *  否则上一段的过渡尾巴会被算进下一段，读数就不是「这一段操作」的成本了。 */
const SEGMENTS = [
  {
    id: 'open',
    label: '打开 Inspector（closed → open）',
    /** 切换完成后应成立的类名条件 */
    flag: (cls) => !cls.includes('inspector-closed'),
    /** 切换前应成立的类名条件（用来在同一次采样序列里定位「翻转发生在第几帧」） */
    before: (cls) => cls.includes('inspector-closed'),
    act: (p) => clickOpen(p),
  },
  {
    id: 'close',
    label: '关闭 Inspector（open → closed）',
    flag: (cls) => cls.includes('inspector-closed'),
    before: (cls) => !cls.includes('inspector-closed'),
    act: (p) => clickClose(p),
  },
  {
    id: 'fullpage',
    label: '进入整页（triptych → fullpage）',
    flag: (cls) => cls.includes('inspector-fullpage'),
    before: (cls) => !cls.includes('inspector-fullpage'),
    act: (p) => clickFullpage(p),
  },
  {
    id: 'exit-fullpage',
    label: '退出整页（fullpage → triptych）',
    flag: (cls) => !cls.includes('inspector-fullpage'),
    before: (cls) => cls.includes('inspector-fullpage'),
    act: (p) => clickFullpage(p),
  },
].filter((s) => !SEG_FILTER.length || SEG_FILTER.includes(s.id));

const regionsCls = (p) => p.locator('.app-regions').getAttribute('class').then((c) => c ?? '');
const isOpen = (c) => !c.includes('inspector-closed');
const isClosed = (c) => c.includes('inspector-closed');
const isFull = (c) => c.includes('inspector-fullpage');

/** 前置态收敛的小工具：**语义命名**，不再让调用方拿「段 id」猜状态
 *  （探针第一版就因为把「open 段的前置 = 面板已关闭」当成「面板打开」，
 *   对着 visibility:hidden 的拖宽手柄拖了一把空气 —— 见 runDragProbe）。 */
const clickOpen = (p) => p.getByRole('button', { name: '展开 Inspector' }).first().click();
const clickClose = (p) => p.locator('button[aria-label="关闭 Inspector"]').click();
const clickFullpage = (p) => p.locator('.detail-ctrl[aria-label="整页"]').click();

async function ensureNotFull(p) {
  if (!isFull(await regionsCls(p))) return;
  await clickFullpage(p);
  await p.waitForTimeout(500);
}
async function ensureOpen(p) {
  await ensureNotFull(p);
  if (isOpen(await regionsCls(p))) return;
  await clickOpen(p);
  await p.waitForTimeout(500);
}
async function ensureClosed(p) {
  await ensureNotFull(p); // 整页态下没有「关闭 Inspector」这个动作
  if (isClosed(await regionsCls(p))) return;
  await clickClose(p);
  await p.waitForTimeout(500);
}
async function ensureFull(p) {
  await ensureOpen(p);
  if (isFull(await regionsCls(p))) return;
  await clickFullpage(p);
  await p.waitForTimeout(500);
}

/** 把界面带到某段所需的前置态，并**静置** `settleMs`：让上一段的过渡与收尾彻底走完，
 *  否则它的尾巴会被算进这一段，读数就不是「这一段操作」的成本了。
 *  `settleMs` 默认取测量窗 `MS`；**放大时长臂（V4）必须显式传更长的静置**，
 *  否则上一段那条 2000ms 的过渡还在跑，翻转帧就落在它的中途。 */
async function reachPrecondition(p, segId, settleMs = MS) {
  if (segId === 'open') await ensureClosed(p);
  else if (segId === 'close') await ensureOpen(p);
  else if (segId === 'fullpage') await ensureOpen(p);
  else if (segId === 'exit-fullpage') await ensureFull(p);
  await p.waitForTimeout(settleMs);
}

// ───────────────────────────── 主流程 ─────────────────────────────

const { server, port } = await serveDist();
const BASE = `http://127.0.0.1:${port}`;
await mkdir(SHOTS, { recursive: true });

const browser = await chromium.launch({ headless: !HEADED });
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });

// WS 接流通道：不 mock 的话 App 走 SSE 降级 + 重连调度，那些重连本身就会造 long task，
// 把 F7 读数污染掉。与 `e2e/fixtures.ts::installWsRoute` 同形：快照即全量、无在途 run。
// 挂在 **context** 上而不是 page 上——本脚本每个模式各开一个 page，挂 page 会漏掉后面几个。
await ctx.routeWebSocket(/\/api\/ws$/, (ws) => {
  ws.onMessage((raw) => {
    let msg;
    try {
      msg = JSON.parse(String(raw));
    } catch {
      return;
    }
    if (msg.type !== 'subscribe' || !msg.session_id) return;
    ws.send(JSON.stringify({ type: 'snapshot', session_id: msg.session_id, events: EVENTS, has_active_run: false }));
  });
});

const results = { probe: null, segments: null, shots: [], env: null };

const logEnv = () => {
  console.log(`  base=${BASE}  dist=${DIST}  viewport=1440×900  headed=${HEADED}  事件数=${EVENTS.length}（Timeline 尾窗 200 行）`);
};

/** 每个模式一个新 page（仪器不同、生命周期互不干扰），并等真实内容就位。
 *  等待失败时**把页面自己说的话打出来**（控制台错误 / 失败的请求 / 可见文本）——
 *  否则只能看到 locator 超时，查不出是 mock 形状不对还是页面炸了。 */
async function freshPage(initScript) {
  const p = await ctx.newPage();
  const noise = [];
  p.on('console', (m) => {
    if (m.type() === 'error') noise.push(`[console.error] ${m.text().slice(0, 240)}`);
  });
  p.on('pageerror', (e) => noise.push(`[pageerror] ${String(e.message).slice(0, 240)}`));
  p.on('requestfailed', (r) => noise.push(`[reqfail] ${r.url()} ${r.failure()?.errorText}`));
  p.on('response', (r) => {
    if (r.status() >= 400) noise.push(`[status ${r.status()}] ${new URL(r.url()).pathname}`);
  });
  if (initScript) await p.addInitScript(initScript);
  await p.goto(`${BASE}/`, { waitUntil: 'load' });
  try {
    await p.locator('.session-item').first().click({ timeout: 20000 });
    await p.locator('.timeline-row').first().waitFor({ timeout: 20000 });
  } catch (err) {
    const seen = await p.evaluate(() => ({
      text: document.body.innerText.slice(0, 400),
      counts: { sessionItem: document.querySelectorAll('.session-item').length, appRegions: document.querySelectorAll('.app-regions').length },
    }));
    console.log('  ⚠ 界面没进入可用态，页面自述如下：');
    console.log(`     DOM 计数：${JSON.stringify(seen.counts)}`);
    console.log(`     可见文本：${JSON.stringify(seen.text)}`);
    for (const n of [...new Set(noise)].slice(0, 12)) console.log(`     ${n}`);
    throw err;
  }
  const rows = await p.locator('.timeline-row').count();
  return { p, rows };
}

/** **逐轨道**判定「插值 vs 离散翻转」——本票最关键的仪器口径。
 *  ⚠ 第一版只数「整串 `grid-template-columns` 有几个取值」，而那一串**被 rail 主导**：
 *  整页两段里 rail 一直在连续插值，于是「10 种 / 16 种」看着像可插值，
 *  把另两条轨道在过渡中点的一次性跳变**盖掉了**。判据必须落到**每一列**上。
 *  做法：逐帧把计算值拆成三个数，看**相邻帧最大跳幅**占全幅的比例——
 *    · 连续插值：跳幅 ≈ 全幅 / 帧数（真实时长下 ~1/10，放大时长下 ~1/100）；
 *    · 离散翻转：某一帧**一帧吃掉整个行程**（比例 ≈ 1）。
 *  真实时长（150/240ms）帧太稀说不出话，所以另有 V4（2000ms）把采样密度拉起来复核。 */
const trackStats = (series, preGtc) => {
  // ⚠ 存 `{v, t}` 而不是裸数字：有些帧的 `gtc` 解析不出三个数（翻转瞬间/被跳过的帧），
  //   裸数组会与 `series` 下标错位，`maxJumpAt` 指到的就不是那一帧（实测踩过）。
  // ⚠ **把「翻转前最后一帧」也放进序列**：不这么做，`transition: none` 的红证臂会读成
  //   「全程只有一个取值 ⇒ 没跳变」——因为跳变恰好发生在序列的第一个采样点上。
  //   放进去之后，「一帧吃掉全幅」与「平滑插值」才能用同一个口径量出来。
  const cols = [[], [], []];
  const pre = (preGtc ?? '').split(/\s+/).map((x) => parseFloat(x));
  const preOk = pre.length === 3 && pre.every((v) => Number.isFinite(v)) && series.length > 0;
  if (preOk) for (let i = 0; i < 3; i += 1) cols[i].push({ v: pre[i], t: series[0].t - 1 });
  for (const s of series) {
    const t = (s.gtc ?? '').split(/\s+/).map((x) => parseFloat(x));
    if (t.length === 3 && t.every((v) => Number.isFinite(v))) for (let i = 0; i < 3; i += 1) cols[i].push({ v: t[i], t: s.t });
  }
  const names = ['rail', 'workspace', 'inspector'];
  const out = {};
  cols.forEach((pts, i) => {
    if (pts.length < 2) {
      out[names[i]] = { samples: pts.length, distinct: new Set(pts.map((p) => p.v)).size };
      return;
    }
    let maxJump = 0;
    let at = 1; // 初值必须合法：全程无跳变时它会被原样返回，用 -1 会读到 undefined
    for (let k = 1; k < pts.length; k += 1) {
      const d = Math.abs(pts[k].v - pts[k - 1].v);
      if (d > maxJump) { maxJump = d; at = k; }
    }
    const from = pts[0].v;
    const to = pts[pts.length - 1].v;
    const range = Math.abs(to - from);
    const ratio = range < 1 ? 0 : maxJump / range;
    const flipIsJump = preOk && at === 1; // 最大跳幅就发生在「翻转那一格」
    out[names[i]] = {
      samples: pts.length,
      distinct: new Set(pts.map((p) => p.v)).size,
      from, to, range: +range.toFixed(2),
      maxJump: +maxJump.toFixed(2), maxJumpAt: at, maxJumpMs: Math.round(pts[at].t - pts[0].t),
      verdict: range < 1
        ? '不变'
        : ratio >= 0.5
          ? flipIsJump ? '无过渡（一帧到位）' : '离散翻转'
          : '插值',
    };
  });
  return out;
};

// ── 模式 1：可插值性探针 ──
async function runProbe() {
  console.log('════════ F7 可插值性探针（注入候选 CSS，逐帧读计算值） ════════');
  logEnv();
  const { p, rows } = await freshPage(SAMPLER);
  if (!rows) throw new Error('.timeline-row 为 0 —— mock API 没喂出内容，探针结论无效');
  console.log(`  真实 DOM：.timeline-row=${rows}（0 会让探针失去意义）`);

  const ctrl = await p.evaluate(CONTROL);
  const ctrlOk = ctrl.interpolable.distinct >= 3 && ctrl.discrete.distinct === 2;
  console.log(`  自证① 正对照 width 100px→0（可插值）：${ctrl.interpolable.distinct} 个取值 / ${ctrl.interpolable.samples} 帧` +
    ` → ${ctrl.interpolable.values.join(' → ')}`);
  console.log(`  自证② 负对照 visibility（离散）：${ctrl.discrete.distinct} 个取值 [${ctrl.discrete.values.join(', ')}]`);
  console.log(`  → 「数不同取值」这一口径 ${ctrlOk ? '✅ 既能看见插值、也能认出离散' : '❌ 自证失败，本节读数作废'}`);
  console.log(`  引擎能力：transition-behavior:allow-discrete = ${(await p.evaluate(() => CSS.supports('transition-behavior', 'allow-discrete'))) ? '支持' : '不支持'}`);
  console.log('');

  /** V4 是慢速放大臂：过渡本身 2000ms ⇒ 静置与采样窗都要跟着放大，
   *  否则读到的只是那条过渡的前 700ms（尾段与「落位」永远看不见）。 */
  const SLOW_WIN = 2400;
  const arms = [
    { id: 'V0', name: '改造前语义（transition:none + width:0）—— AC1 红证', css: BEFORE_CSS },
    { id: 'V1', name: '候选（grid-template-columns + visibility 0s/延迟）', css: CANDIDATE_CSS },
    { id: 'V2', name: 'V1 + transition-behavior:allow-discrete（对照：有无作用）', css: CANDIDATE_CSS_ALLOW_DISCRETE },
    { id: 'V3', name: 'V1 但整页中间轨道写 minmax(0,0fr)（问 1fr↔0fr 是否可插值）', css: CANDIDATE_CSS_WORKSPACE_0FR },
    { id: 'V4', name: 'V1 + 时长放大到 2000ms（**仪器**：逐帧判可插值性用）', css: CANDIDATE_CSS_SLOW, settle: SLOW_WIN, window: SLOW_WIN },
    /** **V5：交付物原样**。V1–V4 都是「往 dist 里注入候选 CSS」的臂——它们的结论都挂在
     *  「候选声明 ≡ 交付物里的声明」这个假设上。V5 不注入、直接量构建产物，同一套逐轨道
     *  口径 ⇒ AC4 的读数才是对交付物本身成立的。css 写纯注释而不是空串：注入的 `<style>`
     *  必须存在，后面统一 `tag.remove()` 才不用加分支（注释不含规则 ⇒ 对层叠零影响）。 */
    { id: 'V5', name: '交付物原样（不注入，直接量 dist）', css: '/* V5：交付物原样，无注入 */' },
  ];
  const out = [];
  for (const arm of arms) {
    console.log(`── 臂 ${arm.id}：${arm.name}`);
    for (const seg of SEGMENTS) {
      // 前置必须在**注入之前**做到位：否则前置那一次切换会带着过渡，污染时序
      await reachPrecondition(p, seg.id, arm.settle ?? MS);
      const tag = await p.addStyleTag({ content: arm.css });
      const parsed = await p.evaluate(() => {
        const r = document.querySelector('.app-regions');
        const d = document.querySelector('.step-detail');
        return {
          regions: r ? getComputedStyle(r).transitionProperty : null,
          detail: d ? getComputedStyle(d).transitionProperty : null,
        };
      });
      await p.evaluate(() => {
        window.__s.list.length = 0;
        window.__s.on = true;
      });
      await seg.act(p);
      await p.waitForTimeout(Math.max(500, arm.window ?? MS));
      const raw = await p.evaluate(() => {
        window.__s.on = false;
        return window.__s.list.slice();
      });
      await tag.evaluate((el) => el.remove());

      const baseCls = raw.length ? raw[0].cls ?? '' : '';
      // 翻转帧 = 第一帧「新状态已成立」。`before` 只用来做 sanity（前一帧必须还是旧态）：
      // 若它不成立，说明采样起点就没落在切换之前，这一段读数不可用。
      const flippedAt = raw.findIndex((s) => seg.flag(s.cls ?? baseCls));
      const beforeOk = flippedAt > 0 && seg.before(raw[flippedAt - 1].cls ?? baseCls);
      const series = flippedAt < 0 ? [] : raw.slice(flippedAt);
      const uniq = (xs) => [...new Set(xs.filter((v) => v !== null && v !== undefined))];
      const gtc = uniq(series.map((s) => s.gtc));
      const dW = uniq(series.map((s) => s.dW));
      const dPad = uniq(series.map((s) => s.dPadL));
      const railW = uniq(series.map((s) => s.railW));
      const firstAt = (key, want) => {
        const i = series.findIndex((s) => s[key] === want);
        if (!series.length) return '无样本';
        if (i < 0) return `未在 ${series.length} 帧内出现`;
        return `第 ${i} 帧（+${Math.round(series[i].t - series[0].t)}ms）`;
      };
      const anims = uniq(series.flatMap((s) => s.anims));
      const tracks = trackStats(series, flippedAt > 0 ? raw[flippedAt - 1].gtc : null);
      const rec = {
        arm: arm.id, seg: seg.id, parsed, flippedAt, frames: series.length,
        gtcDistinct: gtc.length, gtc, dWDistinct: dW.length, dW, dPadDistinct: dPad.length, railWDistinct: railW.length,
        detailHiddenAt: firstAt('dVis', 'hidden'), railHiddenAt: firstAt('railVis', 'hidden'),
        wsHiddenAt: firstAt('wsVis', 'hidden'), anims, tracks,
        detailVisibleAgainAt: firstAt('dVis', 'visible'),
      };
      out.push(rec);
      // 逐轨道判定：**不再**用「整串有几个取值」当结论（那被 rail 主导，会掩盖另两条轨道的跳变）
      const tm = ['rail', 'workspace', 'inspector']
        .filter((k) => tracks[k].verdict !== '不变')
        .map((k) => `${k}:${tracks[k].verdict}(取值${tracks[k].distinct}/帧${tracks[k].samples}/最大跳${tracks[k].maxJump}px@第${tracks[k].maxJumpAt}帧)`)
        .join('  ');
      console.log(`   · ${seg.id.padEnd(14)} 翻转@样本#${flippedAt}（前态吻合=${beforeOk}）  其后 ${series.length} 帧`);
      console.log(`       轨道（逐列）：${tm || '（本段无轨道变化）'}`);
      console.log(`       整串取值 ${gtc.length} 种：${gtc.slice(0, 4).join(' → ')}${gtc.length > 4 ? ' → …' : ''}`);
      console.log(`       .step-detail width ${dW.length} 种 [${dW.slice(0, 4).join(', ')}]  padding-left ${dPad.length} 种`);
      console.log(`       .step-detail 隐于 ${rec.detailHiddenAt}；rail 隐于 ${rec.railHiddenAt}；workspace 隐于 ${rec.wsHiddenAt}`);
      console.log(`       运行中的过渡：${anims.join(' | ') || '（无）'}`);
    }
    console.log('');
  }
  results.probe = { controls: ctrl, controlOk: ctrlOk, rows, arms: out };
  await runDragProbe(p, (extra) => Object.assign(results.probe, extra));
  await runReducedMotionProbe(p, (extra) => Object.assign(results.probe, extra));
  await p.close();
  return out;
}

/** 拖宽守卫（#197）的 A/B：G1 = 候选（有 `:has(data-resizing)` 守卫）、G0 = 去掉守卫。
 *  指标是「一次 40px 拖步之后，第三列轨道要过几帧才落到目标值」——
 *  G1 应该 ≤1 帧（瞬时），G0 应该是十几帧（240ms 的滞后 = 手柄黏手）。
 *  ⚠ 前置必须用 `ensureOpen`（面板**打开**）：关着时 `.detail-resizer` 继承
 *  `visibility: hidden`，拖的是空气 —— 探针第一版踩过，读数全是 0。 */
async function runDragProbe(p, save) {
  console.log('── 拖宽守卫（#197）A/B：拖动一步之后轨道多久落到目标值');
  const arms = [
    { id: 'G1', name: '候选（含 :has(data-resizing) 守卫）', css: CANDIDATE_CSS },
    { id: 'G0', name: '去掉守卫（对照：拖拽带上过渡滞后）—— 用 GUARD_NEUTRALIZED 盖掉交付物自带的那条', css: CANDIDATE_CSS_NO_GUARD },
  ];
  const trackOf = (gtc) => parseFloat(gtc.slice(gtc.lastIndexOf(' ') + 1));
  const out = [];
  for (const arm of arms) {
    await ensureOpen(p);
    const tag = await p.addStyleTag({ content: arm.css });
    const box = await p.locator('.detail-resizer').boundingBox();
    if (!box) throw new Error('拖宽手柄没有布局盒');
    const y = box.y + Math.min(120, box.height / 2);
    const startTrack = await p.evaluate(() => {
      const g = getComputedStyle(document.querySelector('.app-regions')).gridTemplateColumns;
      return parseFloat(g.slice(g.lastIndexOf(' ') + 1));
    });
    await p.mouse.move(box.x + 3, y);
    await p.mouse.down();
    await p.waitForTimeout(120); // 让 data-resizing 落到 DOM 上
    const resizingAtStart = await p.locator('.step-detail').getAttribute('data-resizing');
    await p.evaluate(() => {
      window.__s.list.length = 0;
      window.__s.on = true;
    });
    // 向左拖 40px = 面板变宽（#197：手柄在面板左缘，向左 = 面板变宽）
    await p.mouse.move(box.x + 3 - 40, y);
    await p.waitForTimeout(400);
    const raw = await p.evaluate(() => {
      window.__s.on = false;
      return window.__s.list.slice();
    });
    await p.mouse.up();
    await tag.evaluate((el) => el.remove());

    const target = startTrack + 40;
    const settle = raw.findIndex((s) => Math.abs(trackOf(s.gtc) - target) <= 1);
    const mid = raw.filter((s, i) => (settle < 0 || i < settle) && trackOf(s.gtc) > 1 && trackOf(s.gtc) < target - 1).length;
    const resizingSeen = [...new Set(raw.map((s) => s.resizing))];
    const rec = {
      arm: arm.id, startTrack, target, settleFrames: settle, intermediateFrames: mid,
      frames: raw.length, resizingAtStart, resizingSeen,
      firstTracks: raw.slice(0, 8).map((s) => trackOf(s.gtc)),
    };
    out.push(rec);
    const usable = resizingAtStart === 'true' && raw.length > 2;
    console.log(`   · ${arm.id} ${arm.name}`);
    if (!usable) {
      console.log(`       ⚠ 读数不可用：data-resizing=${resizingAtStart}（应为 true）、样本 ${raw.length} 帧 —— 这次拖动没落在手柄上`);
      continue;
    }
    console.log(`       起始第三轨=${startTrack}px 目标=${target}px  落到目标用了 ${settle} 帧` +
      `（其中过渡中间值 ${mid} 帧）  data-resizing 取值=[${resizingSeen.join(', ')}]`);
    console.log(`       头几帧轨道=[${rec.firstTracks.join(', ')}]`);
  }
  console.log('');
  save({ drag: out });
}

/** AC5：`prefers-reduced-motion: reduce` 下新过渡必须全关。
 *  两个方向都要看：① 轨道**不再插值**；② 关闭方向的 visibility **不再被延迟**
 *  （全局块只把 duration 压成 0.01ms，`transition-delay` 它不管 —— 那正是候选 CSS 里
 *   那段 `@media` 的职责，RM0 臂就是来证明这一段的必要性的）。 */
async function runReducedMotionProbe(p, save) {
  console.log('── prefers-reduced-motion: reduce（AC5）：新过渡是否全关');
  const arms = [
    { id: 'RM1', name: '候选（含 reduce 下 transition-delay 复位）', css: CANDIDATE_CSS },
    { id: 'RM0', name: '去掉复位（对照：visibility 是否仍被推迟）', css: CANDIDATE_CSS_NO_DELAY_RESET },
  ];
  const out = [];
  await p.emulateMedia({ reducedMotion: 'reduce' });
  try {
    for (const arm of arms) {
      await ensureOpen(p);
      const tag = await p.addStyleTag({ content: arm.css });
      const media = await p.evaluate(() => ({
        reduce: matchMedia('(prefers-reduced-motion: reduce)').matches,
        dur: getComputedStyle(document.querySelector('.app-regions')).transitionDuration,
      }));
      await p.evaluate(() => {
        window.__s.list.length = 0;
        window.__s.on = true;
      });
      await clickClose(p);
      await p.waitForTimeout(Math.max(500, MS));
      const raw = await p.evaluate(() => {
        window.__s.on = false;
        return window.__s.list.slice();
      });
      const closingDelay = await p.locator('.step-detail').evaluate((el) => getComputedStyle(el).transitionDelay);
      await tag.evaluate((el) => el.remove());
      const baseCls = raw.length ? raw[0].cls ?? '' : '';
      const flippedAt = raw.findIndex((s) => isClosed(s.cls ?? baseCls));
      const series = flippedAt < 0 ? [] : raw.slice(flippedAt);
      const uniq = (xs) => [...new Set(xs.filter((v) => v != null))];
      const gtc = uniq(series.map((s) => s.gtc));
      const hiddenAt = series.findIndex((s) => s.dVis === 'hidden');
      const rec = {
        arm: arm.id, media, gtcDistinct: gtc.length, gtc, frames: series.length,
        detailHiddenAt: hiddenAt < 0 ? `未在 ${series.length} 帧内出现` : `第 ${hiddenAt} 帧`,
        detailHiddenAfterMs: hiddenAt < 0 ? null : Math.round(series[hiddenAt].t - series[0].t),
        closingDelay, runningAnims: uniq(series.flatMap((s) => s.anims)),
      };
      out.push(rec);
      console.log(`   · ${arm.id} ${arm.name}`);
      console.log(`       matchMedia.reduce=${media.reduce}  regions.transition-duration=${media.dur}`);
      console.log(`       轨道取值 ${gtc.length} 种 [${gtc.join(', ')}] → ${gtc.length <= 2 ? '✅ 过渡已关' : '❌ 仍在插值'}`);
      console.log(`       .step-detail 隐于 ${rec.detailHiddenAt}（${rec.detailHiddenAfterMs}ms）` +
        `  关闭态 detail.transition-delay=${closingDelay}`);
    }
  } finally {
    await p.emulateMedia({ reducedMotion: 'no-preference' });
  }
  console.log('');
  save({ reducedMotion: out });
}

// ── 模式 2：G4 四段 long task / 最长单帧 ──
async function runSegments(css) {
  console.log('════════ F7 G4 四段（long task 数 + 最长单帧） ════════');
  logEnv();
  const label = !css
    ? '构建产物原样（shipped）'
    : ATTR
      ? '候选 CSS + 内容摘除（**归因臂**，不进生产）'
      : BEFORE
        ? '改造前语义（transition:none + width:0）—— A/B 的「前」臂'
        : '注入候选 CSS（受控臂）';
  console.log(`  被测样式：${label}${SEG_FILTER.length ? `  仅跑段：${SEG_FILTER.join('/')}` : ''}`);
  const { p, rows } = await freshPage(INIT_METRICS);
  console.log(`  真实 DOM：.timeline-row=${rows}`);
  const tag = css ? await p.addStyleTag({ content: css }) : null;
  const segs = [];
  for (const seg of SEGMENTS) {
    const reps = [];
    for (let rep = 0; rep < REPS; rep += 1) {
      await reachPrecondition(p, seg.id);
      await p.evaluate(() => {
        window.__lt.length = 0;
        window.__frames.length = 0;
        window.__sampling = true;
      });
      await seg.act(p);
      await p.waitForTimeout(MS);
      const raw = await p.evaluate(() => {
        window.__sampling = false;
        return { lt: window.__lt.slice(), frames: window.__frames.slice(), ltErr: window.__ltErr };
      });
      reps.push({
        ltCount: raw.lt.length,
        ltMaxMs: raw.lt.length ? Math.max(...raw.lt.map((e) => e.dur)) : 0,
        ltErr: raw.ltErr,
        ...frameStats(raw.frames),
      });
      if (rep === REPS - 1) await p.screenshot({ path: join(SHOTS, `seg-${seg.id}.png`) });
    }
    const med = {};
    for (const k of ['ltCount', 'ltMaxMs', 'frameCount', 'frameMaxMs', 'dropped', 'severe']) {
      med[k] = median(reps.map((r) => r[k]));
    }
    segs.push({ id: seg.id, label: seg.label, reps, median: med });
    console.log(`   · ${seg.id.padEnd(14)} long task 数=${med.ltCount}  最长=${med.ltMaxMs}ms  ` +
      `｜rAF ${med.frameCount} 帧 掉帧(>20ms)=${med.dropped} 严重(>33ms)=${med.severe} 最长帧间隔=${med.frameMaxMs}ms`);
    console.log(`       逐轮 longtask(数/最长ms)=[${reps.map((r) => `${r.ltCount}/${r.ltMaxMs}`).join(', ')}]  ` +
      `最长帧间隔=[${reps.map((r) => r.frameMaxMs).join(', ')}]`);
  }
  if (tag) await tag.evaluate((el) => el.remove());

  // ── 正对照：本车道的 long task 探针**看得见**长任务吗？──
  // 没有这一条，「四段都是 0」跟「观察器坏了」长得一模一样（F6 的教训，本票实测撞上）。
  // ⚠ **忙等必须写在页面里**（`setTimeout` 里 spin），不能是 `page.evaluate(() => {…spin…})`：
  //   实测（本票 `_f7_lt_probe.mjs` 的四种写法）CDP 注入的那次 `Runtime.evaluate` **不产生**
  //   longtask 条目（0 条），而页面自己的定时器任务产生 1 条、时长 125ms。
  //   用错写法会得到「控制组也是 0」这种自证失败 —— 那正是这一条存在的意义。
  const ctrlReps = [];
  for (let rep = 0; rep < REPS; rep += 1) {
    await reachPrecondition(p, 'close');
    await p.evaluate(() => {
      window.__lt.length = 0;
      window.__sampling = true;
      window.__ctrlDone = false;
      setTimeout(() => {
        const t0 = performance.now();
        while (performance.now() - t0 < 120) {
          /* 故意占满主线程 */
        }
        window.__ctrlDone = true;
      }, 0);
    });
    await p.waitForFunction(() => window.__ctrlDone === true, null, { timeout: 5000 });
    await p.waitForTimeout(200);
    const raw = await p.evaluate(() => {
      window.__sampling = false;
      return window.__lt.slice();
    });
    ctrlReps.push({ ltCount: raw.length, ltMaxMs: raw.length ? Math.max(...raw.map((e) => e.dur)) : 0 });
  }
  const ctrlMed = { ltCount: median(ctrlReps.map((r) => r.ltCount)), ltMaxMs: median(ctrlReps.map((r) => r.ltMaxMs)) };
  console.log(`   · ${'正对照 busy-120ms'.padEnd(17)} long task 数=${ctrlMed.ltCount}  最长=${ctrlMed.ltMaxMs}ms  ` +
    `（探针须为 1 / ≈120ms，否则本节所有 0 都不成立）`);

  results.segments = {
    style: !css ? 'shipped' : ATTR ? 'candidate+content-removed' : 'candidate',
    injected: Boolean(css), segs: SEGMENTS.map((s) => s.id), reps: REPS, ms: MS, control: { reps: ctrlReps, median: ctrlMed },
    results: segs,
  };
  console.log('');
  await p.close();
  return segs;
}

// ── 模式 3：过渡的逐帧录屏（CDP screencast） ──
/* 为什么不用 `page.screenshot()` 连拍：单次截图在 headless 下就要几十~上百毫秒，
 * 「30/80/140ms 各截一张」根本截不到那三个时刻，得到的是"看起来在动"的假证据。
 * `Page.startScreencast` 是浏览器**逐帧推**上来的，每帧自带 `metadata.timestamp`，
 * 于是能与「动作发生时刻」对齐、如实说出过渡在它那 150/240ms 里被捕获了几帧。 */
async function runShots() {
  console.log('════════ F7 过渡逐帧录屏（CDP Page.startScreencast） ════════');
  const { p } = await freshPage(null);
  const cdp = await ctx.newCDPSession(p);
  await mkdir(join(SHOTS, 'mid'), { recursive: true });
  const captured = [];
  for (const seg of SEGMENTS) {
    await reachPrecondition(p, seg.id);
    const frames = [];
    const onFrame = (f) => {
      frames.push({ ts: f.metadata.timestamp, data: f.data });
      void cdp.send('Page.screencastFrameAck', { sessionId: f.sessionId }).catch(() => {});
    };
    cdp.on('Page.screencastFrame', onFrame);
    await cdp.send('Page.startScreencast', { format: 'png', everyNthFrame: 1, maxWidth: 1440, maxHeight: 900 });
    await p.waitForTimeout(260); // 先收几帧静止态（对照）
    const actAt = Date.now();
    await seg.act(p);
    await p.waitForTimeout(MS);
    await cdp.send('Page.stopScreencast');
    cdp.off('Page.screencastFrame', onFrame);
    // 对齐：动作时刻之后的帧 = 过渡期间
    const after = frames.filter((f) => f.ts * 1000 >= actAt - 5);
    const pickIdx = [0, Math.floor(after.length / 3), Math.floor((2 * after.length) / 3), after.length - 1].filter(
      (i, k, a) => i >= 0 && a.indexOf(i) === k,
    );
    for (const [k, i] of pickIdx.entries()) {
      if (!after[i]) continue;
      await writeFile(join(SHOTS, 'mid', `${seg.id}-${k}.png`), Buffer.from(after[i].data, 'base64'));
    }
    const spanMs = after.length >= 2 ? Math.round(after[after.length - 1].ts * 1000 - after[0].ts * 1000) : 0;
    captured.push({ seg: seg.id, framesAfterAction: after.length, spanMs, saved: pickIdx.length, offsetsMs: pickIdx.map((i) => (after[i] ? Math.round(after[i].ts * 1000 - actAt) : null)) });
    console.log(`   · ${seg.id.padEnd(14)} 动作后捕获 ${after.length} 帧，跨度 ${spanMs}ms；存档 ${pickIdx.length} 帧` +
      `（相对动作时刻 +${captured[captured.length - 1].offsetsMs.join('/')}ms）`);
  }
  results.shots = captured;
  console.log(`   → ${join(SHOTS, 'mid')}/`);
  console.log('');
  await p.close();
}

// ───────────────────────────── 跑 ─────────────────────────────

results.env = { events: EVENTS.length, steps: STEPS, mode: MODE, inject: INJECT, shipped: SHIPPED, before: BEFORE, attr: ATTR, segs: SEG_FILTER };

if (MODE === 'probe' || MODE === 'all') await runProbe();
if (MODE === 'segments' || MODE === 'all') await runSegments(ATTR ? ATTR_CSS : BEFORE ? BEFORE_CSS : INJECT ? CANDIDATE_CSS : '');
if (MODE === 'shots' || MODE === 'all') await runShots();

await browser.close();
server.close();

console.log('════════ 仪器自证（不通过则本节读数作废） ════════');
if (results.probe) {
  console.log(`① 正对照（width 100px→0，可插值）：${results.probe.controls.interpolable.distinct} 个取值 → ` +
    `${results.probe.controls.interpolable.distinct >= 3 ? '✅' : '❌'}`);
  console.log(`② 负对照（visibility，离散）：${results.probe.controls.discrete.distinct} 个取值 → ` +
    `${results.probe.controls.discrete.distinct === 2 ? '✅' : '❌'}`);
  // ⚠ 读 **V1** 臂，不要写 `arms[0]`：`arms[0]` 是 V0（红证臂，`transition:none`），
  //   拿它当「候选 CSS 被解析」的正对照会读出 `transitionProperty=none`（2026-09-19 实测撞到）。
  const v1 = results.probe.arms.find((a) => a.arm === 'V1') ?? results.probe.arms[0];
  console.log(`③ 候选 CSS 被解析（V1 臂）：regions.transitionProperty=${v1?.parsed?.regions}；` +
    `detail.transitionProperty=${v1?.parsed?.detail}`);
  console.log(`④ 采样密度：翻转后帧数=[${results.probe.arms.map((a) => `${a.seg}:${a.frames}`).join(', ')}]` +
    `（≥3 才算"数得出来"）`);
} else console.log('①②③④ 未跑（--mode 未含 probe）');
if (results.segments) {
  const all = results.segments.results.flatMap((s) => s.reps);
  const c = results.segments.control.median;
  console.log(`⑤ long task 探针在本车道可用：${all.some((r) => r.frameCount > 0) ? '✅ 采到了帧' : '❌ 一帧没采到'}` +
    `（探针错误=${all.find((r) => r.ltErr)?.ltErr ?? '无'}）`);
  console.log(`⑥ 正对照（页面内 120ms 忙等）：long task 数=${c.ltCount} 最长=${c.ltMaxMs}ms → ` +
    `${c.ltCount >= 1 && c.ltMaxMs >= 50 ? '✅ 探针看得见长任务，「0」是真的 0' : '❌ 探针没反应，本节四段的 0 全部作废'}`);
} else console.log('⑤⑥ 未跑（--mode 未含 segments）');
console.log(`截图：${SHOTS}/`);

if (JSON_OUT) {
  await writeFile(JSON_OUT, JSON.stringify({ base: BASE, dist: DIST, ...results }, null, 2));
  console.log(`JSON：${JSON_OUT}`);
}
