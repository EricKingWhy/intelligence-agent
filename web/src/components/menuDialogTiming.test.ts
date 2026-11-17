// @vitest-environment jsdom
/** #1694 helper 自检。
 *  除状态机四步外，另有**能真正失败**的守卫：
 *  - 真实 `Event` + `dispatchEvent`：延后闭包执行时 `currentTarget` 已为 null，
 *    所以闭包只能引用同步期捕获的节点（契约写在 `deferMenuInitialFocus` 的 JSDoc）；
 *  - 生产调用点静态扫描（文件末尾两个用例）：`onOpenAutoFocus` 回调体里的
 *    `currentTarget` 只许写在**顶层**（同步读，形状不限），延后调用的实参区一律不许出现；
 *    另有一张固定的**接线计数表**——删掉任一已接入点就会红。
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
    /* 五个候选（`tabindex=-1` 一个，属性上拦不住的四个：hidden / disabled /
     * type=hidden / display:none）都排在期望落点**之前**——若实现退回
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
});

/* ───────────────────────── 生产调用点静态扫描 ───────────────────────── */

const BASE = cwd() as string;
const readComponent = (name: string): string => readFileSync(`${BASE}/src/components/${name}`, 'utf8');

const lineOf = (source: string, at: number): number => source.slice(0, at).split('\n').length;

/** 注释与字符串字面量抹成空格（保留换行与长度，下标仍能映射回原文行号）。
 *
 *  为什么需要它：注释里一个 `)` 就能截断括号配平，让整套扫描静默失明。
 *  为什么写成这份形状：
 *  - 模板串的 `${…}` 是**代码**不是串体——整段抹掉会让「延后调用实参区不许出现
 *    `currentTarget`」在这块区域自己漏判（把事件对象塞进插值正是 P1-1 的形状）；
 *  - 未闭合的引号一律**在行尾终止**——JSX 文本里的英文撇号（`Don't`）与含引号的正则
 *    若被当成串的开头，会把文件余下部分一起吃成空格，守卫从此对谁都失明且**没有红信号**；
 *  - 正则字面量按「前一个有效字符」判定，猜错时最坏只影响该行（见下）。
 *  已知天花板：正则判定用启发式（`REGEX_AFTER`），除法与正则的边界靠它区分。 */
const REGEX_AFTER = new Set('(,=:[!&|?{};+-*%~^'.split(''));

function blank(source: string): string {
  const out = source.split('');
  const n = source.length;
  const wipe = (j: number): void => {
    if (j < n && out[j] !== '\n') out[j] = ' ';
  };

  /** 代码区：从 `i` 扫到 `end` 字符（顶层的 `end` 为 null = 扫到文末），返回其后的下标。 */
  const scanCode = (i: number, end: string | null): number => {
    let j = i;
    let prev = ''; // 上一个有效字符，用来区分 `/` 是除号还是正则开头
    while (j < n) {
      const c = source[j];
      if (end !== null && c === end) return j + 1;
      if (c === '/' && source[j + 1] === '*') {
        const close = source.indexOf('*/', j + 2);
        const stop = close < 0 ? n : close + 2;
        for (let k = j; k < stop; k += 1) wipe(k);
        j = stop;
        continue;
      }
      if (c === '/' && source[j + 1] === '/') {
        let k = j;
        while (k < n && source[k] !== '\n') wipe(k++);
        j = k;
        continue;
      }
      if (c === "'" || c === '"' || c === '`') {
        j = scanString(j, c);
        prev = c;
        continue;
      }
      if (c === '/' && (prev === '' || REGEX_AFTER.has(prev))) {
        j = scanRegex(j);
        prev = '/';
        continue;
      }
      if (!/\s/.test(c)) prev = c;
      j += 1;
    }
    return n;
  };

  /** 串体：`i` 指向开引号，返回串结束后的下标。模板串的插值递归回代码区。 */
  const scanString = (i: number, quote: string): number => {
    let j = i + 1;
    while (j < n) {
      const c = source[j];
      if (c === '\\') {
        wipe(j);
        wipe(j + 1);
        j += 2;
        continue;
      }
      if (c === quote) return j + 1;
      if (quote !== '`' && c === '\n') return j; // 未闭合：行尾终止（JSX 撇号 / 正则里的引号）
      if (quote === '`' && c === '$' && source[j + 1] === '{') {
        j = scanCode(j + 2, '}');
        continue;
      }
      wipe(j);
      j += 1;
    }
    return n;
  };

  /** 正则字面量：`i` 指向开 `/`，返回其后的下标。 */
  const scanRegex = (i: number): number => {
    let j = i + 1;
    let inClass = false;
    while (j < n) {
      const c = source[j];
      if (c === '\\') {
        wipe(j);
        wipe(j + 1);
        j += 2;
        continue;
      }
      if (c === '\n') return j; // 未闭合：行尾终止
      if (c === '[') inClass = true;
      else if (c === ']') inClass = false;
      else if (c === '/' && !inClass) {
        j += 1;
        while (j < n && /[a-z]/i.test(source[j])) j += 1;
        return j;
      }
      wipe(j);
      j += 1;
    }
    return n;
  };

  scanCode(0, null);
  return out.join('');
}

/** 从开括号起配平到收尾，返回闭区间下标。 */
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

/** 允许的站点形态：`onOpenAutoFocus={(e) => { … }}`（参数名不限、允许换行）。
 *  简洁体（`=> e.preventDefault()`）与具名 handler 一律判红——扫描区靠这个形状定位，
 *  放过去就等于静默跳过该站点。 */
const INLINE_BLOCK = /^onOpenAutoFocus=\{\s*\([^)]*\)\s*=>\s*\{/;

/** 三套 helper 入口 + `onOpenAutoFocus` 站点的**固定计数**。
 *
 *  这是「约定成立」的断言，不是自动推导的统计：新增一处同形入口时**必须**来改它。
 *  否则把任一处的 `openMenuDialog(` 包裹（或弹窗侧的 `deferMenuInitialFocus`）删掉，
 *  整套测试仍然是绿的——那正是第 4 轮复审 P3-2 指出的缺口。 */
const WIRING: Array<{ file: string; open: number; defer: number; take: number; autofocus: number }> = [
  { file: 'TopBar.tsx', open: 3, defer: 0, take: 1, autofocus: 0 },
  { file: 'SessionList.tsx', open: 6, defer: 0, take: 2, autofocus: 0 },
  { file: 'ModelPicker.tsx', open: 1, defer: 0, take: 1, autofocus: 0 },
  { file: 'MemoryPanel.tsx', open: 0, defer: 1, take: 0, autofocus: 1 },
  { file: 'ApprovePolicyPanel.tsx', open: 0, defer: 1, take: 0, autofocus: 1 },
  { file: 'DeleteSessionDialog.tsx', open: 0, defer: 1, take: 0, autofocus: 1 },
  { file: 'RecoveryListPanel.tsx', open: 0, defer: 1, take: 0, autofocus: 1 },
  { file: 'SessionReplayDialog.tsx', open: 0, defer: 1, take: 0, autofocus: 1 },
  { file: 'TaskCreationDialog.tsx', open: 0, defer: 1, take: 0, autofocus: 1 },
  { file: 'ProjectDialogs.tsx', open: 0, defer: 3, take: 0, autofocus: 3 },
  { file: 'ProviderManagerDialog.tsx', open: 0, defer: 1, take: 0, autofocus: 1 },
];

describe('生产调用点静态守卫', () => {
  it('接线约定成立：10 处同形入口两侧都有 helper，弹窗初焦站点全为内联箭头块体', () => {
    const totals = { open: 0, defer: 0, take: 0, autofocus: 0 };
    for (const expected of WIRING) {
      const raw = readComponent(expected.file);
      const source = blank(raw);
      const count = (needle: string): number => source.split(needle).length - 1;

      expect(count('openMenuDialog('), `${expected.file} 的 openMenuDialog 调用点`).toBe(expected.open);
      expect(count('deferMenuInitialFocus('), `${expected.file} 的 deferMenuInitialFocus 调用点`).toBe(
        expected.defer,
      );
      expect(count('takeMenuCloseAutoFocus('), `${expected.file} 的 takeMenuCloseAutoFocus 调用点`).toBe(
        expected.take,
      );

      let sites = 0;
      let from = 0;
      for (;;) {
        const at = source.indexOf('onOpenAutoFocus=', from);
        if (at < 0) break;
        sites += 1;
        const where = `${expected.file}:${lineOf(raw, at)}`;
        expect(
          INLINE_BLOCK.exec(source.slice(at)),
          `${where} 的 onOpenAutoFocus 必须是内联箭头块体（写成具名 handler 或简洁体时本守卫覆盖不到，故直接判红）`,
        ).not.toBeNull();
        from = at + 'onOpenAutoFocus='.length;
      }
      expect(sites, `${expected.file} 的 onOpenAutoFocus 站点数`).toBe(expected.autofocus);

      totals.open += expected.open;
      totals.defer += expected.defer;
      totals.take += expected.take;
      totals.autofocus += expected.autofocus;
    }

    /* 菜单侧与弹窗侧是同一机制的两个人：任一处被回退，这里就对不上。 */
    expect(totals.open, '同形入口数（菜单侧 openMenuDialog）').toBe(10);
    expect(totals.defer, '同形入口数（弹窗侧 deferMenuInitialFocus）').toBe(10);
    expect(totals.autofocus, '弹窗初焦站点数').toBe(10);
    expect(totals.take, '菜单关闭归还接管点').toBe(4);
  });

  it('生产调用点不得在延后闭包里读 event.currentTarget', () => {
    for (const { file } of WIRING) {
      const raw = readComponent(file);
      const source = blank(raw);

      /* ① `onOpenAutoFocus` 回调体：`currentTarget` 只许在**顶层**——写进嵌套函数体就是
         跨拍读（闭包在宏任务里才执行）。顶层不限形状：`const root = e.currentTarget as
         HTMLElement`、`if (e.target !== e.currentTarget) return`、`const { currentTarget } = e`
         都是合法的同步读；判据按**函数嵌套深度**而不是花括号深度（解构的 `{…}` 也是一对
         花括号，按括号计数会把它误判成嵌套块）。 */
      const bodyStartOf = (at: number): number => {
        const match = INLINE_BLOCK.exec(source.slice(at));
        return match ? at + match[0].length - 1 : -1;
      };
      let at = source.indexOf('onOpenAutoFocus=');
      while (at >= 0) {
        const bodyStart = bodyStartOf(at);
        if (bodyStart < 0) {
          /* 形态不合约定：由上一个用例判红并给出可读信息，这里只负责不静默跳过。 */
          at = source.indexOf('onOpenAutoFocus=', at + 'onOpenAutoFocus='.length);
          continue;
        }
        const end = balanced(source, bodyStart, '{', '}');
        const body = source.slice(bodyStart, end + 1);
        const where = (index: number): string => `${file}:${lineOf(raw, bodyStart + index)}`;

        let braces = 0;
        const openFunctions: number[] = [];
        let pending: 'arrow' | 'function' | null = null;
        for (let i = 0; i < body.length; i += 1) {
          const rest = body.slice(i);
          if (body.startsWith('currentTarget', i)) {
            expect(
              openFunctions.length + (pending === 'arrow' ? 1 : 0),
              `${where(i)} 的 currentTarget 必须写在回调体顶层（不得在嵌套闭包里）`,
            ).toBe(0);
          }
          if (rest.startsWith('=>')) {
            pending = 'arrow';
            i += 1;
            continue;
          }
          if (body.startsWith('function', i) && !/[\w$]/.test(body[i + 8] ?? '')) {
            pending = 'function';
            i += 7;
            continue;
          }
          if (body[i] === '{') {
            braces += 1;
            if (pending) openFunctions.push(braces);
            pending = null;
          } else if (body[i] === '}') {
            if (openFunctions[openFunctions.length - 1] === braces) openFunctions.pop();
            braces -= 1;
          } else if (pending === 'arrow' && /[;,)]/.test(body[i])) {
            /* 简洁箭头体（`=> e.preventDefault()`）在这里收尾：不增深度，但之后同一表达式里
               读 `currentTarget` 已由上面的 `pending` 判据抓住。 */
            pending = null;
          }
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
          expect(args, `${file}:${lineOf(raw, start)} 的 ${call.trim()} 实参区`).not.toContain(
            'currentTarget',
          );
          from = end + 1;
        }
      }
    }
  });
});
