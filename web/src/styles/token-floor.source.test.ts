import { describe, expect, it } from 'vitest';
/* #1518/#1519 token 基线守卫：读源码而非渲染 DOM（同 StepDetail.timeline.source.test.ts
 * 的 node:fs 先例，app tsconfig 的 types 没有 node 内置模块声明）。 */

// @ts-expect-error node 内置模块没有声明
import { readdirSync, readFileSync } from 'node:fs';
// @ts-expect-error node 内置模块没有声明
import { dirname, join, relative } from 'node:path';
// @ts-expect-error node 内置模块没有声明
import { fileURLToPath } from 'node:url';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..');

/* ── 收集 src 下全部 .css / .tsx 源文 ─────────────────────────────────── */

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, name.name);
    if (name.isDirectory()) out.push(...walk(p));
    else if (/\.(css|tsx)$/.test(name.name)) out.push(p);
  }
  return out;
}

const SOURCES = walk(SRC).map((p) => ({
  rel: relative(SRC, p).replaceAll('\\', '/'),
  text: readFileSync(p, 'utf8'),
}));

const stripCssComments = (s: string) => s.replace(/\/\*[\s\S]*?\*\//g, '');

/* ── ① #1519：可读文本字号地板 12px（UI-02 PRD D3；10px 仅限全大写 micro-label）──
 * 分母 = src 下全部 css/tsx 的 font-size 声明（含 font 简写与 TSX 内联 fontSize）。
 * 例外必须逐条进 WHITELIST 并写规格依据——新违规出现即红，删例外也要说明。 */

type Exception = { file: string; selector: string; reason: string };

const FLOOR_EXCEPTIONS: Exception[] = [
  {
    file: 'styles/app.css',
    selector: '.rail-section-label',
    reason: '全大写 micro-label（text-transform: uppercase，UI-02 的 10px 例外档）',
  },
  {
    file: 'styles/app.css',
    selector: '.ask-user-qico',
    reason: 'aria-hidden 装饰图标字形（16px 圆形问号，AskUserCard.tsx aria-hidden，非可读文本）',
  },
  {
    file: 'styles/app.css',
    selector: '.ih-eyebrow',
    reason: '全大写 micro-label（Inspector 头部眉题，text-transform: uppercase，10px 例外档）',
  },
  {
    file: 'styles/app.css',
    selector: '.tl-axis',
    reason: '全大写 micro-label（时间轴刻度条，text-transform: uppercase，10px 例外档）',
  },
  {
    file: 'styles/app.css',
    selector: '.tl-axis-label',
    reason: '全大写 micro-label（继承 .tl-axis 的 uppercase，10px 例外档）',
  },
  {
    file: 'styles/app.css',
    selector: '.tl-axis-gutter::before',
    reason: '全大写 micro-label（继承 .tl-axis 的 uppercase，10px 例外档）',
  },
  {
    file: 'styles/app.css',
    selector: '.tl-axis-dur::before',
    reason: '全大写 micro-label（继承 .tl-axis 的 uppercase，10px 例外档）',
  },
  {
    file: 'styles/app.css',
    selector: '.step-detail .ov-h',
    reason: '全大写 micro-label（Overview 分节标题，text-transform: uppercase，10px 例外档）',
  },
  {
    file: 'insights/insights.css',
    selector: '.ctx-insights-kpi-label',
    reason: '全大写 micro-label（text-transform: uppercase，10px 例外档）',
  },
  {
    file: 'insights/insights.css',
    selector: '.ctx-insights-group-title',
    reason: '全大写 micro-label（text-transform: uppercase，10px 例外档）',
  },
  {
    file: 'insights/insights.css',
    selector: '.ctx-insights-fleet-col-title',
    reason: '全大写 micro-label（text-transform: uppercase，10px 例外档）',
  },
  {
    file: 'insights/insights.css',
    selector: '.ctx-insights-fleet-role',
    reason: '全大写 micro-label（text-transform: uppercase，10px 例外档）',
  },
];

const isException = (file: string, selector: string) =>
  FLOOR_EXCEPTIONS.some((e) => e.file === file && e.selector === selector);

/** 深度扫描提取叶子规则块：返回 { selector, body, line }（line = 块首行，
 *  声明行 = line + body 内换行数，由调用方折算）。
 *  @media/@supports 外层块的 body 含 '{'，不产出声明，其子块正常产出。 */
function cssRules(css: string): { selector: string; body: string; line: number }[] {
  const rules: { selector: string; body: string; line: number }[] = [];
  let depth = 0;
  let preludeStart = 0;
  let bodyStart = -1;
  for (let i = 0; i < css.length; i++) {
    const ch = css[i];
    if (ch === '{') {
      if (depth === 0) {
        bodyStart = i + 1;
      }
      depth++;
    } else if (ch === '}') {
      depth--;
      if (depth === 0 && bodyStart >= 0) {
        const body = css.slice(bodyStart, i);
        if (!body.includes('{')) {
          const selector = css.slice(preludeStart, bodyStart - 1).trim();
          const line = css.slice(0, preludeStart).split('\n').length;
          rules.push({ selector, body, line });
        }
        bodyStart = -1;
        preludeStart = i + 1;
      }
    }
  }
  return rules;
}

const lineOf = (rule: { line: number; body: string }, declIndex: number) =>
  rule.line + rule.body.slice(0, declIndex).split('\n').length - 1;

function collectFontSizes(): { file: string; selector: string; value: string; line: number }[] {
  const found: { file: string; selector: string; value: string; line: number }[] = [];
  for (const { rel, text } of SOURCES) {
    if (rel.endsWith('.css')) {
      for (const rule of cssRules(stripCssComments(text))) {
        let offset = 0;
        for (const decl of rule.body.split(';')) {
          const start = offset;
          offset += decl.length + 1;
          const m = decl.match(/^\s*(font-size|font)\s*:\s*([^;]+)$/);
          if (!m) continue;
          if (m[1] === 'font-size' || /\b1[01]px\b/.test(m[2])) {
            found.push({
              file: rel,
              selector: rule.selector,
              value: m[2].trim(),
              line: lineOf(rule, start),
            });
          }
        }
      }
    } else {
      const noComments = text
        .replace(/\/\*[\s\S]*?\*\//g, '')
        .replace(/^\s*\/\/.*$/gm, '');
      for (const m of noComments.matchAll(/fontSize:\s*['"`]?(\d+(?:\.\d+)?)px/g)) {
        found.push({ file: rel, selector: '(tsx inline)', value: `${m[1]}px`, line: 0 });
      }
    }
  }
  return found;
}

describe('#1519 可读文本字号地板（12px，UI-02 PRD D3）', () => {
  it('src 下所有 font-size ≥12px，例外仅限白名单（逐条有规格依据）', () => {
    const violations: string[] = [];
    for (const { file, selector, value, line } of collectFontSizes()) {
      const px = value.match(/^(\d+(?:\.\d+)?)px$/);
      if (!px) continue; // var()/rem/inherit 等交给 token 检查与本断言的 px 字面量域
      if (Number.parseFloat(px[1]) >= 12) continue;
      if (isException(file, selector)) continue;
      violations.push(`${file}:${line} ${selector || '(裸)'} → font-size: ${value}`);
    }
    expect(
      violations,
      `字号地板违规（10px 仅限全大写 micro-label；修复 = var(--text-xs) 或进白名单并写依据）:\n${violations.join('\n')}`,
    ).toEqual([]);
  });

  it('字号 token 本身不低于地板（--text-* 定义 ≥12px）', () => {
    for (const { rel, text } of SOURCES) {
      if (!rel.endsWith('.css')) continue;
      for (const m of stripCssComments(text).matchAll(/--text-[a-z0-9-]+\s*:\s*(\d+(?:\.\d+)?)px/g)) {
        expect(
          Number.parseFloat(m[1]),
          `${rel} --text-* token ${m[1]}px 低于 12px 地板`,
        ).toBeGreaterThanOrEqual(12);
      }
    }
  });
});

/* ── ② #1518：var() 引用的 custom property 必须有定义（不允许 fallback 掩盖）──
 * 定义域 = css 的 `--name:` 声明 + tsx style 对象的 `'--name':` 键。
 * 作用域可达性（如 .step-detail 的 --border 局部别名）是设计决策，逐例登记在
 * 票据交付表里，这里只守"名字无定义"这一类（#1518 的 bug 面）。 */

const VAR_WHITELIST_PREFIXES = [/^--radix-/];

const collectVarNames = (regex: RegExp, pick: (m: RegExpMatchArray) => string) => {  const names = new Set<string>();
  for (const { text } of SOURCES) {
    const noComments = text
      .replace(/\/\*[\s\S]*?\*\//g, '')
      .replace(/^\s*\/\/.*$/gm, '');
    for (const m of noComments.matchAll(regex)) names.add(pick(m));
  }
  return names;
};

describe('#1518 var() 引用必须有定义（不允许无定义/fallback 掩盖）', () => {
  it('每个 var(--x) 的 --x 都在 css/tsx 中有定义或有登记豁免', () => {
    const defined = collectVarNames(
      /(^|[\s;{'"`])--([a-zA-Z0-9-]+)['"]?\s*:/g,
      (m) => m[2],
    );
    const used = collectVarNames(/var\(\s*--([a-zA-Z0-9-]+)/g, (m) => m[1]);
    const undefinedUses: string[] = [];
    for (const name of [...used].sort()) {
      if (defined.has(name)) continue;
      if (VAR_WHITELIST_PREFIXES.some((re) => re.test(`--${name}`))) continue;
      undefinedUses.push(`--${name}`);
    }
    expect(
      undefinedUses,
      '存在无定义的 CSS 变量引用（补语义 token 或改用现有 token；不允许 fallback 掩盖）',
    ).toEqual([]);
  });
});
