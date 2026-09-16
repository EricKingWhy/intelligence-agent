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
 * ⚠ 曾经"没有可用数据、故未实现"的两处，**#199 已落地**（数据面由 #203 补齐）：
 *   - 「不可用 provider 置灰 + 行尾原因」：`/api/models` 的 `is_available` 在 #203 之后
 *     是真实判定（有凭据 ⇒ true），`unavailable_reason` 也在。置灰的是**整组都不可用**
 *     的 provider，且**仍可展开**（用户看得到里面有什么，只是选了会用不了），行尾给一句
 *     原因短文案。口径是纯函数（`lib/modelAvailability.ts`，单测直测）。
 *   - 「能力徽标」：同样落地——**只在后端声明为 true 时**出徽标（`false` 与"未声明"都不出，
 *     灰徽标会被读成"不支持"，而未声明时我们并不知道）。
 * 另有「管理模型」入口：#203 交付物（后端 CRUD + 凭据管理器已落地）——经
 * `.picker-foot` 同族槽位渲染，点击打开 ProviderManagerDialog（两栏弹层），
 * 不再是死入口。 */

import * as Menu from '@radix-ui/react-dropdown-menu';
import { useCallback, useMemo, useState } from 'react';
import { ChevronDown, ChevronRight, Cpu, Settings2 } from 'lucide-react';
import type { ModelCatalogEntry } from '../lib/api';
import { capabilityBadges, isUnavailable, providerAvailability, reasonLabel } from '../lib/modelAvailability';
import { DEFAULT_VALUE, OptionRowContent } from './OptionPicker';
import { ProviderManagerDialog } from './ProviderManagerDialog';

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
  // #203：管理模型弹层（在菜单里点击入口后打开；菜单先关闭——两层浮层叠放
  // 会互相抢 Esc/焦点，先关菜单再开弹层）。
  const [manageOpen, setManageOpen] = useState(false);
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
        <Menu.Content className="picker-content" side="top" align="end" sideOffset={6}>
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
            const availability = providerAvailability(items);
            return (
              <Menu.Sub key={provider}>
                <Menu.SubTrigger
                  className="picker-item"
                  // 当前选中模型所在的 provider：一级这里只做**弱强调**（加重 + 强调色箭头），
                  // 不用「已选」那套（勾选 + 左侧条）——那是二级行的语言，复用会让
                  // "provider 被选中了"与"这个 provider 里有选中项"读成同一件事。
                  data-current={isCurrentProvider ? 'true' : undefined}
                  // #199：整组不可用 → 置灰但**仍可展开**（拿掉 hover/键盘可达性会让
                  // 用户看不到里面有什么，而"组里有什么模型"仍然是事实）。
                  data-unavailable={availability.unavailable ? 'true' : undefined}
                >
                  <OptionRowContent
                    title={provider}
                    description={`${items.length} 个模型`}
                    selected={false}
                    trailing={
                      // 行尾优先给原因：不可用时"为什么不能用"比"有几个模型"更要紧，
                      // 两个都放会把这一行挤成一团（设计稿：行尾只挂一个信息）。
                      availability.unavailable ? (
                        <span className="picker-item-note">
                          {reasonLabel(availability.reason)}
                        </span>
                      ) : (
                        <ChevronRight size={13} className="picker-item-trailing" aria-hidden="true" />
                      )
                    }
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
                        const badges = capabilityBadges(m);
                        // 行尾的不可用原因**必须写出来**，不能只靠置灰：颜色是唯一
                        // 区别的话，色觉障碍 / 打印 / 截图里这一行就退化成"和别的
                        // 一样"，而用户要的是"为什么不能用"。一级行同理（见上）。
                        const unavailable = isUnavailable(m);
                        const reason = unavailable ? reasonLabel(m.unavailableReason) : null;
                        return (
                          <Menu.RadioItem
                            key={m.name}
                            value={m.name}
                            className="picker-item"
                            data-unavailable={unavailable ? 'true' : undefined}
                            // 不可用的模型仍然可点：后端会给出明确失败（provider_store
                            // 的凭据错误），前端在这里**不替后端 decide**。点击后的失败
                            // 由既有的错误通路呈现，语义比"点了没反应"清楚。
                          >
                            <OptionRowContent
                              title={m.name}
                              description={modelMeta(m)}
                              selected={isSelected}
                              trailing={
                                reason || badges.length > 0 ? (
                                  // 复用同族的行尾簇容器（flex + gap）装"原因 + 徽标"，
                                  // 两者可以同时在：不可用与能力各自是事实。
                                  <span className="picker-badges">
                                    {reason ? <span className="picker-item-note">{reason}</span> : null}
                                    {badges.map((b) => (
                                      <span key={b.key} className="picker-badge">
                                        {b.label}
                                      </span>
                                    ))}
                                  </span>
                                ) : undefined
                              }
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
          <Menu.Separator className="picker-sep" />
          {/* #203：「管理模型」入口（两栏弹层）。onSelect 里先关菜单再开弹层：
              两层浮层叠放会互相抢 Esc 与焦点；先关菜单让弹层独占浮层栈。 */}
          <Menu.Item
            className="picker-item picker-manage-item"
            onSelect={() => {
              setOpen(false);
              setManageOpen(true);
            }}
          >
            <OptionRowContent
              title="管理模型"
              description="自定义供应商 · API Key · 测试连接"
              selected={false}
              trailing={<Settings2 size={13} className="picker-item-trailing" aria-hidden="true" />}
            />
          </Menu.Item>
        </Menu.Content>
      </Menu.Portal>
      <ProviderManagerDialog open={manageOpen} onOpenChange={setManageOpen} />
    </Menu.Root>
  );
}
