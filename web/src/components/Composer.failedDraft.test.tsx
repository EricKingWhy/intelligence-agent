// @vitest-environment jsdom
/** #1148（分离投递迟到失败丢文本）：**窗外落定**的失败经 `failedDraft` 回到输入框。
 *
 *  这一档的时机与 `Composer.keepText.test.tsx` 全不同：提交时按"已受理"清空了
 *  （窗外无从等待结局），失败是**几十秒后**才迟到的。所以回填不能走 `submit()` 的
 *  返回值，只能靠这条 prop。数据层的证明在 `useSession.detachedFailure.test.tsx`；
 *  这里锁的是组件侧的最后一跳：prop 到手 ⇒ 文本真的出现在 textarea 里。
 *
 *  纪律（成熟产品同款，见票面「成熟产品依据」）：
 *   - 只在输入框**为空**时回填——用户已打新草稿时绝不覆盖；
 *   - 每次失败都是新对象 ⇒ 同一段文本连续失败两次也各算一次注入；
 *   - **刻意不抢焦点**（回执可能迟到几十秒，用户多半在别处）。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return { ...actual, uploadAttachment: vi.fn() };
});

import { Composer } from './Composer';

let container: HTMLDivElement;
let root: Root;

const textarea = (): HTMLTextAreaElement => container.querySelector('#composer-input')!;

/** 带 `failedDraft` 的渲染：hook 把窗外迟到的失败经这条 prop 送回。 */
function paintWith(failedDraft: { text: string } | null): void {
  act(() => {
    root.render(
      createElement(Composer, {
        streaming: false,
        onSubmit: () => {},
        onCancel: () => {},
        onEnsureSession: async () => 's-1',
        sessionId: 's-1',
        models: [],
        selectedModel: null,
        failedDraft,
      }),
    );
  });
}

async function typeText(text: string): Promise<void> {
  await act(async () => {
    const ta = textarea();
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype,
      'value',
    )?.set;
    setter?.call(ta, text);
    ta.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  paintWith(null);
});

afterEach(async () => {
  await act(async () => { root.unmount(); });
  container.remove();
  vi.clearAllMocks();
});

describe('#1148 窗外迟到失败：failedDraft 把原文还回输入框', () => {
  it('输入框为空时：迟到的失败原文出现在输入框里', async () => {
    await typeText('');
    expect(textarea().value).toBe('');

    await act(async () => { paintWith({ text: '窗外迟到的失败原文' }); });

    expect(textarea().value).toBe('窗外迟到的失败原文');
  });

  it('用户已在打新草稿：迟到的回填**不得**覆盖它（AC3）', async () => {
    await typeText('我正在写新的');

    await act(async () => { paintWith({ text: '窗外迟到的失败原文' }); });

    expect(textarea().value).toBe('我正在写新的');
  });

  it('空白草稿视为空：只打了空格时照样回填（不是留一段空白占位）', async () => {
    await typeText('   ');

    await act(async () => { paintWith({ text: '原文' }); });

    expect(textarea().value).toBe('原文');
  });

  /* AC4：裸 string 状态会被 React 的 `Object.is` 挡掉第二次（同一段文本 ⇒ 相等 ⇒
     跳过重渲染 ⇒ effect 不触发）。"重发一次、又失败一次"正是这条路径的常态。 */
  it('同一段文本连续失败两次：第二次仍是新对象 ⇒ 输入框再次拿到原文', async () => {
    await typeText('');

    await act(async () => { paintWith({ text: '同一段' }); });
    expect(textarea().value).toBe('同一段');

    // 用户把它清掉（例如手动删空），第二次失败必须还能还回来。
    await typeText('');
    await act(async () => { paintWith({ text: '同一段' }); });

    expect(textarea().value).toBe('同一段');
  });

  it('failedDraft 为 null（没有失败）时不注入任何东西', async () => {
    await typeText('');

    await act(async () => { paintWith(null); });

    expect(textarea().value).toBe('');
  });

  it('刻意不抢焦点：回填后焦点不在输入框（用户的下一次按键不落错地方）', async () => {
    const other = document.createElement('input');
    document.body.appendChild(other);
    other.focus();

    await act(async () => { paintWith({ text: '迟到几十秒的原文' }); });

    expect(textarea().value).toBe('迟到几十秒的原文');
    expect(document.activeElement).not.toBe(textarea());
    other.remove();
  });
});
