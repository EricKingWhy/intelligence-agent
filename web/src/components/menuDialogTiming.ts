/** 「菜单项 → 弹窗」的初焦时序（#1694，radix dropdown-menu 2.2.1）。
 *
 *  2.2.1 下菜单关闭的焦点归还发生在弹窗初焦之后：弹窗 `onOpenAutoFocus` 里落的
 *  初焦会被菜单仍 trapped 着的 FocusScope 弹回菜单项，随后菜单卸载又把焦点丢给
 *  body（2.1.24 下弹窗初焦获胜）。`w-session-delete.spec.ts:114` 是这一条。
 *  只说现象、不宣称上游根因——两版 focus-scope 的 unmount 回调都是
 *  `setTimeout(…, 0)`，时序差异未定论，结论以 A/B 实测为准。
 *
 *  弹窗**照旧同步打开**——实测把打开推迟到菜单卸载之后，菜单层会一直占着
 *  Radix 的层栈，`Escape` 只关菜单、关不掉弹窗（`memory-management.spec.ts:645`、
 *  `t-a11y-1372.spec.ts:258` 回归）。只解焦点与指针锁，不动打开时序。
 *
 *  模块级单例而非 hook 状态：触发菜单的组件和弹窗不是同一个组件，两边要共享。
 *  同一时刻只可能有一个菜单在关，一个标志够用。
 *  前提（P3-1）：依赖「Radix 菜单每次卸载必发 `onCloseAutoFocus`」——该回调是
 *  `menuClosing` / `pendingFocus` 的唯一消费者。`openMenuDialog` 会先清 `pendingFocus`，
 *  所以即使上一轮标志没被消费，也不会把上一轮的闭包落到下一个弹窗上。
 *
 *  仓内同形先例：`SessionList` 的 `suppressMenuAutoFocus`、
 *  `TopBar` 的 `authFocusPendingRef` / `insightsFocusPendingRef`。
 */
let menuClosing = false;
let pendingFocus: (() => void) | null = null;

/** 菜单项 `onSelect` 里调用：登记本次关闭，摘掉菜单的 body 锁，并同步打开弹窗。
 *
 *  锁的归属：只在「菜单关闭窗口内」调用。这里不区分这把 `none` 是不是本次菜单加的，
 *  若有其它模态层同时锁着 body，本函数会把它一并摘掉——当前仓内不可达（顶栏菜单
 *  被模态遮罩与焦点陷阱挡住），故只把假设写死。弹窗 mount 后会加它自己那把锁，
 *  本函数不负责还原。 */
export function openMenuDialog(open: () => void): void {
  menuClosing = true;
  pendingFocus = null;
  /* 菜单的 DismissableLayer 此刻已把 body 锁成 `pointer-events:none`，而菜单与弹窗
     用的是**两份模块实例**（menu 侧 1.2.1、dialog 侧 1.1.19），弹窗会在自己那份里
     把 `none` 捕获成"原始值"，关闭时原样写回 ⇒ 之后点击全被 `<html>` 吞掉
     （`t-a11y-1372.spec.ts:258`、`s-memories.spec.ts:100` 的 30s 点击超时）。
     开弹窗前先摘掉这把锁；弹窗 mount 后会加它自己那把。 */
  if (document.body.style.pointerEvents === 'none') {
    document.body.style.pointerEvents = '';
  }
  open();
}

/** 弹窗 `onOpenAutoFocus` 里调用：本次打开由菜单触发时接管初焦，返回 true。
 *
 *  契约：`focus` 会在事件派发结束后的宏任务里执行（见 `takeMenuCloseAutoFocus`），
 *  届时 `event.currentTarget` 已按 DOM 规范置为 `null`。闭包**只能引用同步期捕获的
 *  节点**（ref、`const root = e.currentTarget`），不得跨拍读事件对象本身。 */
export function deferMenuInitialFocus(event: Event, focus: () => void): boolean {
  if (!menuClosing) return false;
  event.preventDefault();
  pendingFocus = focus;
  return true;
}

/** 菜单 Content 的 `onCloseAutoFocus`：挡掉「归还触发器」，并落被接管的初焦。
 *  没有待落初焦时（菜单项点了但弹窗没开，如面板已开时重复点同一项）不吃这次归还，
 *  返回 false 让 Radix 把焦点还给触发器。 */
export function takeMenuCloseAutoFocus(event: Event): boolean {
  if (!menuClosing) return false;
  menuClosing = false;
  const focus = pendingFocus;
  pendingFocus = null;
  if (!focus) return false;
  event.preventDefault();
  /* 此刻菜单的 FocusScope 已卸载（trapped 监听已摘），再等一拍让弹窗的 mount
     流程走完，然后落初焦。 */
  setTimeout(focus, 0);
  return true;
}

/** 弹窗内首个**真正能拿到焦点**的元素：按文档序逐个试焦，以焦点落定与否为判据。
 *
 *  与 Radix 的 `getTabbableCandidates` 同类但不等价：后者同样按文档序收集，但额外
 *  过滤 `display`/`visibility`、`disabled`、`INPUT[type=hidden]` 与 `<a>`。
 *  属性层面拦不住「可被选中但不可被聚焦」的元素——`fieldset[disabled]` 内的按钮、
 *  `display:none` 的按钮、`input[type=hidden]` 的 `tabIndex` 都是 0——所以只能试一次
 *  再看焦点是否真的挪过去（Radix 的 `focusFirst` 也是这个做法）。也正因如此，这里
 *  不另做 `disabled`/`hidden` 属性过滤：试焦已经覆盖，加了也只是重复一层。
 *  `tabIndex < 0` 仍要显式跳过——`tabindex="-1"` 的元素**能**被 `focus()` 拿到焦点。 */
const FOCUSABLE =
  'a[href], button, input, select, textarea, summary, [tabindex],' +
  ' [contenteditable]:not([contenteditable="false"])';

export function focusFirstIn(root: HTMLElement): void {
  for (const candidate of root.querySelectorAll<HTMLElement>(FOCUSABLE)) {
    if (candidate.tabIndex < 0) continue;
    candidate.focus();
    /* 焦点真的落到它身上才算成功；被拒（不可见 / 祖先 fieldset 禁用）就试下一个。 */
    if (document.activeElement === candidate) return;
  }
  /* 候选全被拒：退回容器，至少不把焦点留在菜单项上。 */
  root.focus();
}
