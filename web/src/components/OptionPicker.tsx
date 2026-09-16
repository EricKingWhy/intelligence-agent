/** OptionPicker — Composer 控制行共用的单选档位控件（#201：合并 ControlPicker +
 *  ContextProviderPicker，并把三处手抄收敛成一份实现）。
 *
 * 为什么合并：此前三个下拉各自手搓一份 Radix Popover + cmdk——
 * `DEFAULT_VALUE` 常量在 `ModelPicker` / `ControlPicker` 各写一遍、cmdk 的 `filter`
 * 回调三份逐字复制、弹层容器类名三处共用却叫 `model-picker-content`；随之漂移出十条
 * 不一致（选中指示有的用 `Check` 有的用 `☑/☐`、多选设 `aria-checked` 而单选只靠
 * `aria-selected`、搜索阈值两种口径、弹层 `align` 两种、可访问名中英混用）。
 * 合并后只剩一份实现、一份视觉、一套 ARIA。
 *
 * 结构（设计稿 §4）：
 *   面板头部（一句问句标题——**不放外链**：本仓没有对应文档页，不造死链）
 *   ├ 搜索框（目录 > 5 条才显示；阈值与 `picker-search-visibility` 契约一致）
 *   ├ 首行「默认（未选）」（单选必须能回到未选，FE-R11-05）
 *   └ 选项行：标题（13px）+ 描述（12px，2 行内截断）+ 选中标记
 *   面板底部：`footer` 插槽（调用方放说明性内容）
 *
 * 选中态**三通道**（只靠颜色不足以表达选中，设计稿 §7）：勾选图标 + 标题加重 +
 * 左侧 2px accent 条（以 DESIGN.md 允许的「accent 半透明 2px 签名条」这一既有形态
 * 实现，用伪元素而不是 `border-left`——craft-floor 把 >1px 的彩色 border-left 列为
 * 默认该拒绝的形态）。
 *
 * ⚠ **没有**设计稿 §4 写的「每行 20px 图标槽」：三份目录的条目契约是
 * `CatalogEntry = {id, display_name, description}`，**没有任何 per-option 图标数据**；
 * 而为后端可扩展的枚举（fixture 里就有 `mode-0…mode-5`）编一套字形，正是本产品明令
 * 禁止的「编占位」行为（PRODUCT.md 原则 3「真实优先于好看」/ PRD §4 No fake values）。
 * 图标留在 trigger 上——那是每个控件一个（调用方传入），不是每行一个。设计稿 §4 的
 * 「档位收窄提示」已落地（#201）：`GET /api/agent-profiles` 自 #201 起回
 * `tool_scope {open, total, excluded}`，调用方（`Composer.tsx` 的档位 picker）把
 * `lib/agentProfileScope.ts` 组装的文案塞进 `footer` 插槽——本组件不认识那段话的语义，
 * 只负责位置与样式（没被收窄时不传 footer，就没有这一行）。
 *
 * 契约（向后兼容）：
 *   - trigger 仍是 `button.composer-control`，`aria-label` 由调用方传入——组件**不内置**
 *     默认值（三处默认值打架正是原来 e2e 定位器误伤的来源）；
 *   - 空目录不渲染任何节点（不伪造列表）；
 *   - 搜索框恒在 DOM（cmdk 要求 `CommandInput` 不卸载），短目录由 CSS 隐藏。 */

import * as Popover from '@radix-ui/react-popover';
import { Command, CommandGroup, CommandInput, CommandItem, CommandList } from 'cmdk';
import { useMemo, useRef, useState, type ReactNode } from 'react';
import { Check, ChevronDown, type LucideIcon } from 'lucide-react';
import type { CatalogEntry } from '../lib/api';
import { focusPickerListOnOpen } from '../lib/pickerFocus';

/** 一行选项的数据。`description` 为空 → 不渲染描述行（不填占位文案）。 */
export interface Option {
  value: string;
  title: string;
  description?: string;
}

/** `CatalogEntry[]` → `Option[]`（三份档位目录共用的唯一映射点，前端零硬编码文案）。 */
export function toCatalogOptions(entries: CatalogEntry[]): Option[] {
  return entries.map((e) => ({
    value: e.id,
    title: e.display_name,
    description: e.description.length > 0 ? e.description : undefined,
  }));
}

/** cmdk Item value 必须唯一、稳定（不依赖 textContent）。null 选中态用 sentinel。 */
export const DEFAULT_VALUE = '__default__';

/** 选项行内容（不含交互元素本身）——**ModelPicker 的第二级复用同一份实现**，
 *  避免"抽出行组件"变成"复制一份视觉"（#201 的明确要求）。 */
export function OptionRowContent({
  title,
  description,
  selected,
  trailing,
}: {
  title: string;
  description?: string;
  selected: boolean;
  /** 行尾附加内容（例如二级菜单的 `▸`）。放在选中标记之前。 */
  trailing?: ReactNode;
}) {
  return (
    <>
      <span className="picker-item-text">
        <span className="picker-item-title">{title}</span>
        {description ? <span className="picker-item-desc">{description}</span> : null}
      </span>
      {trailing}
      {selected ? <Check size={13} className="picker-item-check" aria-hidden="true" /> : null}
    </>
  );
}

interface Props {
  /** `aria-label`（也是 trigger 的 `title`）。由调用方传入。 */
  ariaLabel: string;
  /** 面板头部的问句标题（这个下拉在决定什么）。 */
  title: string;
  icon: LucideIcon;
  /** 未选中时 trigger 上的文案。 */
  placeholder: string;
  /** 目录条目；空 → 返回 null（调用方据此隐藏入口）。 */
  options: Option[];
  /** 当前选中 value；`null` = 未选（= 后端默认值）。 */
  value: string | null;
  /** 选中回调；`null` 表示选了「默认（未选）」。 */
  onChange: (value: string | null) => void;
  /** 面板底部插槽（说明性内容，例如未来的档位收窄提示）。 */
  footer?: ReactNode;
  disabled?: boolean;
}

export function OptionPicker({
  ariaLabel,
  title,
  icon: Icon,
  placeholder,
  options,
  value,
  onChange,
  footer,
  disabled = false,
}: Props) {
  const [open, setOpen] = useState(false);
  const selected = useMemo(
    () => (value ? options.find((o) => o.value === value) ?? null : null),
    [options, value],
  );
  // 选中值不在目录里（目录换了）→ 归一化为「默认（未选）」：与 trigger 文案同一口径。
  const effectiveValue = selected?.value ?? null;
  // #198 次方案：选中非默认档位时 trigger 的 hover title 附上**后端下发的**条目
  // 描述（`研究审查\n只读：不含 write / edit…`）——零视觉占位，文案零前端硬编码
  // （把 tool_scope 写成前端常量就是抄第二份知识，profiles.py 一改就漂移）。
  const triggerTitle = selected ? `${selected.title}\n${selected.description ?? ''}`.trimEnd() : ariaLabel;
  const triggerLabel = selected?.title ?? placeholder;
  const searchHidden = options.length <= 5; // 阈值与 picker-search-visibility 契约一致
  const listRef = useRef<HTMLDivElement>(null);

  // 端点缺席：不渲染（不伪造列表）。调用方靠这个降级隐藏入口。
  if (options.length === 0) return null;

  /** 关闭态下的选中一律丢弃（与 ModelPicker 同一条 BUG-011 守卫）：浮层退出动画期间
   *  节点仍在 DOM 且可命中，第二次点击会再发一次请求。 */
  const commit = (next: string) => {
    if (!open) return;
    onChange(next === DEFAULT_VALUE ? null : next);
    setOpen(false);
  };

  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        <button
          type="button"
          className="composer-control"
          aria-label={ariaLabel}
          title={triggerTitle}
          // Radix Popover 注入 aria-haspopup/aria-expanded；aria-disabled 让 SSR 可见
          aria-disabled={disabled || undefined}
          disabled={disabled}
        >
          <Icon size={13} className="composer-trigger-icon" aria-hidden="true" />
          <span className="composer-trigger-current">{triggerLabel}</span>
          <ChevronDown size={12} className="composer-trigger-chevron" aria-hidden="true" />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          className="picker-content"
          side="top"
          align="start"
          sideOffset={6}
          // FE-R11-04：短目录搜索框不可见 → 焦点交给 listbox，键盘导航才有效
          onOpenAutoFocus={focusPickerListOnOpen(listRef, searchHidden)}
        >
          <Command
            label={ariaLabel}
            // includes 子串匹配：cmdk 默认 command-score 与旧行为不一致（三处原本逐字复制）
            filter={(val, search, keywords) => {
              const q = search.trim().toLocaleLowerCase();
              if (!q) return 1;
              const haystack = [val, ...(keywords ?? [])].join(' ').toLocaleLowerCase();
              return haystack.includes(q) ? 1 : 0;
            }}
          >
            <div className="picker-head">{title}</div>
            <div className={`picker-search-wrap${searchHidden ? ' hidden' : ''}`}>
              <Icon size={13} aria-hidden="true" />
              <CommandInput placeholder="搜索…" className="picker-search" />
            </div>
            <CommandList ref={listRef}>
              <CommandGroup>
                {/* FE-R11-05：单选必须能回到「没选」——否则选了就再也退不回来
                    （只能整页 reload）。提交 null，与 trigger placeholder 同义。 */}
                <CommandItem
                  value={DEFAULT_VALUE}
                  keywords={['默认', '未选', 'default', 'none']}
                  className="picker-item"
                  // 选中态走 `data-state`，**不能**用 `data-selected`：cmdk 的 Item 把调用方
                  // 属性先展开、再用自己的高亮值覆盖 `data-selected`（dist 里 `...q` 之后紧跟
                  // `"data-selected":!!R`），所以那个属性只表示「键盘高亮」；选中标记由
                  // `.picker-item[data-state="checked"]` 提供（Radix 的 RadioItem 也正好是它）。
                  data-state={effectiveValue === null ? 'checked' : 'unchecked'}
                  onSelect={commit}
                >
                  <OptionRowContent
                    title="默认（未选）"
                    description="用后端默认值"
                    selected={effectiveValue === null}
                  />
                </CommandItem>
                {options.map((o) => {
                  const isSelected = effectiveValue === o.value;
                  return (
                    <CommandItem
                      key={o.value}
                      value={o.value}
                      keywords={[o.title, o.description ?? '']}
                      className="picker-item"
                      data-state={isSelected ? 'checked' : 'unchecked'}
                      onSelect={commit}
                    >
                      <OptionRowContent title={o.title} description={o.description} selected={isSelected} />
                    </CommandItem>
                  );
                })}
              </CommandGroup>
            </CommandList>
            {footer ? <div className="picker-foot">{footer}</div> : null}
          </Command>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
