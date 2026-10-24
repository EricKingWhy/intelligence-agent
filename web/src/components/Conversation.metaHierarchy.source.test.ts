import { describe, expect, it } from 'vitest';
/* #1447（F-05）：中间栏元信息三级层级守卫。
 *
 * 背景：产品把元信息统一到 12px（`--text-xs`）后，与正文 13px 只差 1px，
 * 「弱化灰字」的层级只能靠颜色深浅 + 字重拉开（用户 2026-10-21 22:05 裁决）。
 * 口径：正文 primary > 关键 secondary/600 > 中间 secondary/500 > 次要 tertiary/400。
 * 硬约束：本票只动 color / font-weight；font-size 一律 `var(--text-xs)`（含 compact
 * 密度变体）；字重一律显式声明（不靠「未声明 ⇒ 400」推断）；
 * token 值不动（tertiary 已是 AA 地板，禁止再造更浅的颜色）。
 * 审查修复轮：药丸 hover 上档到 primary（默认态已占 secondary，hover 不再同值），
 * 该变更是有意修复，不是「hover 语义不变」。
 *
 * jsdom 不加载 CSS，行为测试拿不到计算样式，故直接对样式源码设守卫；
 * 形态照 `StepDetail.timeline.source.test.ts`（CSS 用 node:fs，不走 `?raw`）。 */
// @ts-expect-error node 内置模块没有声明
import { readFileSync } from 'node:fs';
// @ts-expect-error node 内置模块没有声明
import { dirname, join } from 'node:path';
// @ts-expect-error node 内置模块没有声明
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
/* Windows checkout 是 CRLF：ruleBlock 的正则以 \n 匹配多行选择器，读入时先归一。 */
const appCss = readFileSync(join(here, '../styles/app.css'), 'utf8').replace(/\r\n/g, '\n');
const indexCss = readFileSync(join(here, '../index.css'), 'utf8');

/** 取一条 CSS 规则的声明块；选择器必须整行开头，避免命中注释 / 状态变体里的同名文本。 */
function ruleBlock(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = appCss.match(new RegExp(`^${escaped}\\s*\\{([^}]*)\\}`, 'm'));
  expect(match, `missing CSS rule ${selector}`).not.toBeNull();
  return match![1];
}

/** 读声明值；未声明返回 null。 */
function decl(body: string, prop: string): string | null {
  const match = body.match(new RegExp(`(?:^|[;\\s])${prop}:\\s*([^;]+);`));
  return match ? match[1].trim() : null;
}

/** 字重必须显式声明（#1447 审查修复：不再靠「未声明 ⇒ 400」的推断）。 */
function weightOf(selector: string): number {
  const value = decl(ruleBlock(selector), 'font-weight');
  expect(value, `${selector} 必须显式声明 font-weight`).not.toBeNull();
  return Number(value);
}

/** 主题 token 块：暗色 `:root` / 亮色 `:root[data-theme='light']`。 */
function tokenBlock(header: string): string {
  const start = indexCss.indexOf(header);
  expect(start, `missing token block ${header}`).toBeGreaterThan(-1);
  const open = indexCss.indexOf('{', start);
  const end = indexCss.indexOf('\n}', open);
  expect(end, `unterminated token block ${header}`).toBeGreaterThan(open);
  return indexCss.slice(open + 1, end);
}

function textToken(block: string, name: string): { rgb: string; alpha: number } {
  const match = block.match(
    new RegExp(`${name}:\\s*rgba\\(\\s*(\\d+),\\s*(\\d+),\\s*(\\d+),\\s*([\\d.]+)\\s*\\)`),
  );
  expect(match, `missing ${name} rgba()`).not.toBeNull();
  return { rgb: `${match![1]},${match![2]},${match![3]}`, alpha: Number(match![4]) };
}

const dark = tokenBlock('\n:root {');
const light = tokenBlock(":root[data-theme='light'] {");

/** 档位映射（测试即契约；改档位必须同时改这里与 app.css）。 */
const TIERS = {
  /* 关键：与正文拉开第一档 —— 更深的 secondary + 600。 */
  key: {
    color: 'var(--text-secondary)',
    weight: 600,
    selectors: ['.tm-n', '.act-name', '.act-group-head b'],
  },
  /* 中间：secondary + 500（二选一口径里固定这一支）。
   * 颜色与关键同档、字重降一档 ⇒ 至少一维严格居中，另一维不倒挂。 */
  middle: {
    color: 'var(--text-secondary)',
    weight: 500,
    selectors: ['.tm-l', '.turn-nav-pill'],
  },
  /* 次要：维持 tertiary + 400（AA 地板，不再往浅造色）。 */
  minor: {
    color: 'var(--text-tertiary)',
    weight: 400,
    selectors: ['.tm-d', '.act-args', '.act-duration', '.act-group-kinds', '.act-group-total'],
  },
} as const;

describe('#1447 中间栏元信息层级（color / font-weight）', () => {
  it('关键档 = secondary + 600；次要档 = tertiary + 400，两主题 token 均不倒挂', () => {
    for (const selector of TIERS.key.selectors) {
      expect(decl(ruleBlock(selector), 'color'), `${selector} color`).toBe(TIERS.key.color);
      expect(weightOf(selector), `${selector} weight`).toBe(TIERS.key.weight);
    }
    for (const selector of TIERS.minor.selectors) {
      expect(decl(ruleBlock(selector), 'color'), `${selector} color`).toBe(TIERS.minor.color);
      expect(weightOf(selector), `${selector} weight`).toBe(TIERS.minor.weight);
    }

    /* token 层级：两主题都必须 secondary 比 tertiary 深，且同底色（alpha 可比）。 */
    for (const [theme, block] of [
      ['dark', dark],
      ['light', light],
    ] as const) {
      const secondary = textToken(block, '--text-secondary');
      const tertiary = textToken(block, '--text-tertiary');
      expect(secondary.rgb, `${theme} token rgb`).toBe(tertiary.rgb);
      expect(secondary.alpha, `${theme} secondary > tertiary`).toBeGreaterThan(tertiary.alpha);
    }
  });

  it('中间档 = secondary + 500，字重严格居中、颜色不倒挂', () => {
    for (const selector of TIERS.middle.selectors) {
      expect(decl(ruleBlock(selector), 'color'), `${selector} color`).toBe(TIERS.middle.color);
      expect(weightOf(selector), `${selector} weight`).toBe(TIERS.middle.weight);
    }

    const keyWeight = weightOf(TIERS.key.selectors[0]);
    const middleWeight = weightOf(TIERS.middle.selectors[0]);
    const minorWeight = weightOf(TIERS.minor.selectors[0]);
    expect(keyWeight, '600 > 500 > 400').toBeGreaterThan(middleWeight);
    expect(middleWeight).toBeGreaterThan(minorWeight);

    /* 颜色维度：中间与关键同档（不倒挂）——与次要的差异已由 token 名精确锁定。 */
    const middleColor = decl(ruleBlock(TIERS.middle.selectors[0]), 'color');
    expect(middleColor).toBe(decl(ruleBlock(TIERS.key.selectors[0]), 'color'));
  });

  it('`.tm-n` 与 `.tm-l` 不再倒挂：数字与标签同色，数字更重', () => {
    /* 精确色/字重已由前两条按档位表锁定，这里只钉这一对元素的相对关系。 */
    expect(decl(ruleBlock('.tm-n'), 'color')).toBe(decl(ruleBlock('.tm-l'), 'color'));
    expect(weightOf('.tm-n')).toBeGreaterThan(weightOf('.tm-l'));
  });

  it('12px 地板：全部层级选择器 font-size = var(--text-xs)，token 本身是 12px', () => {
    expect(indexCss).toMatch(/--text-xs:\s*12px;/);
    const selectors = [
      ...TIERS.key.selectors,
      ...TIERS.middle.selectors,
      ...TIERS.minor.selectors,
    ];
    for (const selector of selectors) {
      expect(decl(ruleBlock(selector), 'font-size'), `${selector} font-size`).toBe(
        'var(--text-xs)',
      );
    }

    /* compact 密度变体：`.act-name` 曾是漏网的 11px，同样必须守 12px 地板
       （同组 `.act-duration` / `.act-args` 本就是 `--text-xs`）。 */
    expect(
      decl(ruleBlock("[data-density='compact'] .act-name"), 'font-size'),
      'compact .act-name font-size',
    ).toBe('var(--text-xs)');
  });

  it('hover 上档到 primary；is-on / is-off / 状态色语义不变', () => {
    /* #1447 审查修复（有意变更，非「语义不变」）：默认态已提到 secondary，
       hover 若仍写 secondary 则颜色维度归零（浅色主题只剩 ≈1.09:1 底色差）
       ⇒ hover 上档到 primary，并断言它与默认态确实不同。 */
    const hover = ruleBlock('.turn-nav-pill:hover');
    expect(decl(hover, 'background')).toBe('var(--surface-2)');
    expect(decl(hover, 'color')).toBe('var(--text-primary)');
    expect(decl(hover, 'color')).not.toBe(decl(ruleBlock('.turn-nav-pill'), 'color'));

    const off = ruleBlock('.turn-nav-pill.is-off');
    expect(decl(off, 'color')).toBe('var(--text-tertiary)');
    expect(decl(off, 'opacity')).toBe('0.5');
    expect(decl(off, 'text-decoration')).toBe('line-through');

    const on = ruleBlock('.turn-nav-pill.is-on');
    expect(decl(on, 'background')).toBe('var(--accent-soft)');
    expect(decl(on, 'color')).toBe(
      'color-mix(in srgb, var(--accent) 45%, var(--text-primary))',
    );
    expect(decl(on, 'font-weight')).toBe('600');

    const failName = ruleBlock('.act-card.is-fail .act-name');
    expect(decl(failName, 'color')).toBe(
      'color-mix(in srgb, var(--danger) 45%, var(--text-primary))',
    );
    const failArgs = ruleBlock('.act-card.is-fail .act-args');
    expect(decl(failArgs, 'color')).toBe(
      'color-mix(in srgb, var(--danger) 45%, var(--text-primary))',
    );
    expect(decl(failArgs, 'opacity')).toBe('0.82');

    expect(decl(ruleBlock(".act-node[aria-expanded='true'] .act-name"), 'color')).toBe(
      'var(--accent-text)',
    );
    expect(decl(ruleBlock('.turn-mark.is-hit .tm-n'), 'color')).toBe('var(--accent-text)');
    expect(decl(ruleBlock('.tm-anchor.is-done'), 'color')).toBe('var(--success)');
    expect(decl(ruleBlock('.act-group-head:hover'), 'color')).toBe('var(--text-secondary)');

    const statusFailed = ruleBlock('.act-node .act-status-failed,\n.act-group-head .act-status-failed');
    expect(decl(statusFailed, 'color')).toBe('var(--danger)');
    const statusRunning = ruleBlock('.act-node .act-status-running,\n.act-group-head .act-status-running');
    expect(decl(statusRunning, 'color')).toBe('var(--accent-text)');
  });
});
