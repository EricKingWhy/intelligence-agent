/** ContextProviderPicker — Radix Popover + cmdk Command 实现的多选控件。
 *
 * 复用 ControlPicker / ModelPicker 的 Popover+cmdk Command 模式，差异：
 *   - 多选语义：onSelect 切换某条目的选中态，不关闭 popover（用户可连续勾选）；
 *   - trigger 显示选中数量（如「Context · 2」），未选时显示 placeholder；
 *   - 每个条目前有 checkbox 指示选中态（lucide CheckSquare / Square）；
 *   - 空目录不渲染（不伪造列表）。
 *
 * 契约向后兼容：
 *   - class 名 `composer-control` 在 trigger 上（与 ControlPicker 同族）；
 *   - aria-label 由调用方传入；
 *   - 空目录不渲染任何节点。 */

import * as Popover from '@radix-ui/react-popover';
import { Command, CommandGroup, CommandInput, CommandItem, CommandList } from 'cmdk';
import { useMemo, useState } from 'react';
import { ChevronDown, Layers, type LucideIcon } from 'lucide-react';
import type { CatalogEntry } from '../lib/api';

interface Props {
  /** aria-label，也是 trigger title 的一部分。 */
  ariaLabel: string;
  /** 目录条目（来自 GET /api/context-providers）。空 → 返回 null（调用方隐藏入口）。 */
  entries: CatalogEntry[];
  /** 当前选中的 id 列表。空 = 未选。 */
  selectedIds: string[];
  /** 选中变更回调——传入新的完整 id 数组。 */
  onChange: (ids: string[]) => void;
  /** trigger 上显示的 icon。默认 Layers。 */
  icon?: LucideIcon;
  /** 未选中时 trigger 显示的 placeholder 文本。 */
  placeholder: string;
  disabled?: boolean;
}

export function ContextProviderPicker({
  ariaLabel,
  entries,
  selectedIds,
  onChange,
  icon: Icon = Layers,
  placeholder,
  disabled = false,
}: Props) {
  const [open, setOpen] = useState(false);

  const selectedSet = useMemo(() => new Set(selectedIds), [selectedIds]);
  const count = selectedSet.size;
  const triggerLabel = count > 0 ? `${placeholder} · ${count}` : placeholder;

  // 端点缺席：不渲染（不伪造列表）。调用方靠这个降级隐藏入口。
  if (entries.length === 0) return null;

  const toggle = (id: string) => {
    const next = new Set(selectedSet);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onChange(Array.from(next));
  };

  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        <button
          type="button"
          className="composer-control"
          aria-label={ariaLabel}
          title={triggerLabel}
          aria-disabled={disabled || undefined}
          disabled={disabled}
        >
          <Icon size={13} className="composer-control-icon" aria-hidden="true" />
          <span className="composer-control-current">{triggerLabel}</span>
          <ChevronDown size={12} className="composer-control-chevron" aria-hidden="true" />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          className="model-picker-content"
          side="top"
          align="start"
          sideOffset={6}
        >
          <Command
            label={ariaLabel}
            // 与 ControlPicker / ModelPicker 同款 includes 子串匹配。
            filter={(value, search, keywords) => {
              const q = search.trim().toLocaleLowerCase();
              if (!q) return 1;
              const haystack = [value, ...(keywords ?? [])].join(' ').toLocaleLowerCase();
              return haystack.includes(q) ? 1 : 0;
            }}
          >
            <div className="model-picker-search-wrap">
              <Icon size={13} aria-hidden="true" />
              <CommandInput placeholder="搜索…" className="model-picker-search" />
            </div>
            <CommandList>
              <CommandGroup>
                {entries.map((e) => {
                  const checked = selectedSet.has(e.id);
                  return (
                    <CommandItem
                      key={e.id}
                      value={e.id}
                      keywords={[e.display_name, e.description]}
                      className={`model-picker-item ${checked ? 'sel' : ''}`}
                      // 多选：切换选中态，不关闭 popover。
                      onSelect={() => toggle(e.id)}
                    >
                      <span
                        className="ctx-picker-checkbox"
                        role="checkbox"
                        aria-checked={checked}
                        aria-hidden="true"
                      >
                        {checked ? '☑' : '☐'}
                      </span>
                      <span className="model-picker-item-label">{e.display_name}</span>
                      <span className="model-picker-item-meta">{e.description}</span>
                    </CommandItem>
                  );
                })}
              </CommandGroup>
            </CommandList>
          </Command>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
