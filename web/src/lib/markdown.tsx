/** 轻量 Markdown 渲染（零依赖，白名单子集）。
 *
 * 模型输出常带 **bold**、`inline code`、- 列表、### 标题。纯 pre-wrap 会把
 * 标记符号裸露给用户，因此做最小行级解析。安全：全部走 JSX 结构化渲染，
 * 不用 dangerouslySetInnerHTML，React 自动转义文本，无注入面。
 *
 * 支持子集（刻意不做的：表格、嵌套列表、图片——模型回答里出现率低，
 * 解析错了反而制造视觉噪音）：
 *   - ``` 代码块（围栏）
 *   - #/##/### 标题
 *   - -/* 无序列表（单层）
 *   - 行内 **bold** 与 `code`
 */

import type { ReactNode } from 'react';

/** 行内标记解析：把 **bold** / `code` 切成 JSX 节点。 */
function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g);
  return parts.filter(Boolean).map((part, i) => {
    const key = `${keyPrefix}-${i}`;
    if (part.startsWith('**') && part.endsWith('**') && part.length > 4) {
      return <strong key={key}>{part.slice(2, -2)}</strong>;
    }
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
      return <code key={key}>{part.slice(1, -1)}</code>;
    }
    return <span key={key}>{part}</span>;
  });
}

export function renderMarkdown(text: string): ReactNode[] {
  const lines = text.split('\n');
  const blocks: ReactNode[] = [];
  let listBuffer: string[] = [];
  let codeBuffer: string[] | null = null;

  const flushList = (key: string) => {
    if (listBuffer.length === 0) return;
    blocks.push(
      <ul className="md-ul" key={key}>
        {listBuffer.map((item, i) => (
          <li className="md-li" key={`${key}-${i}`}>{renderInline(item, `${key}-${i}`)}</li>
        ))}
      </ul>,
    );
    listBuffer = [];
  };

  for (const [idx, line] of lines.entries()) {
    const key = `md-${idx}`;

    // 围栏代码块
    if (line.trimStart().startsWith('```')) {
      if (codeBuffer === null) {
        flushList(key);
        codeBuffer = [];
      } else {
        blocks.push(
          <pre className="md-code-block" key={key}>
            <code>{codeBuffer.join('\n')}</code>
          </pre>,
        );
        codeBuffer = null;
      }
      continue;
    }
    if (codeBuffer !== null) {
      codeBuffer.push(line);
      continue;
    }

    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      flushList(key);
      blocks.push(
        <div className="md-heading" key={key}>{renderInline(heading[2], key)}</div>,
      );
      continue;
    }

    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    if (bullet) {
      listBuffer.push(bullet[1]);
      continue;
    }

    flushList(key);
    if (line.trim() === '') continue;
    blocks.push(
      <div className="md-paragraph" key={key}>{renderInline(line, key)}</div>,
    );
  }

  // 收尾：未闭合的代码块/列表按原样落盘
  if (codeBuffer !== null) {
    blocks.push(
      <pre className="md-code-block" key="md-code-final">
        <code>{codeBuffer.join('\n')}</code>
      </pre>,
    );
  }
  flushList('md-list-final');

  return blocks;
}
