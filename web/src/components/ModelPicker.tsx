/** ModelPicker — 两级飞出模型选择器（#199）：一级 provider，悬停/`→` 展开二级模型。
 *
 * 为什么从「Popover + cmdk 扁平列表」改为「菜单 + 子菜单」：
 *   1. 用户裁定要 ZCode 那种**两级飞出**形态（图2/图3），并要求删掉搜索框
 *      （「才几个模型没有必要使用搜索框」）。扁平 cmdk 列表给不了两级结构。
 *   2. 两级飞出在平台上**有原生范式**：菜单 + 子菜单。它的键盘模型正好是设计稿 §3
 *      要求的那一套——`→`/Enter 进二级、`←`/Esc 回一级/关闭——因为 APG 的
 *      「menu with submenu」就是这么定义的；悬停迟滞（防抖）与 pointer-grace 也由
 *      Radix 负责，不必手写 timer + 焦点管理。
 *   3. 语义上比原来更准：二级是「从这一组里选一个」= `menuitemradio`
 *      （Radix `RadioGroup`/`RadioItem` 自动给出 `aria-checked`），而 cmdk 的
 *      `aria-selected` 原本同时表示「高亮」与「已选」两件事（FE-R11-06 早就记过这个
 *      毛病）。
 *   ⚠ 代价如实记：**`role="listbox"`/`combobox` 不再存在**，打开信号变成
 *   `[role="menu"]`。原票面约束 5 说"改结构时别破坏语义"——这里不是破坏，是换成
 *   与两级结构匹配的那套语义；e2e 的定位器与 helper 同步改（`fixtures.pickFirstModel`
 *   等），不是为了让测试变绿而弱化断言。
 *
 * 分组语义沿用 `groupByProvider()`（原实现不动）：Map 保序 ⇒ 组顺序 = 目录首次出现序。
 *
 * 数据真相仍是 /api/models（`lib/api.ts` 的 `ModelCatalogEntry`）。这里只提交偏好，
 * 不是会话内模型真相——后者仍以模型卡 `data.model` 为准（不变量 #22）。
 *
 * ⚠ 两处设计稿写了但**没有数据**、故未实现（不编占位）：
 *   - 「不可用 provider 置灰 + 行尾原因」：目录里没有 `is_available`/`unavailable_reason`
 *     字段（那是 #203 要补的），现在所有列出的 provider 都是后端已配置的；
 *   - 「能力徽标」：目录里没有能力字段。
 * 另有「管理模型」入口未做：它是 #203 的交付物，现在放上去只能是个死入口
 * （#203 落地时加在第一级底部，位置已由本票的 `.picker-foot` 插槽预留）。 */

import * as Menu from '@radix-ui/react-dropdown-menu';
import { useCallback, useMemo, useState } from 'react';
import { ChevronDown, ChevronRight, Cpu } from 'lucide-react';
import type { ModelCatalogEntry } from '../lib/api';
import { DEFAULT_VALUE, OptionRowContent } from './OptionPicker';

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

/** 二级行的次级文案：`默认` 是后端目录里的标记（有信息量），否则回退到真实 model id
 *  （仅当它和展示名不同才有信息量）。**不重复 provider 名**——二级本来就是某个
 *  provider 的展开，写它只占掉描述行。两项都没有 → 不渲染描述行（不填占位）。 */
function modelMeta(m: ModelCatalogEntry): string | undefined {
  if (m.default) return '默认';
  return m.model && m.model !== m.name ? m.model : undefined;
}

export function ModelPicker({ models, selectedModel, onModelChange, disabled = false }: Props) {
  // 受控开关：一级列表与二级子菜单的展开/收起由 Radix 管，这里只管整棵菜单的开与关。
  const [open, setOpen] = useState(false);
  const grouped = useMemo(() => groupByProvider(models), [models]);
  const selectedEntry = useMemo(
    () => (selectedModel ? models.find((m) => m.name === selectedModel) ?? null : null),
    [models, selectedModel],
  );
  const effectiveSelectedModel = selectedEntry?.name ?? null;
  const triggerLabel = selectedEntry?.name ?? '默认链';

  /** BUG-011：**弹层已关就不再接受选中**。
   *
   * 第一次选中后 `setOpen(false)`，但浮层在 `--dur-out`（150ms）退出动画期间节点
   * 仍留在 DOM 且可命中——真机上人类双击的第二次 `click`（实测间隔 ≤120ms 都会
   * 命中）会再次进入 `onSelect`，发出第二个 `POST /model`；后端两个请求各自基于
   * 同一份快照取号，写出重复 `seq`，该会话此后恒 404。
   * 关闭态下的选中一律丢弃。回归锁见 e2e/q-model-dedupe.spec.ts。 */
  const commitSelection = useCallback(
    (value: string) => {
      if (!open) return;
      onModelChange(value === DEFAULT_VALUE ? null : value);
      setOpen(false);
    },
    [open, onModelChange],
  );

  // 端点缺席：不渲染（不伪造列表）。调用方靠这个降级隐藏入口。
  if (models.length === 0) return null;

  return (
    <Menu.Root open={open} onOpenChange={setOpen}>
      <Menu.Trigger asChild>
        <button
          type="button"
          className="composer-model model-picker"
          aria-label="模型选择"
          title={triggerLabel}
          // Radix 注入 aria-haspopup="menu"/aria-expanded；aria-disabled 让 SSR 可见
          aria-disabled={disabled || undefined}
          disabled={disabled}
        >
          <Cpu size={13} className="composer-trigger-icon" aria-hidden="true" />
          <span className="composer-trigger-current">{triggerLabel}</span>
          <ChevronDown size={12} className="composer-trigger-chevron" aria-hidden="true" />
        </button>
      </Menu.Trigger>
      <Menu.Portal>
        <Menu.Content className="picker-content picker-content-root" side="top" align="end" sideOffset={6}>
          <div className="picker-head">用哪个模型？</div>
          {/* 一级：「默认链」是唯一的一级可选项（= 提交 null，后端按默认链行为）。 */}
          <Menu.Item
            className="picker-item"
            data-state={effectiveSelectedModel === null ? 'checked' : 'unchecked'}
            onSelect={() => commitSelection(DEFAULT_VALUE)}
          >
            <OptionRowContent
              title="默认链"
              description="系统自动选"
              selected={effectiveSelectedModel === null}
            />
          </Menu.Item>
          <Menu.Separator className="picker-sep" />
          {grouped.map(({ provider, items }) => {
            const isCurrentProvider = items.some((m) => m.name === effectiveSelectedModel);
            return (
              <Menu.Sub key={provider}>
                <Menu.SubTrigger
                  className="picker-item"
                  // 当前选中模型所在的 provider：一级这里只做**弱强调**（加重 + 强调色箭头），
                  // 不用「已选」那套（勾选 + 左侧条）——那是二级行的语言，复用会让
                  // "provider 被选中了"与"这个 provider 里有选中项"读成同一件事。
                  data-current={isCurrentProvider ? 'true' : undefined}
                >
                  <OptionRowContent
                    title={provider}
                    description={`${items.length} 个模型`}
                    selected={false}
                    trailing={<ChevronRight size={13} className="picker-item-trailing" aria-hidden="true" />}
                  />
                </Menu.SubTrigger>
                <Menu.Portal>
                  <Menu.SubContent className="picker-content picker-content-sub" sideOffset={6}>
                    <div className="picker-head">{provider}</div>
                    {/* 二级 = 「从这一组里选一个」：RadioGroup 给出 menuitemradio +
                        aria-checked，正是这个语义该有的角色。 */}
                    <Menu.RadioGroup value={effectiveSelectedModel ?? ''} onValueChange={commitSelection}>
                      {items.map((m) => {
                        const isSelected = effectiveSelectedModel === m.name;
                        return (
                          <Menu.RadioItem key={m.name} value={m.name} className="picker-item">
                            <OptionRowContent
                              title={m.name}
                              description={modelMeta(m)}
                              selected={isSelected}
                            />
                          </Menu.RadioItem>
                        );
                      })}
                    </Menu.RadioGroup>
                  </Menu.SubContent>
                </Menu.Portal>
              </Menu.Sub>
            );
          })}
        </Menu.Content>
      </Menu.Portal>
    </Menu.Root>
  );
}
