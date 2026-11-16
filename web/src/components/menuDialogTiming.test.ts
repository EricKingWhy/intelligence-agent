// @vitest-environment jsdom
/** #1694 helper 自检。
 *  除状态机四步外，另有两类**能真正失败**的守卫：
 *  - 真实 `Event` + `dispatchEvent`：延后闭包执行时 `currentTarget` 已为 null，
 *    所以闭包只能引用同步期捕获的节点（契约写在 `deferMenuInitialFocus` 的 JSDoc）；
 *  - 生产调用点静态扫描：这些文件里 `currentTarget` 只允许出现在同步取容器那一种
 *    形状（`const x = e.currentTarget as HTMLElement;`）——写成多行闭包也绕不过去。
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

  it('上一轮没被消费的初焦不会落到下一个弹窗上（openMenuDialog 清 pendingFocus）', () => {
    /* 造出「置位了、但菜单关闭回调没来」的残留：pendingFocus 此刻挂着 stale。 */
    let staleRan = false;
    openMenuDialog(() => {});
    deferMenuInitialFocus(cancelable(), () => {
      staleRan = true;
    });

    /* 下一次打开：必须先清掉残留，否则下面这次关闭会把 stale 闭包落下去。 */
    openMenuDialog(() => {});
    const closeEvent = cancelable();
    expect(takeMenuCloseAutoFocus(closeEvent)).toBe(false);
    expect(closeEvent.defaultPrevented).toBe(false);
    expect(staleRan).toBe(false);
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

  it('focusFirstIn 跳过 tabindex=-1 / disabled / hidden，落到首个真正可聚焦的元素', () => {
    const root = document.createElement('div');
    /* 三个"拦不住但聚焦不了"的形态都排在期望落点**之前**——否则 find 命中 #a 后
     * 这几条过滤删掉也不会红（恒真断言）。 */
    root.innerHTML =
      '<button tabindex="-1">关闭</button>' +
      '<button id="hidden" hidden>藏起来</button>' +
      '<button id="disabled" disabled>不可用</button>' +
      '<input id="hiddentype" type="hidden" />' +
      '<input id="a" />' +
      '<button id="b">取消</button>';
    document.body.append(root);
    focusFirstIn(root);
    expect(document.activeElement?.id).toBe('a');
    root.remove();
  });

  it('focusFirstIn 遇到聚焦不了的候选会继续试下一个（fieldset[disabled] 内的按钮）', () => {
    const root = document.createElement('div');
    /* fieldset[disabled] 内的按钮：`tabIndex` 是 0、`disabled` 属性为 false，
     * 但 `focus()` 被拒 —— 只靠属性过滤会停在这里。 */
    root.innerHTML =
      '<fieldset disabled><button id="dead">关不掉</button></fieldset><input id="a" />';
    document.body.append(root);
    focusFirstIn(root);
    expect(document.activeElement?.id).toBe('a');
    root.remove();
  });

  it('生产调用点不得在延后闭包里读 event.currentTarget', () => {
    /* jsdom 里 `import.meta.url` 不是 file: URL，vitest 的 cwd 是 `web/`。 */
    const base = cwd() as string;
    const allowed = /^\s*const \w+ = e\.currentTarget as HTMLElement;\s*$/;
    for (const name of [
      'MemoryPanel.tsx',
      'ApprovePolicyPanel.tsx',
      'DeleteSessionDialog.tsx',
      'RecoveryListPanel.tsx',
      'SessionList.tsx',
      'SessionReplayDialog.tsx',
      'TaskCreationDialog.tsx',
      'ProjectDialogs.tsx',
    ]) {
      const source = readFileSync(`${base}/src/components/${name}`, 'utf8');
      const lines = source.split('\n') as string[];

      /* ① 全文件：`currentTarget` 只允许「同步取容器」这一种形状（注释不算）。 */
      lines.forEach((line: string, index: number) => {
        if (!line.includes('currentTarget')) return;
        const trimmed = line.trim();
        /* 注释里的说明文字不算调用点。 */
        if (trimmed.startsWith('*') || trimmed.startsWith('/*') || trimmed.startsWith('//')) return;
        expect(line, `${name}:${index + 1}`).toMatch(allowed);
      });

      /* ② 实参区：从 `deferMenuInitialFocus(` 起按括号配平扫到收尾，整段都不许出现
       * `currentTarget`——闭包写成多行也逃不掉（行局部扫描的盲区）。 */
      /* ② 实参区（含**) 全部**调用点**）：从 `deferMenuInitialFocus(` 起按括号配平扫到
       * 收尾，整段都不许出现 `currentTarget`——闭包写成多行也逃不掉（行局部扫描的盲区）。 */
      let from = 0;
      for (;;) {
        const at = source.indexOf('deferMenuInitialFocus(', from);
        if (at < 0) break;
        let depth = 0;
        let end = at;
        for (; end < source.length; end += 1) {
          const ch = source[end];
          if (ch === '(') depth += 1;
          else if (ch === ')') {
            depth -= 1;
            if (depth === 0) break;
          }
        }
        const call = source.slice(at, end + 1);
        const lineOfCall = source.slice(0, at).split('\n').length;
        expect(call, `${name}:${lineOfCall} 的实参区`).not.toContain('currentTarget');
        from = end + 1;
      }
    }
  });
});
