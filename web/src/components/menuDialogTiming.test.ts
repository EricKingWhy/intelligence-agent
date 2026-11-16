// @vitest-environment jsdom
/** #1694 helper 自检。
 *  除状态机四步外，另有两类**能真正失败**的守卫：
 *  - 真实 `Event` + `dispatchEvent`：延后闭包执行时 `currentTarget` 已为 null，
 *    所以闭包只能引用同步期捕获的节点（契约写在 `deferMenuInitialFocus` 的 JSDoc）；
 *  - 生产调用点静态扫描：不允许在 `deferMenuInitialFocus` 的同一个实参里读
 *    `e.currentTarget`——这正是 std P1-1 那条每次打开都抛 TypeError 的写法。
 */
import { afterEach, describe, expect, it } from 'vitest';
// node 内置模块没有声明（web 的 tsconfig 只带 vite/client），同其他源码扫描用例。
// @ts-expect-error node 内置模块没有声明
import { readFileSync } from 'node:fs';
// @ts-expect-error node 内置模块没有声明
import { cwd } from 'node:process';
import {
  deferMenuInitialFocus,
  focusFirstIn,
  openMenuDialog,
  takeMenuCloseAutoFocus,
} from './menuDialogTiming';

const cancelable = () => {
  const event = {
    defaultPrevented: false,
    preventDefault() {
      event.defaultPrevented = true;
    },
  };
  return event as unknown as Event & { defaultPrevented: boolean };
};

const previousPointerEvents = document.body.style.pointerEvents;
afterEach(() => {
  document.body.style.pointerEvents = previousPointerEvents;
});

describe('menuDialogTiming', () => {
  it('菜单触发的弹窗：推迟初焦 → 关闭时挡掉归还并落焦', async () => {
    const opened: string[] = [];
    const focused: string[] = [];
    document.body.style.pointerEvents = 'none';

    openMenuDialog(() => opened.push('dialog'));
    /* 菜单留下的 body 锁必须在弹窗 mount 前摘掉，否则弹窗会把它当"原始值"写回。 */
    expect(document.body.style.pointerEvents).toBe('');
    expect(opened).toEqual(['dialog']);

    const mountEvent = cancelable();
    expect(deferMenuInitialFocus(mountEvent, () => focused.push('first'))).toBe(true);
    expect(mountEvent.defaultPrevented).toBe(true);

    const closeEvent = cancelable();
    expect(takeMenuCloseAutoFocus(closeEvent)).toBe(true);
    expect(closeEvent.defaultPrevented).toBe(true);

    await new Promise((r) => setTimeout(r, 5));
    expect(focused).toEqual(['first']);

    /* 标志用完即清：下一次关闭不应再挡。 */
    expect(takeMenuCloseAutoFocus(cancelable())).toBe(false);
  });

  it('菜单关闭但弹窗没开（重复点同一项）：不吃归还，焦点仍回触发器', () => {
    openMenuDialog(() => {});
    const closeEvent = cancelable();
    expect(takeMenuCloseAutoFocus(closeEvent)).toBe(false);
    expect(closeEvent.defaultPrevented).toBe(false);
    /* 置位已消费，后续任何弹窗都不再被接管。 */
    expect(takeMenuCloseAutoFocus(cancelable())).toBe(false);
  });

  it('不由菜单触发的弹窗：两个入口都放行，不劫持焦点', () => {
    const mountEvent = cancelable();
    expect(deferMenuInitialFocus(mountEvent, () => {})).toBe(false);
    expect(mountEvent.defaultPrevented).toBe(false);
    expect(takeMenuCloseAutoFocus(cancelable())).toBe(false);
  });

  it('延后闭包里的事件对象已失效，只有同步期捕获的节点可用', async () => {
    const root = document.createElement('div');
    root.innerHTML = '<button id="close">关闭</button><input id="search" />';
    document.body.append(root);

    /* 真实派发：currentTarget 只在派发期间有值。 */
    const seen: Array<EventTarget | null> = [];
    root.addEventListener('open-auto-focus', (event) => {
      const captured = event.currentTarget as HTMLElement;
      deferMenuInitialFocus(event, () => {
        seen.push(event.currentTarget);
        focusFirstIn(captured);
      });
    });
    openMenuDialog(() => root.dispatchEvent(new Event('open-auto-focus')));
    takeMenuCloseAutoFocus(cancelable());
    await new Promise((r) => setTimeout(r, 5));

    /* 跨拍读事件对象拿到的是 null —— 生产代码若照此写就是 P1-1。 */
    expect(seen).toEqual([null]);
    /* 同步期捕获的容器仍然有效，初焦落进弹窗内首个可聚焦元素。 */
    expect(document.activeElement?.id).toBe('close');
    root.remove();
  });

  it('focusFirstIn 跳过 tabindex=-1 与 hidden，落到首个真正可聚焦的元素', () => {
    const root = document.createElement('div');
    root.innerHTML =
      '<button tabindex="-1">关闭</button><input id="a" />' +
      '<button id="hidden" hidden>藏起来</button><button id="b">取消</button>';
    document.body.append(root);
    focusFirstIn(root);
    expect(document.activeElement?.id).toBe('a');
    root.remove();
  });

  it('生产调用点不得在延后闭包里读 event.currentTarget', () => {
    /* jsdom 里 `import.meta.url` 不是 file: URL，vitest 的 cwd 是 `web/`。 */
    const base = cwd() as string;
    for (const name of [
      'MemoryPanel.tsx',
      'ApprovePolicyPanel.tsx',
      'DeleteSessionDialog.tsx',
      'RecoveryListPanel.tsx',
    ]) {
      const source = readFileSync(`${base}/src/components/${name}`, 'utf8');
      (source.split('\n') as string[]).forEach((line: string, index: number) => {
        if (!line.includes('deferMenuInitialFocus')) return;
        expect(line, `${name}:${index + 1}`).not.toContain('currentTarget');
      });
    }
  });
});
