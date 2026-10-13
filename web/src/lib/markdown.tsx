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
 *   - 链接 [text](url) 与裸 URL autolink（#1096）
 */

import { Fragment, useEffect, useState, type ReactNode } from 'react';
import { CopyButton } from '../components/CopyButton';
import { highlightCode, type HighlightedCode } from './highlight';

/** 围栏 info string → shiki 语言 id：取首词（```ts strict → ts）、空白 → null。
 *  未知名不做猜测——白名单拒绝在 highlight.ts（bundledLanguages 存在性）。
 *  导出仅为测试断言用。 */
export function parseFenceLang(info: string): string | null {
  const id = info.trim().split(/\s+/)[0] ?? '';
  return id ? id : null;
}

/** 行内标记解析：把 [text](url) / 裸 URL / **bold** / `code` 切成 JSX 节点。
 *  链接放首位，label 内可再含 bold/code（下方递归渲染）。
 *  裸 URL 终止于空白与中日韩标点——中文句子里 URL 与后文之间无空格，
 *  不把全角标点算进终止符会吞掉后续正文（#1096 实测）。 */
const INLINE_SPLIT = /(\[[^\]]*\]\((?:[^()\n]|\([^()]*\))*\)|\*\*[^*]+\*\*|`[^`]+`|https?:\/\/[^\s<>"`，。、；：！？（）【】《》「」『』]+)/gi;

/** markdown 链接 `[label](url)`；url 允许一层配对括号（如 wiki 词条 `Foo_(bar)`）。 */
const MD_LINK = /^\[([^\]]*)\]\(((?:[^()\n]|\([^()]*\))*)\)$/;

/** 链接 scheme 白名单：仅 http/https 且 `//` 后非空可点（防 javascript:/data:/
 *  vbscript: 等注入，并挡退化的 `https://`）。沿用 StepDetail.tsx:686 约定
 *  （ADR-0018 D7）：JSX 结构化渲染 + target/rel，不用 dangerouslySetInnerHTML。 */
const SAFE_HREF = /^https?:\/\/\S/i;

/** 裸 URL 的句末标点不属于链接（仿 GFM / Slack autolink）：剥离中英文句读与
 *  未配对的收尾括号（配对括号如 wiki 词条 `Foo_(bar)` 保留）。 */
const URL_TAIL_PUNCT = new Set(['.', ',', ';', ':', '!', '?', '。', '，', '、', '；', '：', '！', '？', '"', "'"]);
const URL_CLOSING = new Set([')', '）', ']']);

function trimUrlTail(url: string): string {
  let end = url.length;
  while (end > 0 && URL_TAIL_PUNCT.has(url[end - 1])) end--;
  while (end > 0 && URL_CLOSING.has(url[end - 1])) {
    const seg = url.slice(0, end - 1);
    const opens = (seg.match(/[（(]/g) ?? []).length;
    const closes = (seg.match(/[）)]/g) ?? []).length;
    if (opens > closes) break;
    end--;
  }
  return url.slice(0, end);
}

/** 白名单 scheme 才产生结构化 <a>；危险/未知 scheme 返回 null，交回纯文本渲染。 */
function renderAnchor(href: string, label: ReactNode, key: string): ReactNode | null {
  if (!SAFE_HREF.test(href)) return null;
  return (
    <a key={key} className="md-link" href={href} target="_blank" rel="noopener noreferrer">
      {label}
    </a>
  );
}

function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const parts = text.split(INLINE_SPLIT);
  return parts.filter(Boolean).map((part, i) => {
    const key = `${keyPrefix}-${i}`;
    const mdLink = MD_LINK.exec(part);
    if (mdLink) {
      // 空 label 或不安全 scheme：按原文纯文本呈现（内容不静默丢弃）
      const anchor =
        mdLink[1].trim() === ''
          ? null
          : renderAnchor(mdLink[2].trim(), renderInline(mdLink[1], `${key}-l`), key);
      return anchor ?? <span key={key}>{part}</span>;
    }
    if (part.startsWith('**') && part.endsWith('**') && part.length > 4) {
      return <strong key={key}>{part.slice(2, -2)}</strong>;
    }
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
      return <code key={key}>{part.slice(1, -1)}</code>;
    }
    if (/^https?:\/\//i.test(part)) {
      const href = trimUrlTail(part);
      const anchor = renderAnchor(href, href, `${key}-a`);
      if (anchor && href.length < part.length) {
        // 句末标点留在链接外，作为普通文本
        return (
          <Fragment key={key}>
            {anchor}
            {part.slice(href.length)}
          </Fragment>
        );
      }
      if (anchor) return anchor;
    }
    return <span key={key}>{part}</span>;
  });
}

/** 完成态围栏代码块（#99）：异步高亮渐进增强——首帧纯文本（SSR 同构），
 *  highlightCode 返回后换 token 渲染；失败/无语言保持纯文本（不白屏）。
 *  高亮只发生在 done 段（renderMarkdown 仅完成态调用，流式路径零 shiki）。 */
export function MdCodeBlock({ code, lang }: { code: string; lang?: string | null }) {
  const [hl, setHl] = useState<HighlightedCode | null>(null);
  const [wrap, setWrap] = useState(false);
  useEffect(() => {
    let alive = true;
    setHl(null); // code/lang 变更即回退纯文本——旧结果的 token 不得错配到新代码
    void highlightCode(code, lang ?? null).then((r) => {
      if (alive) setHl(r);
    });
    return () => {
      alive = false;
    };
  }, [code, lang]);

  return (
    <div className={`md-code${wrap ? ' md-code-wrap' : ''}`}>
      <button
        type="button"
        className="md-code-wrap-btn"
        onClick={() => setWrap((v) => !v)}
        aria-label={wrap ? '代码不换行' : '代码自动换行'}
      >
        {wrap ? '不换行' : '自动换行'}
      </button>
      <CopyButton text={code} label="复制代码" />
      {hl ? <HighlightedPre hl={hl} /> : <pre className="md-code-block"><code>{code}</code></pre>}
    </div>
  );
}

/** 高亮渲染：token 色直接 inline（恒深底容器，颜色不随主题翻转——见
 *  highlight.ts 单主题决策）。行以 inline span + '\n' 换行（空行为空数组，
 *  '\n' 保高度）；仓库 no-innerHTML 边界内渲染。 */
function HighlightedPre({ hl }: { hl: HighlightedCode }) {
  return (
    <pre className="md-code-block md-code-hl">
      <code>
        {hl.lines.map((line, i) => (
          <span key={i} className="md-code-line">
            {line.map((t, j) =>
              t.color ? (
                <span key={j} style={{ color: t.color }}>
                  {t.content}
                </span>
              ) : (
                t.content
              ),
            )}
            {i < hl.lines.length - 1 ? '\n' : ''}
          </span>
        ))}
      </code>
    </pre>
  );
}

export function renderMarkdown(text: string): ReactNode[] {
  const lines = text.split('\n');
  const blocks: ReactNode[] = [];
  let listBuffer: string[] = [];
  let codeBuffer: string[] | null = null;
  // T6（#99）：围栏 info string 的语言 id（开栏捕获、闭栏传递）
  let fenceLang: string | null = null;

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
        fenceLang = parseFenceLang(line.trimStart().slice(3));
      } else {
        blocks.push(<MdCodeBlock key={key} code={codeBuffer.join('\n')} lang={fenceLang} />);
        codeBuffer = null;
        fenceLang = null;
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
      // 语义化标题：对话流里 #/## 视作 h3，###/#### 视作 h4（h1/h2 保留给页面骨架）
      const Tag = heading[1].length <= 2 ? 'h3' : 'h4';
      blocks.push(
        <Tag className="md-heading" key={key}>{renderInline(heading[2], key)}</Tag>,
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
    blocks.push(<MdCodeBlock key="md-code-final" code={codeBuffer.join('\n')} lang={fenceLang} />);
  }
  flushList('md-list-final');

  return blocks;
}
