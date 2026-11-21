/** CommandPalette — Ctrl/Cmd+K 命令面板（PRD §15，ADR-0014）。
 *
 * Raycast/Linear 式：中心浮层、keyboard-first、fuzzy 过滤（lib/commands）。
 * Radix Dialog 承担浮层语义（焦点陷阱 / Esc / backdrop）；列表与选择自绘。
 * 命令集 = 静态动作 + 动态事件项（Search Runtime Events），由 App 组装传入。
 * backdrop blur 只用于该浮层（PRD §15 / 冻结视觉原则）。
 */

import { Fragment, useEffect, useMemo, useRef, useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import {
  Activity,
  ArrowDownToLine,
  Brain,
  Command,
  Copy,
  ExternalLink,
  LayoutList,
  Maximize2,
  PanelRight,
  Plus,
  Search,
  ShieldCheck,
  SunMoon,
  TextCursorInput,
  type LucideIcon,
} from 'lucide-react';
import { filterCommands, type CommandItem } from '../lib/commands';

/** 分组标题以 App.tsx 实际传入的 group 为准（actions / density / events）。 */
const GROUP_LABELS: Record<CommandItem['group'], string> = {
  actions: '操作',
  density: '密度',
  events: '运行事件',
};

/** 行首图标：按命令 id 给最接近的语义；未来新增命令回落到所属分组的图标。 */
const COMMAND_ICONS: Record<string, LucideIcon> = {
  'toggle-inspector': PanelRight,
  'toggle-inspector-fullpage': Maximize2,
  'new-session': Plus,
  'jump-latest': ArrowDownToLine,
  'copy-run-id': Copy,
  'copy-trace-id': Copy,
  'open-trace': ExternalLink,
  'toggle-theme': SunMoon,
  'focus-composer': TextCursorInput,
  'manage-memories': Brain,
  'manage-approve-policy': ShieldCheck,
};
const GROUP_ICONS: Record<CommandItem['group'], LucideIcon> = {
  actions: Command,
  density: LayoutList,
  events: Activity,
};

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  items: CommandItem[];
  /** 关闭**之后**执行一次的动作（当前有「聚焦输入框」/「新建会话」两处调用）；**返回 `true` = 已接管落点**。
   *
   *  为什么需要这个缝：命令是在浮层**还开着**的时候跑 `run()` 的，而 Radix 关闭时会把焦点
   *  还给"打开前的元素"——于是命令里那句 `composer-input.focus()` 当场被这次焦点恢复打断，
   *  用户看到的是"点了没反应 + 打的字进不去"。同一个坑在 `App.tsx` 的 `createEmptySession`
   *  已经踩过一次并写明"放在浮层关闭之后……否则会被浮层的关闭焦点打断"，这里是**同一机制的
   *  第二处入口**。所以把动作挂在 `onCloseAutoFocus`（Radix 唯一的关闭焦点钩子）上。
   *
   *  **只有真的登记了落点才接管**：普通 Esc / Ctrl+K 关闭时默认恢复（还给打开前的元素，
   *  通常正是 composer）必须保留，否则焦点会掉到 body。 */
  onAfterClose?: () => boolean | void;
}

export function CommandPalette({ open, onOpenChange, items, onAfterClose }: Props) {
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const listRef = useRef<HTMLDivElement>(null);
  /* #1706：每个「打开周期」换一个 key，强制重新挂载 Radix 子树。
   *
   * 缺陷只在「关闭动画（`--dur-out` 150ms）没走完又打开」这一条路径上出现：Radix
   * `Presence` 在退出期间复用原节点，于是
   *   1. body 子节点顺序不变 —— 命令面板仍排在常驻管理面板（`.memory-panel` 等，
   *      同为 `position: fixed; z-index: 51`）之前，命令项被面板头部盖住、点不到；
   *   2. `DismissableLayer` 的图层栈也停在旧的注册位 —— 记忆面板仍被当成顶层模态，
   *      把「点在命令面板里」判成 outside 而关掉自己（点击命令项后记忆面板消失）。
   * 重新挂载 = 节点重新 append 到 body 末尾、图层重新入栈，两个症状一起消失；
   * CSS 侧的 52/53 抬层只负责让新节点确实压在上面板层族之上。
   *
   * 用「渲染期调整 state」（React 官方派生状态写法：本次渲染被丢弃并立即重渲）而不是
   * effect：effect 会在 open 的第一次提交之后才跑，那会先挂一个旧 key 的子树再换掉它，
   * 多出一次挂载/卸载。 */
  const [openCycle, setOpenCycle] = useState(0);
  const [previousOpen, setPreviousOpen] = useState(open);
  if (open !== previousOpen) {
    setPreviousOpen(open);
    if (open) setOpenCycle((n) => n + 1);
  }

  const results = useMemo(() => filterCommands(items, query).slice(0, 50), [items, query]);

  // 打开时重置输入与选择
  useEffect(() => {
    if (open) {
      setQuery('');
      setActive(0);
    }
  }, [open]);

  // query/结果变化时选择归零；active 越界收敛
  useEffect(() => {
    setActive((a) => Math.min(a, Math.max(0, results.length - 1)));
  }, [results.length]);

  // 键盘：↑↓ 移动、Enter 执行（Esc/backdrop 由 Radix 处理）
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActive((a) => (results.length ? (a + 1) % results.length : 0));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActive((a) => (results.length ? (a - 1 + results.length) % results.length : 0));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const cmd = results[active];
      if (cmd) {
        onOpenChange(false);
        cmd.run();
      }
    }
  };

  return (
    <Dialog.Root key={openCycle} open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        {/* #1706：命令面板是瞬时操作面，必须压过常驻管理面板（`.project-dialog` /
            `.memory-panel` / … 都是 z-index 51）。裸 `.palette-overlay` / `.palette-content`
            被全仓 13+ 处 Dialog 共用，直接改它们的数值会把所有弹窗的遮罩一起抬到自身内容
            之上（#203：`inset:0` 遮罩接走指针事件）。所以在**本组件**上加修饰类，只抬这一份；
            否则两者同为 51 时由 body 下的 DOM 顺序决定谁在上，命令项会被面板头部盖住
            （#1706 根因）。遮罩与内容一起抬，保持 52/53 配对（内容仍高于遮罩）。 */}
        <Dialog.Overlay className="palette-overlay palette-overlay-raised" />
        <Dialog.Content
          className="palette-content palette-content-raised"
          aria-label="命令面板"
          onKeyDown={onKeyDown}
          onCloseAutoFocus={(e) => {
            // 默认行为 = 把焦点还给"打开前的元素"（Ctrl+K 时通常正是 composer）。
            // 只有命令登记了落点时（返回 true）才拦下默认恢复；普通关闭不拦，
            // 免得焦点掉到 body。见 Props。
            if (onAfterClose?.() === true) e.preventDefault();
          }}
        >
          <Dialog.Title className="palette-title">命令面板</Dialog.Title>
          <div className="palette-input-row">
            <Search size={14} className="palette-search-icon" />
            <input
              className="palette-input"
              placeholder="搜索命令或运行事件…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              autoFocus
              aria-label="搜索命令"
            />
          </div>
          <div className="palette-list" ref={listRef} role="listbox" aria-label="命令列表">
            {results.length === 0 && (
              <div className="palette-empty">无匹配命令</div>
            )}
            {results.map((cmd, i) => {
              const Icon = COMMAND_ICONS[cmd.id] ?? GROUP_ICONS[cmd.group];
              const showGroup = i === 0 || results[i - 1].group !== cmd.group;
              return (
                <Fragment key={cmd.id}>
                  {showGroup && (
                    <div className="palette-group" role="presentation">
                      {GROUP_LABELS[cmd.group]}
                    </div>
                  )}
                  <button
                    role="option"
                    aria-selected={i === active}
                    className={`palette-item palette-item-${cmd.group}${i === active ? ' active' : ''}`}
                    onMouseEnter={() => setActive(i)}
                    onClick={() => {
                      onOpenChange(false);
                      cmd.run();
                    }}
                  >
                    <Icon size={14} className="palette-item-icon" aria-hidden="true" />
                    <span className="palette-item-label">{cmd.label}</span>
                    {cmd.meta && <span className="palette-item-meta">{cmd.meta}</span>}
                  </button>
                </Fragment>
              );
            })}
          </div>
          <div className="palette-footer">
            <span>↑↓ 选择</span>
            <span>Enter 执行</span>
            <span>Esc 关闭</span>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
