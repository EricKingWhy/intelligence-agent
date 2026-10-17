// @vitest-environment jsdom
/** #1312：Composer 会话级目标 pill。
 *
 * AC1 的两态：
 *  - 选中会话（`sessionId !== null`）→「正在向会话 <8 位 id> 追问」+「改为新会话」；
 *  - 未选中（`sessionId === null`）→「将创建新会话」，无按钮。
 *
 * 用真挂载而不是 SSR 字符串：`改为新会话` 的价值在**点击后走到 onNewSession**
 * （App 侧接 `handleNew`），SSR 只能证明文案在，证明不了动作通。
 *
 * 第三条是 AC2 的机械下限：新 pill 与 #1028 归属 chip 必须是**两个节点**、两块
 * 语义（会话身份 vs 项目归属），不能一边把另一边吃掉或并进同一个 role="note"。
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Ownership } from '../lib/ownership';
import { Composer } from './Composer';

const SESSION_ID = 'abcdef12-3456-7890-abcd-ef1234567890';

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

function paint(props: {
  sessionId?: string | null;
  onNewSession?: () => void;
  ownership?: Ownership | null;
}): void {
  act(() => {
    root.render(
      <Composer
        streaming={false}
        onSubmit={() => {}}
        onCancel={() => {}}
        onEnsureSession={async () => null}
        {...props}
      />,
    );
  });
}

const target = (): HTMLElement | null =>
  container.querySelector<HTMLElement>('[data-testid="composer-session-target"]');

describe('Composer 会话级目标 pill（#1312）', () => {
  it('选中会话：显示 8 位 id 与「改为新会话」，点击调用 onNewSession', () => {
    const onNewSession = vi.fn();
    paint({ sessionId: SESSION_ID, onNewSession });

    const pill = target();
    expect(pill).not.toBeNull();
    expect(pill!.textContent).toContain('正在向会话');
    expect(pill!.textContent).toContain('abcdef12');
    // 只露前 8 位：完整 id 不应出现在可见文案里（避免长 id 撑破 composer）。
    expect(pill!.textContent).not.toContain(SESSION_ID);

    const button = pill!.querySelector<HTMLButtonElement>('button[aria-label="改为新会话"]');
    expect(button).not.toBeNull();
    expect(button!.textContent).toBe('改为新会话');

    act(() => button!.click());
    expect(onNewSession).toHaveBeenCalledTimes(1);
  });

  it('未选中会话：显示「将创建新会话」，且没有「改为新会话」按钮', () => {
    paint({ sessionId: null });

    const pill = target();
    expect(pill).not.toBeNull();
    expect(pill!.textContent).toBe('将创建新会话');
    expect(pill!.querySelector('button')).toBeNull();
  });

  it('与 #1028 归属 chip 并存且互不吞并（会话身份 ≠ 项目归属）', () => {
    paint({
      sessionId: 'sess-1',
      onNewSession: () => {},
      ownership: {
        kind: 'project',
        projectTitle: '项目 alpha',
        cwd: 'D:/repos/alpha',
        label: '项目 alpha · D:/repos/alpha',
        title: '项目 alpha · D:/repos/alpha',
      },
    });

    const pill = target();
    const chip = container.querySelector<HTMLElement>('.composer-owner-chip');
    expect(pill).not.toBeNull();
    expect(chip).not.toBeNull();
    expect(pill!.contains(chip)).toBe(false);
    expect(chip!.contains(pill)).toBe(false);
    expect(chip!.getAttribute('role')).toBe('note');
    expect(pill!.textContent).toContain('正在向会话');
    expect(chip!.textContent).toContain('项目 alpha');
  });
});
