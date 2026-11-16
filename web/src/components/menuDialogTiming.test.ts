// @vitest-environment jsdom
/** #1694 helper 自检。
 *  除状态机四步外，另有两类**能真正失败**的守卫：
 *  - 真实 `Event` + `dispatchEvent`：延后闭包执行时 `currentTarget` 已为 null，
 *    所以闭包只能引用同步期捕获的节点（契约写在 `deferMenuInitialFocus` 的 JSDoc）；
 *  - 生产调用点静态扫描：`onOpenAutoFocus` 回调体的顶层只允许「同步取容器」那一句，
 *    `deferMenuInitialFocus(` / `setTimeout(` 等的实参区一律不许出现 `currentTarget`。
 *    扫描前剥掉注释与字符串（否则注释里一个 `)` 就能截断括号配平）。
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

  it('focusFirstIn 跳过 tabindex=-1 / disabled / hidden / display:none，落到首个真正可聚焦的元素', () => {
    const root = document.createElement('div');
    /* 四个「属性拦不住但聚焦不了」的形态都排在期望落点**之前**——若实现退回
     * 「只取首个候选」，本用例会立刻红（恒真断言的防线就靠这个顺序）。 */
    root.innerHTML =
      '<button tabindex="-1">关闭</button>' +
      '<button id="hidden" hidden>藏起来</button>' +
      '<button id="disabled" disabled>不可用</button>' +
      '<input id="hiddentype" type="hidden" />' +
      '<button id="dn" style="display:none">不可见</button>' +
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

  it('focusFirstIn 候选全被拒时退回容器（前提：容器可聚焦，如 FocusScope 的 tabindex=-1）', () => {
    const root = document.createElement('div');
    /* 裸 `div` 的 `focus()` 在浏览器与 jsdom 里都不改 activeElement，所以生产里这个兜底
       只在 `root` 是 Radix FocusScope 容器时有效——它固定注入 `tabIndex: -1`。
       这里显式给它 tabindex，验证「可聚焦容器才会成为兜底落点」这条前提。 */
    root.tabIndex = -1;
    root.innerHTML = '<fieldset disabled><button>关不掉</button></fieldset>';
    document.body.append(root);
    focusFirstIn(root);
    expect(document.activeElement).toBe(root);
    root.remove();
  });

  it('生产调用点不得在延后闭包里读 event.currentTarget', () => {
    /* jsdom 里 `import.meta.url` 不是 file: URL，vitest 的 cwd 是 `web/`。 */
    const base = cwd() as string;
    /* 允许的形状：同步期把容器取进局部 const（参数名不限——`evt` 与 `e` 等价）。 */
    const capture = /^\s*const \w+ = \w+\.currentTarget as HTMLElement;\s*$/;

    /* 扫描前先把注释与字符串字面量的内容抹成空格（保留换行与长度，下标仍可映射回原文）。
       否则注释里一个 `)` 就能截断括号配平，让守卫整体失效——这正是上一轮的可达盲区。 */
    const blank = (source: string): string => {
      const out = source.split('');
      const n = source.length;
      for (let i = 0; i < n; i += 1) {
        const ch = source[i];
        if (ch === '/' && source[i + 1] === '*') {
          const close = source.indexOf('*/', i + 2);
          const stop = close < 0 ? n : close + 2;
          for (let j = i; j < stop; j += 1) if (out[j] !== '\n') out[j] = ' ';
          i = stop - 1;
        } else if (ch === '/' && source[i + 1] === '/') {
          let j = i;
          while (j < n && source[j] !== '\n') {
            out[j] = ' ';
            j += 1;
          }
          i = j - 1;
        } else if (ch === "'" || ch === '"' || ch === '`') {
          let j = i + 1;
          while (j < n && source[j] !== ch) {
            if (source[j] === '\\') {
              out[j] = ' ';
              j += 1;
            }
            if (j < n && source[j] !== '\n') out[j] = ' ';
            j += 1;
          }
          i = j;
        }
      }
      return out.join('');
    };

    /* 从开括号起配平到收尾，返回闭区间下标。 */
    const balanced = (source: string, from: number, open: string, close: string): number => {
      let depth = 0;
      for (let i = from; i < source.length; i += 1) {
        if (source[i] === open) depth += 1;
        else if (source[i] === close) {
          depth -= 1;
          if (depth === 0) return i;
        }
      }
      return source.length - 1;
    };

    const lineOf = (source: string, at: number): number => source.slice(0, at).split('\n').length;

    for (const name of [
      'MemoryPanel.tsx',
      'ApprovePolicyPanel.tsx',
      'DeleteSessionDialog.tsx',
      'RecoveryListPanel.tsx',
      'SessionList.tsx',
      'SessionReplayDialog.tsx',
      'TaskCreationDialog.tsx',
      'ProjectDialogs.tsx',
      'ModelPicker.tsx',
      'ProviderManagerDialog.tsx',
    ]) {
      const raw = readFileSync(`${base}/src/components/${name}`, 'utf8');
      const source = blank(raw);

      /* ① `onOpenAutoFocus` 回调体：`currentTarget` 只允许出现在**同步期取容器**那一句
         （体的顶层、形状固定）。写在嵌套块 / 嵌套闭包里的一律红——那些地方才是跨拍读。
         约定：本次全部调用点都是 `onOpenAutoFocus={(e) => { … }}`，故以箭头体为扫描区。 */
      let at = source.indexOf('onOpenAutoFocus=');
      while (at >= 0) {
        const arrow = source.indexOf('=>', at);
        const bodyStart = arrow < 0 ? -1 : source.indexOf('{', arrow);
        if (bodyStart < 0) {
          at = source.indexOf('onOpenAutoFocus=', at + 'onOpenAutoFocus='.length);
          continue;
        }
        const end = balanced(source, bodyStart, '{', '}');
        const body = source.slice(bodyStart, end + 1);
        let depth = 0;
        for (let i = 0; i < body.length; i += 1) {
          if (body[i] === '{') depth += 1;
          else if (body[i] === '}') depth -= 1;
          if (body[i] !== 'c' || !body.startsWith('currentTarget', i)) continue;
          const startOfLine = body.lastIndexOf('\n', i) + 1;
          const endOfLine = body.indexOf('\n', i);
          const line = body.slice(startOfLine, endOfLine < 0 ? body.length : endOfLine);
          const where = `${name}:${lineOf(raw, bodyStart + startOfLine)}`;
          expect(depth, `${where} 的 currentTarget 必须写在回调体顶层（不得在嵌套闭包里）`).toBe(1);
          expect(line.trim(), where).toMatch(capture);
        }
        at = source.indexOf('onOpenAutoFocus=', end);
      }

      /* ② 延后执行的实参区：`deferMenuInitialFocus(` 与其它把回调推迟到下一拍的调用，
         整个实参区都不许出现 `currentTarget`——闭包写成多行、无花括号也逃不掉。 */
      for (const call of ['deferMenuInitialFocus(', 'setTimeout(', 'queueMicrotask(', 'requestAnimationFrame(']) {
        let from = 0;
        for (;;) {
          const start = source.indexOf(call, from);
          if (start < 0) break;
          const open = start + call.length - 1;
          const end = balanced(source, open, '(', ')');
          const args = source.slice(open, end + 1);
          expect(args, `${name}:${lineOf(raw, start)} 的 ${call.trim()} 实参区`).not.toContain(
            'currentTarget',
          );
          from = end + 1;
        }
      }
    }
  });
});
