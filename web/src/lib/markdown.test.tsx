/** markdown.tsx 白名单渲染测试——结构断言（无需 DOM）。 */

import { describe, expect, it } from 'vitest';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import type { ReactElement } from 'react';
import { parseFenceLang, renderMarkdown, MdCodeBlock } from './markdown';

// React 19 类型中 ReactElement.props 是 unknown——测试里断言结构需要访问 props，
// 收窄为 any 是测试代码的局部决定，不扩散到产品代码。
type AnyEl = ReactElement<Record<string, any>>;

function firstElement(nodes: ReturnType<typeof renderMarkdown>): AnyEl {
  return nodes[0] as AnyEl;
}

describe('renderMarkdown', () => {
  it('**bold** 渲染为 strong', () => {
    const nodes = renderMarkdown('**创建文件**：hi.txt');
    const el = firstElement(nodes);
    expect(el.props.children[0].type).toBe('strong');
    expect(el.props.children[0].props.children).toBe('创建文件');
  });

  it('`code` 渲染为 code 元素', () => {
    const nodes = renderMarkdown('内容为 `hello`（5 字符）');
    const el = firstElement(nodes);
    // renderInline split: ['内容为 ', '`hello`', '（5 字符）'] → code 在 index 1
    expect(el.props.children[1].type).toBe('code');
    expect(el.props.children[1].props.children).toBe('hello');
  });

  it('- 列表聚合为 md-ul/md-li', () => {
    const nodes = renderMarkdown('- a\n- b');
    const ul = firstElement(nodes);
    expect(ul.type).toBe('ul');
    expect(ul.props.className).toBe('md-ul');
    expect(ul.props.children).toHaveLength(2);
  });

  it('#/## 渲染为 h3，###/#### 渲染为 h4', () => {
    const h3 = firstElement(renderMarkdown('# 标题'));
    const h4 = firstElement(renderMarkdown('### 小节'));
    expect(h3.type).toBe('h3');
    expect(h4.type).toBe('h4');
    expect(h4.props.className).toBe('md-heading');
  });

  /** 围栏代码块现为 MdCodeBlock 组件（.md-code wrapper 含复制按钮）——
   *  未渲染元素树无法穿透函数组件，按组件类型 + code 属性断言。 */
  it('围栏代码块渲染为 MdCodeBlock（wrapper 含复制按钮）', () => {
    const nodes = renderMarkdown('```py\nprint(1)\n```');
    const wrapper = firstElement(nodes);
    expect(wrapper.type).toBe(MdCodeBlock);
    expect(wrapper.props.code).toBe('print(1)');
  });

  it('未闭合围栏按代码块收尾（流式安全）', () => {
    const nodes = renderMarkdown('```py\nprint(1)');
    const wrapper = firstElement(nodes);
    expect(wrapper.type).toBe(MdCodeBlock);
    expect(wrapper.props.code).toBe('print(1)');
  });

  it('HTML 文本不产生 script 元素（无注入面，React 结构化渲染）', () => {
    const nodes = renderMarkdown('<script>alert(1)</script>');
    // 结构化断言：整树不存在字符串 'script' 类型的元素节点
    const types: unknown[] = [];
    const walk = (n: unknown) => {
      if (Array.isArray(n)) return n.forEach(walk);
      if (n && typeof n === 'object' && 'type' in n) {
        types.push((n as AnyEl).type);
        walk((n as AnyEl).props?.children);
      }
    };
    walk(nodes);
    expect(types).not.toContain('script');
    expect(types).toContain('span'); // 原文作为纯文本 children
  });

  it('纯文本按行分段', () => {
    const nodes = renderMarkdown('第一行\n\n第二行');
    expect(nodes).toHaveLength(2);
    // md-paragraph 的 children 是 renderInline 数组，文本在首个 span 里
    expect((nodes[0] as AnyEl).props.children[0].props.children).toBe('第一行');
  });
});

describe('T6 — 围栏语言提取与代码块控件（#99）', () => {
  it('parseFenceLang：语言 id 提取、多词 info 取首词、空白 → null', () => {
    expect(parseFenceLang('python')).toBe('python');
    expect(parseFenceLang(' ts strict ')).toBe('ts');
    expect(parseFenceLang('')).toBeNull();
    expect(parseFenceLang('   ')).toBeNull();
  });

  it('renderMarkdown 围栏产出 MdCodeBlock（md-code 容器在场）', () => {
    const blocks = renderMarkdown('```js\nconst a = 1;\n```');
    const html = renderToStaticMarkup(createElement('div', null, ...blocks));
    expect(html).toContain('md-code');
    expect(html).toContain('const a = 1;');
  });

  it('MdCodeBlock fallback：纯文本 + wrap 控件 + 复制（SSR 不触发异步高亮）', () => {
    const html = renderToStaticMarkup(createElement(MdCodeBlock, { code: 'const a = 1;', lang: 'js' }));
    expect(html).toContain('const a = 1;');
    expect(html).toContain('md-code-wrap-btn');
    expect(html).toContain('复制代码');
    // wrap 初始 off：容器不挂 md-code-wrap（Standards 轴 Dead-Code finding 的回归锚）
    expect(html).not.toContain('md-code md-code-wrap');
  });
});

/** 遍历渲染树收集所有元素节点（结构断言用；不依赖 DOM）。 */
function collectElements(nodes: unknown, out: AnyEl[] = []): AnyEl[] {
  if (Array.isArray(nodes)) {
    nodes.forEach((n) => collectElements(n, out));
    return out;
  }
  if (nodes && typeof nodes === 'object' && 'type' in nodes) {
    out.push(nodes as AnyEl);
    collectElements((nodes as AnyEl).props?.children, out);
  }
  return out;
}

function anchorsOf(nodes: ReturnType<typeof renderMarkdown>): AnyEl[] {
  return collectElements(nodes).filter((n) => n.type === 'a');
}

describe('链接渲染（#1096 — 对话区出处 URL 可点）', () => {
  it('[text](https://…) 渲染为可点 <a>：新标签页 + rel=noopener noreferrer', () => {
    const nodes = renderMarkdown('见 [文档](https://example.com/docs)。');
    const anchors = anchorsOf(nodes);
    expect(anchors).toHaveLength(1);
    expect(anchors[0].props.href).toBe('https://example.com/docs');
    expect(anchors[0].props.target).toBe('_blank');
    expect(anchors[0].props.rel).toBe('noopener noreferrer');
    expect(anchors[0].props.children).toBe('文档');
  });

  it('裸 https://… 自动链接化（Slack 式 autolink）', () => {
    const nodes = renderMarkdown('来源 https://example.com/a/b');
    const anchors = anchorsOf(nodes);
    expect(anchors).toHaveLength(1);
    expect(anchors[0].props.href).toBe('https://example.com/a/b');
    expect(anchors[0].props.target).toBe('_blank');
    expect(anchors[0].props.children).toBe('https://example.com/a/b');
  });

  it('裸 URL 后的句末标点不被吞进链接（中英文标点）', () => {
    const en = anchorsOf(renderMarkdown('详见 https://example.com/a, 再见'));
    expect(en).toHaveLength(1);
    expect(en[0].props.href).toBe('https://example.com/a');
    const zh = anchorsOf(renderMarkdown('详见 https://example.com/a。'));
    expect(zh).toHaveLength(1);
    expect(zh[0].props.href).toBe('https://example.com/a');
  });

  it('javascript: / data: / 相对 scheme 不链接化（安全回归）', () => {
    expect(anchorsOf(renderMarkdown('[点我](javascript:alert(1))'))).toHaveLength(0);
    expect(anchorsOf(renderMarkdown('[x](data:text/html,<b>1</b>)'))).toHaveLength(0);
    expect(anchorsOf(renderMarkdown('[x](//evil.example.com)'))).toHaveLength(0);
    // 危险 markdown 链接按原文纯文本呈现，内容不被静默丢弃
    const html = renderToStaticMarkup(
      createElement('div', null, ...renderMarkdown('[点我](javascript:alert(1))')),
    );
    expect(html).toContain('javascript:alert(1)');
    expect(html).not.toContain('href=');
  });

  it('普通文本不受影响（不产生 <a>，仍是纯文本 span）', () => {
    const nodes = renderMarkdown('这是一段普通文本，没有链接。');
    expect(anchorsOf(nodes)).toHaveLength(0);
    expect((nodes[0] as AnyEl).props.children[0].props.children).toBe('这是一段普通文本，没有链接。');
  });

  it('行内 **bold** / `code` 与链接共存时各自渲染', () => {
    const nodes = renderMarkdown('**重点**：`code` 见 [文档](https://example.com)');
    const anchors = anchorsOf(nodes);
    expect(anchors).toHaveLength(1);
    expect(anchors[0].props.href).toBe('https://example.com');
    const types = collectElements(nodes).map((n) => n.type);
    expect(types).toContain('strong');
    expect(types).toContain('code');
  });
});
