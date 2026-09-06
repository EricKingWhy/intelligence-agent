/** highlight 管线契约（#99，D18）——真实 shiki（dependencies，node 可跑）。
 *  锁定：双主题 token 结构一致、内容 verbatim、无标注/未知语言不猜、空码降级。 */

import { describe, expect, it } from 'vitest';
import { highlightCode } from './highlight';

describe('highlightCode — 完成态代码高亮管线', () => {
  it('js 代码：token 色 non-empty（恒深底单主题）、内容 verbatim', async () => {
    const r = await highlightCode('const a = 1;', 'js');
    expect(r).not.toBeNull();
    const flat = r!.lines.flat();
    expect(flat.map((t) => t.content).join('')).toBe('const a = 1;');
    expect(flat.filter((t) => t.content.trim()).every((t) => t.color)).toBe(true);
  });

  it('python 多行：行结构保留', async () => {
    const r = await highlightCode('def f():\n    return 1', 'python');
    expect(r).not.toBeNull();
    expect(r!.lines.length).toBe(2);
  });

  it('无标注语言不猜（票面硬约束：语言从围栏 info string 取）', async () => {
    expect(await highlightCode('SELECT 1;', null)).toBeNull();
  });

  it('未知语言不猜', async () => {
    expect(await highlightCode('x', 'definitely-not-a-lang')).toBeNull();
  });

  it('空代码降级 null', async () => {
    expect(await highlightCode('   ', 'js')).toBeNull();
  });
});
