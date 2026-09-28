/** F6(#277) ——「呼吸辉光」`pulse-glow` 的渲染成本实测（真机 Chromium + **生产构建 CSS**）。
 *
 *  跑法（先 `npm run build`，脚本读 `web/dist`）：
 *    cd web
 *    node scripts/perf-pulse-cost.mjs                      # 8 臂 × 3 轮（默认）
 *    node scripts/perf-pulse-cost.mjs --reps 5 --only A1,B1,C,C2
 *
 *  为什么这样测（票面 §第 1 步要求「先测量」，G3 要求留可复核数字）：
 *  - 本票测的是 **`box-shadow` 动画的渲染成本**，这是 CSS 的固有属性，与「数据从哪来」无关。
 *    真机「真实 run 的 thinking/tool 相位」当前**不可得**：模型供应商账户冻结
 *    （`HTTP 400 {"code":"billing"}`，证据见 `docs/PERF_BASELINE.md` F1 节），
 *    所以这里用**受控臂**：真 chromium + `web/dist` 里那份**生产 CSS** + 与
 *    `TopBar.tsx:122` 同形的胶囊 DOM，只把变量（动画开/关、面积、个数）单独掀开。
 *  - A/B 的 B 臂用 CDP `Emulation.setEmulatedMedia` 打开
 *    `prefers-reduced-motion: reduce` —— 走的是**真机制**（`index.css:368-376` 的全局块，
 *    `animation-duration: 0.01ms !important` / `iteration-count: 1 !important`），
 *    不是这里另写一条 `animation: none` 假装关掉。
 *  - **仪器自证**（防「测不出 ⇒ 误判可忽略」的假绿），三条都要成立：
 *    ① B 臂实测到「运行中的 animation 数 = 0」——否则动画没被关掉，A/B 不成立；
 *    ② 放大臂 `A40`（40 个胶囊）的指标必须**高于** `A1`（1 个）——证明指标随被测变量响应；
 *    ③ 正对照 `C` / `C2`（同机制铺满视口 / 一个确定 paint-bound 的机制）必须显著高于地板
 *       —— 证明这套 trace 在这台机器/这个 headless 口子上**看得见 paint**。
 *       哪条不成立就在报告里明说，而不是把 0 当成「零成本」。
 *  - 每臂 **多轮取中位数**：`PERF_BASELINE.md` F2 节已写明「单次采样不足以支撑任何前后对照」。
 *
 *  口径（`docs/PERF_BASELINE.md` §2.1）：**long task 数** + **最长单帧**（不是 FPS 均值），
 *  外加票面要求的 **paint 时间占比**（CDP `Tracing`，与 Chrome DevTools Performance 面板同源）。
 *  **只报数字、不做阈值断言**（同 `perf-longtask-live.mjs` / `f1-cost-probe`）。
 *
 *  每一臂**先测后 trace**（两趟）：long task 那趟不挂 tracer，免得 tracer 自身开销污染头条数字。
 */

import { createServer } from 'node:http';
import { readFile, readdir, mkdir, writeFile } from 'node:fs/promises';
import { extname, join, resolve, sep } from 'node:path';
import { chromium } from '@playwright/test';

// 代理会让对 127.0.0.1 的请求发不出去，先清掉（同 perf-longtask-live.mjs）。
for (const k of ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy']) {
  delete process.env[k];
}

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i !== -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const DIST = resolve('dist');
const MEASURE_MS = Number(arg('ms', '3000'));
const WARMUP_MS = Number(arg('warmup', '1200'));
const REPS = Number(arg('reps', '3'));
const SHOTS = arg('shots', join('gui-test-screenshots', 'perf-pulse-cost'));
const JSON_OUT = arg('json', '');
const HEADED = process.argv.includes('--headed');
const ONLY = arg('only', '').split(',').map((s) => s.trim()).filter(Boolean);

// ─────────────────────────── 受控臂 ───────────────────────────

/** 与 `TopBar.tsx:122-132` 同形的胶囊：`.run-pulse` + 状态类 + 14px lucide 图标 + 中文 label。
 *  图标用 `Loader2` 的几何近似（一段圆弧）——影响的是 14×14 的占位，不影响胶囊外框尺寸。 */
const pulseHtml = (cls, label) =>
  `<span class="run-pulse ${cls}">` +
  '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
  'stroke-width="2" stroke-linecap="round" aria-hidden="true">' +
  '<path d="M12 3a9 9 0 1 0 9 9"/></svg>' +
  `${label}<span class="num"> · 12s</span></span>`;

const ARMS = [
  { id: 'Z0', name: '空舞台（零动画）＝ 仪器噪声地板', reduced: false, stage: () => '' },
  { id: 'A1', name: 'pulse-thinking ×1（run 里的真实数量）· 动画开', reduced: false, stage: () => pulseHtml('pulse-thinking', '思考中') },
  { id: 'B1', name: 'pulse-thinking ×1 ＋ prefers-reduced-motion:reduce（真机制关闭）', reduced: true, stage: () => pulseHtml('pulse-thinking', '思考中') },
  { id: 'A2', name: 'pulse-tool ×1（run 里的真实数量）· 动画开', reduced: false, stage: () => pulseHtml('pulse-tool', '执行工具') },
  { id: 'B2', name: 'pulse-tool ×1 ＋ prefers-reduced-motion:reduce', reduced: true, stage: () => pulseHtml('pulse-tool', '执行工具') },
  { id: 'A40', name: 'pulse-thinking ×40（放大臂：指标必须随个数响应）', reduced: false, stage: () => Array.from({ length: 40 }, () => pulseHtml('pulse-thinking', '思考中')).join('') },
  { id: 'C', name: '正对照①：**同一套** keyframes 铺满 1440×900（指标须随面积响应）', reduced: false, control: 'glow', stage: () => '' },
  { id: 'C2', name: '正对照②：`filter: blur()` 铺满 1440×900（确定 paint-bound 的机制）', reduced: false, control: 'blur', stage: () => '' },
].filter((a) => !ONLY.length || ONLY.includes(a.id));

// ─────────────────────────── 静态服务（dist + 一个无 JS 舞台页） ───────────────────────────

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.woff2': 'font/woff2',
  '.woff': 'font/woff',
};

const HARNESS_PATH = '/__perf_harness__.html';

async function buildHarness() {
  const assets = await readdir(join(DIST, 'assets'));
  const css = assets.filter((f) => /^index-.*\.css$/.test(f));
  if (css.length !== 1) {
    throw new Error(`dist/assets 里 index-*.css 不是恰好 1 个（${css.join(', ')}）——先 npm run build`);
  }
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>perf-pulse-cost</title>
<link rel="stylesheet" href="/assets/${css[0]}">
<style>
  html, body { margin: 0; height: 100%; overflow: hidden; background: #0b0b0e; color: #e8e8ee;
    font-family: system-ui, -apple-system, 'Segoe UI', sans-serif; }
  /* 舞台只给位置：所有 .run-pulse / 动画规则一律来自上面那份**生产 CSS** */
  #stage { position: absolute; left: 40px; top: 40px; display: flex; flex-direction: column;
    gap: 10px; align-items: flex-start; }
  /* 正对照①：刻意**复用同一套** pulse-glow keyframes（就在上面那份生产 CSS 里），只换承载面积 */
  #control-glow { position: absolute; inset: 0; border-radius: 0; background: rgba(124, 140, 255, 0.10);
    color: #7c8cff; animation: pulse-glow 1.4s ease-in-out infinite; }
  /* 正对照②：一个**确定走 paint**的机制（filter 每帧重算 → 必须重新光栅化） */
  #control-blur { position: absolute; inset: 0; background:
    repeating-linear-gradient(45deg, #2a2f6a 0 24px, #6a2a4a 24px 48px);
    animation: perf-blur 0.6s ease-in-out infinite; }
  @keyframes perf-blur { 0%,100% { filter: blur(0px); } 50% { filter: blur(24px); } }
</style></head>
<body><div id="control-glow" hidden></div><div id="control-blur" hidden></div>
<div id="stage"></div></body></html>`;
}

async function serveDist(harness) {
  const server = createServer(async (req, res) => {
    try {
      const url = new URL(req.url ?? '/', 'http://127.0.0.1');
      if (url.pathname === HARNESS_PATH) {
        res.writeHead(200, { 'content-type': MIME['.html'] });
        res.end(harness);
        return;
      }
      if (url.pathname === '/') {
        res.writeHead(302, { location: HARNESS_PATH });
        res.end();
        return;
      }
      const file = join(DIST, decodeURIComponent(url.pathname));
      // 前缀判断必须带分隔符：否则同级目录 `dist2/` 会被放通（低危，但没理由留着）
      if (!resolve(file).startsWith(DIST + sep)) {
        res.writeHead(403);
        res.end();
        return;
      }
      const body = await readFile(file);
      res.writeHead(200, { 'content-type': MIME[extname(file)] ?? 'application/octet-stream' });
      res.end(body);
    } catch {
      res.writeHead(404);
      res.end();
    }
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  return { server, port: server.address().port };
}

// ─────────────────────────── 采集器 ───────────────────────────

/** 页面侧探针：`longtask` 观察器（阈值 50ms，与 DevTools 同源）+ rAF 帧间隔采样器。
 *  rAF 只做**掉帧**判定：帧间隔被 vsync 夹在 ~16.7ms，能超过说明真的掉了帧。 */
const INIT = () => {
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

/** 「这一臂到底有没有在做动画」自检 —— 没有它，B 臂可能只是**没关掉**动画，
 *  于是 A−B ≈ 0 会被误读成「动画零成本」。(防假绿①) */
const PROBE = () => {
  const el = document.querySelector('#stage .run-pulse');
  const seen = document.querySelector('#control-glow:not([hidden]), #control-blur:not([hidden])');
  const target = el ?? seen;
  const anims = document.getAnimations();
  const cs = target ? getComputedStyle(target) : null;
  const rect = target ? target.getBoundingClientRect() : null;
  return {
    pulseCount: document.querySelectorAll('#stage .run-pulse').length,
    runningCount: anims.filter((a) => a.playState === 'running').length,
    runningNames: [...new Set(anims.filter((a) => a.playState === 'running')
      .map((a) => a.animationName ?? '?'))],
    totalAnims: anims.length,
    animationName: cs?.animationName ?? null,
    animationDuration: cs?.animationDuration ?? null,
    animationIterationCount: cs?.animationIterationCount ?? null,
    padding: cs ? `${cs.paddingTop} ${cs.paddingRight}` : null,
    boxShadow: cs?.boxShadow ?? null,
    rect: rect ? { w: +rect.width.toFixed(1), h: +rect.height.toFixed(1) } : null,
    ltErr: window.__ltErr,
  };
};

function parseTrace(events) {
  const X = (n) => events.filter((e) => e.name === n && e.ph === 'X' && typeof e.dur === 'number');
  const ms = (n) => X(n).reduce((s, e) => s + e.dur, 0) / 1000;
  const runs = X('RunTask');
  const durs = runs.map((e) => e.dur / 1000);
  const win = runs.length
    ? (Math.max(...runs.map((e) => e.ts + e.dur)) - Math.min(...runs.map((e) => e.ts))) / 1000
    : 0;
  const paint = ms('Paint');
  return {
    traceWindowMs: +win.toFixed(1),
    runTaskCount: runs.length,
    runTaskMaxMs: +(durs.length ? Math.max(...durs) : 0).toFixed(1),
    runTaskOver16Ms: durs.filter((d) => d >= 16.7).length,
    runTaskLong: durs.filter((d) => d >= 50).length,
    paintMs: +paint.toFixed(1),
    paintRatioPct: win > 0 ? +((paint / win) * 100).toFixed(2) : 0,
    rasterMs: +ms('RasterTask').toFixed(1),
    layoutMs: +ms('Layout').toFixed(1),
    styleMs: +ms('UpdateLayoutTree').toFixed(1),
    compositeMs: +ms('CompositeLayers').toFixed(1),
  };
}

function framesStats(frames) {
  if (frames.length < 3) return { frameCount: frames.length, frameMaxMs: null, droppedFrames: null, severeDrops: null };
  const d = [];
  for (let i = 1; i < frames.length; i += 1) d.push(frames[i] - frames[i - 1]);
  return {
    frameCount: frames.length,
    frameMaxMs: +Math.max(...d).toFixed(1),
    // 帧间隔被 vsync 夹在 ~16.7ms：>20ms = 掉了一帧，>33ms = 掉了两帧以上（可感卡顿）
    droppedFrames: d.filter((x) => x > 20).length,
    severeDrops: d.filter((x) => x > 33).length,
  };
}

const NUMERIC = ['ltCount', 'ltMaxMs', 'frameCount', 'frameMaxMs', 'droppedFrames', 'severeDrops',
  'traceWindowMs', 'runTaskCount', 'runTaskMaxMs', 'runTaskOver16Ms', 'runTaskLong', 'paintMs',
  'paintRatioPct', 'rasterMs', 'layoutMs', 'styleMs', 'compositeMs'];

const median = (xs) => {
  const s = [...xs].sort((a, b) => a - b);
  if (!s.length) return null;
  // 偶数个取两个中位的**平均**（只取上/下中位会系统性偏高/偏低，`--reps` 为偶数时会偏）
  const m = s.length % 2 ? s[(s.length - 1) / 2] : (s[s.length / 2 - 1] + s[s.length / 2]) / 2;
  return +m.toFixed(2);
};

// ─────────────────────────── 主流程 ───────────────────────────

const harness = await buildHarness();
const { server, port } = await serveDist(harness);
const BASE = `http://127.0.0.1:${port}`;
await mkdir(SHOTS, { recursive: true });

const browser = await chromium.launch({ headless: !HEADED });
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
await ctx.addInitScript(INIT);
const page = await ctx.newPage();
const cdp = await ctx.newCDPSession(page);

console.log('F6 pulse-glow 渲染成本实测');
console.log(`  base=${BASE}  dist=${DIST}  headed=${HEADED}`);
console.log(`  每臂 ${REPS} 轮 × (warmup=${WARMUP_MS}ms + 测量 ${MEASURE_MS}ms ×2 趟：先 longtask/rAF，再 CDP trace)`);
console.log('');

const results = {};
for (const arm of ARMS) {
  results[arm.id] = { name: arm.name, reps: [] };
  for (let rep = 0; rep < REPS; rep += 1) {
    await page.emulateMedia({ reducedMotion: arm.reduced ? 'reduce' : 'no-preference' });
    await page.goto(`${BASE}${HARNESS_PATH}`, { waitUntil: 'load' });
    await page.evaluate(
      ([html, control]) => {
        document.getElementById('stage').innerHTML = html;
        document.getElementById('control-glow').hidden = control !== 'glow';
        document.getElementById('control-blur').hidden = control !== 'blur';
      },
      [arm.stage(), arm.control ?? ''],
    );
    await page.waitForTimeout(WARMUP_MS);
    const probe = await page.evaluate(PROBE);

    // ── 第 1 趟：long task + rAF 帧间隔（不挂 tracer） ──
    await page.evaluate(() => {
      window.__lt.length = 0;
      window.__frames.length = 0;
      window.__sampling = true;
    });
    await page.waitForTimeout(MEASURE_MS);
    const raw = await page.evaluate(() => {
      window.__sampling = false;
      return { lt: window.__lt.slice(), frames: window.__frames.slice() };
    });
    const ltMax = raw.lt.length ? Math.max(...raw.lt.map((e) => e.dur)) : 0;
    const fs = framesStats(raw.frames);

    // ── 第 2 趟：CDP trace（paint 占比） ──
    const events = [];
    const onData = ({ value }) => events.push(...value);
    cdp.on('Tracing.dataCollected', onData);
    await cdp.send('Tracing.start', {
      categories: 'devtools.timeline,disabled-by-default-devtools.timeline,blink.user_timing,cc,viz',
      transferMode: 'ReportEvents',
    });
    const doneTrace = new Promise((r) => cdp.once('Tracing.tracingComplete', r));
    await page.waitForTimeout(MEASURE_MS);
    await cdp.send('Tracing.end');
    await doneTrace;
    cdp.off('Tracing.dataCollected', onData);

    const rec = {
      rep,
      ltCount: raw.lt.length,
      ltMaxMs: ltMax,
      ...fs,
      ...parseTrace(events),
      probe,
    };
    results[arm.id].reps.push(rec);
    if (rep === REPS - 1) await page.screenshot({ path: join(SHOTS, `${arm.id}.png`) });
  }
}

await browser.close();
server.close();

// ─────────────────────────── 汇总 ───────────────────────────

for (const arm of ARMS) {
  const r = results[arm.id];
  const p = r.reps[r.reps.length - 1].probe;
  const agg = {};
  for (const m of NUMERIC) agg[m] = median(r.reps.map((x) => x[m]).filter((v) => typeof v === 'number'));
  r.median = agg;
  console.log(`── ${arm.id}  ${r.name}`);
  console.log(`   动画：name=${p.animationName} dur=${p.animationDuration} iter=${p.animationIterationCount}` +
    `  running=${p.runningCount}[${p.runningNames.join('+') || '—'}]  total=${p.totalAnims}`);
  console.log(`   盒：${p.rect ? `${p.rect.w}×${p.rect.h}` : '—'} padding=${p.padding} ` +
    `pulse×${p.pulseCount}  longtask 探针错误=${p.ltErr ?? '无'}`);
  console.log(`   中位（${REPS} 轮）：longtask 数=${agg.ltCount} 最长 longtask=${agg.ltMaxMs}ms ` +
    `｜rAF ${agg.frameCount} 帧 掉帧(>20ms)=${agg.droppedFrames} 严重(>33ms)=${agg.severeDrops} ` +
    `最长帧间隔=${agg.frameMaxMs}ms`);
  console.log(`   trace 中位：窗口=${agg.traceWindowMs}ms RunTask=${agg.runTaskCount} 最长=${agg.runTaskMaxMs}ms ` +
    `>16.7ms=${agg.runTaskOver16Ms} ≥50ms=${agg.runTaskLong} ｜paint=${agg.paintMs}ms(${agg.paintRatioPct}%) ` +
    `raster=${agg.rasterMs} style=${agg.styleMs} layout=${agg.layoutMs} composite=${agg.compositeMs}`);
  console.log(`   逐轮 longest RunTask(ms)=[${r.reps.map((x) => x.runTaskMaxMs).join(', ')}]  ` +
    `paint(ms)=[${r.reps.map((x) => x.paintMs).join(', ')}]  ltCount=[${r.reps.map((x) => x.ltCount).join(', ')}]  ` +
    `掉帧=[${r.reps.map((x) => x.droppedFrames).join(', ')}]`);
  console.log('');
}

const M = (id, m) => results[id]?.median?.[m];
const diff = (a, b, m) => {
  const A = M(a, m);
  const B = M(b, m);
  if (typeof A !== 'number' || typeof B !== 'number') return null;
  return `  ${a} − ${b}  ${m} = ${+(A - B).toFixed(2)}   （${A} vs ${B}）`;
};
console.log('════════════ A/B 差值（A＝动画开，B＝同一 DOM 在 reduced-motion 下；中位数口径） ════════════');
for (const [a, b] of [['A1', 'B1'], ['A2', 'B2']]) {
  if (!results[a] || !results[b]) {
    console.log(`${a}/${b}：未在本轮臂集合中`);
    continue;
  }
  console.log(`${a}/${b}：`);
  for (const m of ['ltCount', 'ltMaxMs', 'frameMaxMs', 'droppedFrames', 'severeDrops', 'runTaskCount',
    'runTaskMaxMs', 'runTaskOver16Ms', 'paintMs', 'paintRatioPct', 'styleMs', 'rasterMs']) {
    const s = diff(a, b, m);
    if (s) console.log(s);
  }
}
console.log('');
console.log('════════════ 仪器自证（三条都要成立，否则本报告作废） ════════════');
const has = (id) => Boolean(results[id]);
const running = (id) => results[id]?.reps?.[0]?.probe?.runningCount;
if (has('A1') && has('B1')) {
  console.log(`① B 臂动画确实被关掉：running animation 数 A1=${running('A1')} / B1=${running('B1')} ` +
    `（B 应为 0）→ ${running('B1') === 0 ? '✅' : '❌'}`);
} else console.log('① 未在本轮臂集合中（需 A1 + B1）');
if (has('A1') && has('A40')) {
  const ok = M('A40', 'styleMs') > M('A1', 'styleMs') && M('A40', 'runTaskCount') > M('A1', 'runTaskCount');
  console.log(`② 指标随被测变量响应：RunTask 数 A1=${M('A1', 'runTaskCount')} vs A40=${M('A40', 'runTaskCount')}；` +
    `style A1=${M('A1', 'styleMs')}ms vs A40=${M('A40', 'styleMs')}ms；` +
    `paint A1=${M('A1', 'paintMs')}ms vs A40=${M('A40', 'paintMs')}ms（A40 应更高）→ ${ok ? '✅' : '❌'}`);
} else console.log('② 未在本轮臂集合中（需 A1 + A40）');
if (has('Z0') && has('C') && has('C2')) {
  console.log('③ trace 的两个渲染列（paint / raster）在本车道**是否可用**：');
  console.log(`   地板 Z0：paint=${M('Z0', 'paintMs')}ms raster=${M('Z0', 'rasterMs')}ms`);
  console.log(`   正对照① C（同 keyframes 铺满 1440×900）：paint=${M('C', 'paintMs')}ms raster=${M('C', 'rasterMs')}ms ` +
    `最长 RunTask=${M('C', 'runTaskMaxMs')}ms >16.7ms=${M('C', 'runTaskOver16Ms')} 掉帧=${M('C', 'droppedFrames')}`);
  console.log(`   正对照② C2（filter:blur 铺满视口，确定 paint-bound）：paint=${M('C2', 'paintMs')}ms ` +
    `raster=${M('C2', 'rasterMs')}ms 最长 RunTask=${M('C2', 'runTaskMaxMs')}ms ` +
    `>16.7ms=${M('C2', 'runTaskOver16Ms')} 掉帧=${M('C2', 'droppedFrames')}`);

  const paintUsable = M('C2', 'paintMs') > 0;
  console.log(`   → C2 明显卡顿（掉帧 ${M('C2', 'droppedFrames')}、最长帧间隔 ${M('C2', 'frameMaxMs')}ms）时 ` +
    `paint 列仍为 ${M('C2', 'paintMs')}ms ⇒ paint/raster 列` +
    `${paintUsable ? '可用' : '**在本车道不可用**（本车道不做真光栅化），只能当参考、不得当证据'}`);
  const jankUsable = M('C2', 'droppedFrames') > 0 && M('C2', 'runTaskOver16Ms') > 0;
  console.log(`   → 「最长帧间隔 / 掉帧 / trace 最长 RunTask」列：` +
    `${jankUsable ? '✅ 能响应（C2 与地板拉开）' : '❌ 无响应，本报告作废'}`);
} else console.log('③ 未在本轮臂集合中（需 Z0 + C + C2）');
console.log('');
console.log(`截图：${SHOTS}/`);

if (JSON_OUT) {
  await writeFile(JSON_OUT, JSON.stringify({ base: BASE, dist: DIST, measureMs: MEASURE_MS, reps: REPS, results }, null, 2));
  console.log(`JSON：${JSON_OUT}`);
}
