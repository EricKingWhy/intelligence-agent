/** CommandPalette — Ctrl/Cmd+K 命令面板（PRD §15，ADR-0014）。
 *
 * Raycast/Linear 式：中心浮层、keyboard-first、fuzzy 过滤（lib/commands）。
 * Radix Dialog 承担浮层语义（焦点陷阱 / Esc / backdrop）；列表与选择自绘。
 * 命令集 = 静态动作 + 动态事件项（Search Runtime Events），由 App 组装传入。
 * backdrop blur 只用于该浮层（PRD §15 / 冻结视觉原则）。
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { Search } from 'lucide-react';
import { filterCommands, type CommandItem } from '../lib/commands';

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  items: CommandItem[];
  /** 关闭**之后**执行一次的动作（当前只有「聚焦输入框」用）；**返回 `true` = 已接管落点**。
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
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="palette-overlay" />
        <Dialog.Content
          className="palette-content"
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
            {results.map((cmd, i) => (
              <button
                key={cmd.id}
                role="option"
                aria-selected={i === active}
                className={`palette-item palette-item-${cmd.group}${i === active ? ' active' : ''}`}
                onMouseEnter={() => setActive(i)}
                onClick={() => {
                  onOpenChange(false);
                  cmd.run();
                }}
              >
                <span className="palette-item-label">{cmd.label}</span>
                {cmd.hint && <span className="palette-item-hint">{cmd.hint}</span>}
              </button>
            ))}
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
