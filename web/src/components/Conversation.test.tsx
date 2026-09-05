/** Conversation.tsx 流式渲染策略测试（P0-2a：streaming 段纯文本零解析，
 *  done 段一次性 markdown——HANDOFF_PERF_FRONTEND §6 P0-2 方案 a）。 */
import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { ChainNodeView } from './Conversation';
import type { ChainNode } from '../lib/projection';

function modelNode(text: string, status: 'streaming' | 'done'): ChainNode {
  return { kind: 'model', segment: { text, status } };
}

describe('ChainNodeView — 流式 markdown 增量化（P0-2a）', () => {
  it('streaming 段渲染纯文本：不跑 markdown 解析（原始标记原样透传）', () => {
    const html = renderToStaticMarkup(
      <ChainNodeView node={modelNode('**加粗** 与 `code`', 'streaming')} density="balanced" />,
    );
    expect(html).toContain('**加粗**'); // 标记原样可见（打字机状态）
    expect(html).not.toContain('<strong>'); // 未解析
    expect(html).not.toContain('md-paragraph');
    expect(html).toContain('stream-caret');
  });

  it('done 段一次性 markdown 解析（model/completed 后）', () => {
    const html = renderToStaticMarkup(
      <ChainNodeView node={modelNode('**加粗**', 'done')} density="balanced" />,
    );
    expect(html).toContain('<strong>');
    expect(html).not.toContain('stream-caret');
  });

  it('streaming 段纯文本路径仍过不可信截断（20k 上限不旁路）', () => {
    const html = renderToStaticMarkup(
      <ChainNodeView node={modelNode('x'.repeat(25_000), 'streaming')} density="balanced" />,
    );
    expect(html.length).toBeLessThan(25_000);
  });
});
