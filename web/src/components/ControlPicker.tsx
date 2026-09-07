/** ControlPicker — Radix Popover + cmdk Command 实现的通用单选控件。
 *
 * 复用 ModelPicker 的 Popover+cmdk Command 模式，用于 Composer control row 的
 * Permission Mode / Agent Profile / Reasoning Effort 三个单选控件。
 *
 * 与 ModelPicker 的差异：
 *   - 数据源是 CatalogEntry（{id, display_name, description}），不是 ModelCatalogEntry；
 *   - 没有 provider 分组——清单短，扁平展示即可；
 *   - 没有「默认链」item——null 选中态由 trigger placeholder 文本表达；
 *   - trigger 显示 display_name，不是 id。
 *
 * 契约向后兼容：
 *   - class 名 `composer-control` 在 trigger 上；
 *   - aria-label 由调用方传入；
 *   - 空目录不渲染任何节点。 */

import * as Popover from '@radix-ui/react-popover';
import { Command, CommandGroup, CommandInput, CommandItem, CommandList } from 'cmdk';
import { useMemo, useState } from 'react';
import { Check, ChevronDown, type LucideIcon } from 'lucide-react';
import type { CatalogEntry } from '../lib/api';

interface Props {
  /** aria-label，也是 trigger title 的一部分。 */
  ariaLabel: string;
  /** 目录条目（来自端点）。空 → 返回 null（调用方隐藏入口）。 */
  entries: CatalogEntry[];
  /** 当前选中 id（null = 未选/默认）。 */
  selectedId: string | null;
  /** 选中回调；null 表示选了「默认」或取消选中。 */
  onChange: (id: string | null) => void;
  /** trigger 上显示的 icon。 */
  icon: LucideIcon;
  /** 未选中时 trigger 显示的 placeholder 文本。 */
  placeholder: string;
  disabled?: boolean;
}

export function ControlPicker({
  ariaLabel,
  entries,
  selectedId,
  onChange,
  icon: Icon,
  placeholder,
  disabled = false,
}: Props) {
  const [open, setOpen] = useState(false);
  const selectedEntry = useMemo(
    () => (selectedId ? entries.find((e) => e.id === selectedId) ?? null : null),
    [entries, selectedId],
  );
  const triggerLabel = selectedEntry?.display_name ?? placeholder;

  // 端点缺席：不渲染（不伪造列表）。调用方靠这个降级隐藏入口。
  if (entries.length === 0) return null;

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
          <Command label={ariaLabel}>
            <div className="model-picker-search-wrap">
              <Icon size={13} aria-hidden="true" />
              <CommandInput placeholder="搜索…" className="model-picker-search" />
            </div>
            <CommandList>
              <CommandGroup>
                {entries.map((e) => (
                  <CommandItem
                    key={e.id}
                    value={e.id}
                    keywords={[e.display_name, e.description]}
                    className={`model-picker-item ${selectedId === e.id ? 'sel' : ''}`}
                    onSelect={(value) => {
                      onChange(value);
                      setOpen(false);
                    }}
                  >
                    <span className="model-picker-item-label">{e.display_name}</span>
                    <span className="model-picker-item-meta">{e.description}</span>
                    {selectedId === e.id && <Check size={13} className="model-picker-check" aria-hidden="true" />}
                  </CommandItem>
                ))}
              </CommandGroup>
            </CommandList>
          </Command>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
