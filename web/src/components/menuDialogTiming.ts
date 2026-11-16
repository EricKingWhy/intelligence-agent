/** 「菜单项 → 弹窗」的初焦时序（#1694，radix dropdown-menu 2.2.1）。
 *
 *  2.2.1 起菜单比 2.1.24 晚一拍 teardown，弹窗 `onOpenAutoFocus` 里落的初焦会被
 *  菜单仍 trapped 着的 FocusScope 弹回菜单项，随后菜单卸载又把焦点丢给 body
 *  （2.1.24 下弹窗初焦获胜）。AC9 / `w-session-delete.spec.ts:114` 都是这一条。
 *
 *  弹窗**照旧同步打开**——实测把打开推迟到菜单卸载之后，菜单层会一直占着
 *  Radix 的层栈，`Escape` 只关菜单、关不掉弹窗（`memory-management.spec.ts:645`、
 *  `t-a11y-1372.spec.ts:258` 回归）。只解焦点，不动时序。
 *
 *  模块级单例而非 hook 状态：触发菜单的组件和弹窗不是同一个组件，两边要共享。
 *  同一时刻只可能有一个菜单在关，一个标志够用。
 *
 *  仓内同形先例：`SessionList` 的 `suppressMenuAutoFocus`、
 *  `TopBar` 的 `authFocusPendingRef` / `insightsFocusPendingRef`。
 */
let menuClosing = false;
let pendingFocus: (() => void) | null = null;

/** 菜单项 `onSelect` 里调用：登记本次关闭，摘掉菜单的 body 锁，并同步打开弹窗。 */
export function openMenuDialog(open: () => void): void {
  menuClosing = true;
  /* 菜单的 DismissableLayer 此刻已把 body 锁成 `pointer-events:none`，而菜单与弹窗
     用的是**两份模块实例**（menu 侧 1.2.1、dialog 侧 1.1.19），弹窗会在自己那份里
     把 `none` 捕获成"原始值"，关闭时原样写回 ⇒ 之后点击全被 `<html>` 吞掉
     （`t-a11y-1372.spec.ts:261`、`s-memories.spec.ts:100` 的 30s 点击超时）。
     开弹窗前先摘掉这把锁；弹窗 mount 后会加它自己那把。 */
  if (document.body.style.pointerEvents === 'none') {
    document.body.style.pointerEvents = '';
  }
  open();
}

/** 弹窗 `onOpenAutoFocus` 里调用：本次打开由菜单触发时接管初焦，返回 true。 */
export function deferMenuInitialFocus(event: Event, focus: () => void): boolean {
  if (!menuClosing) return false;
  event.preventDefault();
  pendingFocus = focus;
  return true;
}

/** 菜单 Content 的 `onCloseAutoFocus`：挡掉「归还触发器」，并落被接管的初焦。 */
export function takeMenuCloseAutoFocus(event: Event): boolean {
  if (!menuClosing) return false;
  menuClosing = false;
  event.preventDefault();
  const focus = pendingFocus;
  pendingFocus = null;
  /* 此刻菜单的 FocusScope 已卸载（trapped 监听已摘），再等一拍让弹窗的 mount
     流程走完，然后落初焦。 */
  if (focus) setTimeout(focus, 0);
  return true;
}

/** 弹窗内第一个可聚焦元素（对齐 Radix 默认初焦的取舍：跳过 tabindex=-1）。 */
export function focusFirstIn(root: HTMLElement): void {
  const first = root.querySelector<HTMLElement>(
    'a[href]:not([tabindex="-1"]), button:not([tabindex="-1"]):not(:disabled),' +
      ' input:not([tabindex="-1"]):not(:disabled), select:not([tabindex="-1"]):not(:disabled),' +
      ' textarea:not([tabindex="-1"]):not(:disabled), [tabindex]:not([tabindex="-1"])',
  );
  (first ?? root).focus();
}
