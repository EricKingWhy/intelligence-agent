/** Conversation.tsx 流式渲染策略测试（P0-2a：streaming 段纯文本零解析，
 *  done 段一次性 markdown——HANDOFF_PERF_FRONTEND §6 P0-2 方案 a）。 */
import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { ChainNodeView } from './Conversation';
import type { ChainNode } from '../lib/projection';
import type { ToolCall } from '../types';
import type { Disclosure } from '../lib/disclosure';

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

describe('ChainNodeView — 语义图标行（PRD §5.2/§10，ADR-0014 D1）', () => {
  it('streaming 段 = thinking 行（🧠 思考 + 流式正文共存）', () => {
    const html = renderToStaticMarkup(
      <ChainNodeView node={modelNode('分析中…', 'streaming')} density="balanced" />,
    );
    expect(html).toContain('model-kind-row-thinking');
    expect(html).toContain('思考');
    expect(html).toContain('stream-caret'); // 正文照常打字机
  });

  it('final-answer（isFinalModel=true）：无图标行，高对比正文', () => {
    const html = renderToStaticMarkup(
      <ChainNodeView node={modelNode('**最终答案**', 'done')} density="balanced" isFinalModel />,
    );
    expect(html).not.toContain('model-kind-row');
    expect(html).toContain('<strong>');
    expect(html).not.toContain('model-output-intermediate');
  });

  it('中间 done 段（isFinalModel=false）：model 行 + 低对比正文', () => {
    const html = renderToStaticMarkup(
      <ChainNodeView node={modelNode('阶段小结', 'done')} density="balanced" isFinalModel={false} />,
    );
    expect(html).toContain('model-kind-row'); // 有语义行（无 thinking 态）
    expect(html).not.toContain('model-kind-row-thinking');
    expect(html).toContain('model-output-intermediate');
    expect(html).toContain('阶段小结');
  });

  it('复制按钮按段角色分文案（终段=复制回答 / 中间段=复制输出）', () => {
    const finalHtml = renderToStaticMarkup(
      <ChainNodeView node={modelNode('答', 'done')} density="balanced" isFinalModel />,
    );
    expect(finalHtml).toContain('复制回答');
    const midHtml = renderToStaticMarkup(
      <ChainNodeView node={modelNode('段', 'done')} density="balanced" isFinalModel={false} />,
    );
    expect(midHtml).toContain('复制输出');
  });
});

describe('ChainNodeView — ToolCard L 级接线（ADR-0014 D2）', () => {
  const toolNode: ChainNode = {
    kind: 'tool',
    tool: {
      tool_call_id: 'c1', name: 'bash', args: { command: 'ls' }, status: 'success',
      result: { ok: true, data: { exit_code: 0 } },
    } as ToolCall,
  };

  it('disclosure.levelFor 决定 L 级（override L2 → 渲染完整内容面 + raw）', () => {
    const disclosure: Disclosure = {
      levelFor: (key, d) => (key === 'tool:c1' ? 2 : d === 'raw' ? 2 : 0),
      setLevel: () => {},
    };
    const html = renderToStaticMarkup(
      <ChainNodeView node={toolNode} density="balanced" disclosure={disclosure} />,
    );
    expect(html).toContain('tool-card-body');
  });

  it('无 override 时跟随 density 默认（balanced → 仅 L0 行）', () => {
    const html = renderToStaticMarkup(
      <ChainNodeView node={toolNode} density="balanced" />,
    );
    expect(html).not.toContain('act-detail-inline');
    expect(html).not.toContain('tool-card-body');
  });
});
