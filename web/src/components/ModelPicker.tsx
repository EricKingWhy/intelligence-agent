/** ModelPicker — Radix DropdownMenu 实现的模型选择器（Phase 2a）。
 *
 * 替换 Composer 里原生 <select>。设计参考 Linear / Vercel 的模型选择器：
 *   - 紧凑 trigger（icon + 当前选中 + chevron），不占太多 Composer 宽度；
 *   - 打开为 Radix DropdownMenu（portal），按 provider 分组，展示默认标记；
 *   - 「默认链」永远在顶部（null 选中）；
 *   - 目录缺席（models 空）→ 组件返回 null（调用方据此隐藏入口），不伪造列表；
 *   - 键盘、焦点陷阱、Esc 关闭、外点关闭由 Radix 负责——不自造浮层。
 *
 * 契约向后兼容（Composer.test.tsx 的 SSR 断言）：
 *   - class 名 `composer-model` 仍在 trigger 上；
 *   - aria-label="模型选择"；
 *   - 空目录不渲染任何节点。
 *
 * 数据真相仍是 /api/models（lib/api.ts 的 ModelCatalogEntry）。
 * 这里只是提交偏好，不是会话内模型真相——后者仍以模型卡 data.model 为准（不变量 #22）。 */

import { useMemo } from 'react';
import * as DropdownMenu from '@radix-ui/react-dropdown-menu';
import { Check, ChevronDown, Cpu } from 'lucide-react';
import type { ModelCatalogEntry } from '../lib/api';

interface Props {
  models: ModelCatalogEntry[];
  selectedModel: string | null;
  onModelChange: (name: string | null) => void;
  disabled?: boolean;
}

/** 按 provider 分组（null/空 → 「其他」组）。Map 保留插入序。 */
function groupByProvider(models: ModelCatalogEntry[]): { provider: string; items: ModelCatalogEntry[] }[] {
  const groups = new Map<string, ModelCatalogEntry[]>();
  for (const m of models) {
    const key = m.provider && m.provider.length > 0 ? m.provider : '其他';
    const arr = groups.get(key) ?? [];
    arr.push(m);
    groups.set(key, arr);
  }
  return Array.from(groups, ([provider, items]) => ({ provider, items }));
}

export function ModelPicker({ models, selectedModel, onModelChange, disabled = false }: Props) {
  // Hooks 永远在最前（rules-of-hooks）—— 空目录降级在 Hooks 之后。
  const grouped = useMemo(() => groupByProvider(models), [models]);
  const selectedEntry = useMemo(
    () => (selectedModel ? models.find((m) => m.name === selectedModel) ?? null : null),
    [models, selectedModel],
  );
  const triggerLabel = selectedEntry?.name ?? '默认链';

  // 端点缺席：不渲染（不伪造列表）。调用方靠这个降级隐藏入口。
  if (models.length === 0) return null;

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger
        asChild
        disabled={disabled}
      >
        <button
          type="button"
          className="composer-model model-picker"
          aria-label="模型选择"
          title={triggerLabel}
          // Radix 会注入 aria-haspopup/aria-expanded；aria-disabled 让 SSR 可见
          aria-disabled={disabled || undefined}
        >
          <Cpu size={13} className="model-picker-icon" aria-hidden="true" />
          <span className="model-picker-current">{triggerLabel}</span>
          <ChevronDown size={12} className="model-picker-chevron" aria-hidden="true" />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          className="model-picker-content"
          side="top"
          align="end"
          sideOffset={6}
          // 高度上限 + 滚动，避免目录长时顶出视口
          // (max-height 由 CSS 处理)
        >
          {/* 默认链永远在顶部（null 提交——后端按默认链行为） */}
          <DropdownMenu.Item
            className={`model-picker-item ${selectedModel === null || selectedModel === '' ? 'sel' : ''}`}
            onSelect={(e) => {
              e.preventDefault();
              onModelChange(null);
            }}
          >
            <span className="model-picker-item-label">默认链</span>
            <span className="model-picker-item-meta">系统自动选</span>
            {(selectedModel === null || selectedModel === '') && <Check size={13} className="model-picker-check" aria-hidden="true" />}
          </DropdownMenu.Item>
          {grouped.map(({ provider, items }) => (
            <DropdownMenu.Group key={provider}>
              <DropdownMenu.Label className="model-picker-group-label">{provider}</DropdownMenu.Label>
              {items.map((m) => (
                <DropdownMenu.Item
                  key={m.name}
                  className={`model-picker-item ${selectedModel === m.name ? 'sel' : ''}`}
                  onSelect={(e) => {
                    e.preventDefault();
                    onModelChange(m.name);
                  }}
                >
                  <span className="model-picker-item-label">{m.name}</span>
                  <span className="model-picker-item-meta">
                    {m.default ? '默认' : m.model && m.model !== m.name ? m.model : ''}
                  </span>
                  {selectedModel === m.name && <Check size={13} className="model-picker-check" aria-hidden="true" />}
                </DropdownMenu.Item>
              ))}
            </DropdownMenu.Group>
          ))}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
