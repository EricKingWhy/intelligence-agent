/** #381（W-27）：进度清单渲染（PRD 长任务 §7.5 四件套，全行业收敛形态）。
 *
 * 数据源：仅 `ConversationState.plan`（`task/plan_updated` 整表覆盖投影，W-26
 * 契约）——组件不自取数、不维护第二套清单真相（不变量 #22）。零项不渲染
 * （无清单会话不留空壳，票面）。桌面同构：Electron 直接承载本组件（#344 冻结），
 * 无独立实现。
 *
 * 四件套（§7.5）：①计数「进程 M 项 · 已完成 N 项」（票面逐字）；②当前项高亮
 * （activeForm 非 content + → 前缀，调研 §zcode 实测同构）；③完成项划线 + ✓；
 * ④可折叠——当前项与未完成恒展开，完成组默认收起（zcode 实测：已完成 13 项
 * 折叠行），`hidden` 属性切换只改属性不改结构 ⇒ 节点身份存续。
 *
 * 渲染端容错（票面）：服务端已硬校验状态机（PRD §7.2），防御的只是手写/旧数据
 * ——双 in_progress 只高亮第一个；未知 status 按未完成渲染，不崩。
 *
 * reconcile（票面「无闪烁、已完成项不重新挂载」）：整表替换时 React 按
 * `key={item.id}` 对齐同一父列表 ⇒ 行 DOM 节点身份跨更新存续（测试 pin 到节点）。
 */
import { useState } from 'react';
import { Check, ChevronDown, ChevronRight } from 'lucide-react';
import type { PlanItem } from '../types';

function PlanRow({ item, current, collapsed }: { item: PlanItem; current: boolean; collapsed: boolean }) {
  const done = item.status === 'completed';
  // 当前项用 activeForm（进行时文案，Claude Code TaskCreate 同款字段语义）；
  // activeForm 缺失时投影已回落 content，这里不再兜底第二次。
  const text = current ? item.activeForm : item.content;
  return (
    <li
      className={
        'plan-list-item' +
        (done ? ' plan-list-item-done' : '') +
        (current ? ' plan-list-item-current' : '')
      }
      data-plan-id={item.id}
      data-plan-status={item.status}
      // 完成组折叠用 hidden 属性而非卸载/拆列表：节点留在同一父 <ul> 里，
      // key 对齐的 reconcile 跨状态翻转与折叠切换都不断（票面「不重新挂载」）。
      hidden={collapsed}
      // 内联 display:none 是折叠的**执行层**：hidden 的 UA 规则会被作者样式
      // `.plan-list-item { display: flex }` 盖过（workspace-panel / detail-peek
      // 注释守卫各漏过一次后，2026-10-02 真机第三次踩到）——inline style 胜过
      // 任何作者样式表，且 jsdom 断言得到，测试不再是盲区。
      style={collapsed ? { display: 'none' } : undefined}
    >
      <span className="plan-list-marker">
        {done ? <Check size={13} strokeWidth={2.5} /> : current ? '→' : '○'}
      </span>
      <span className="plan-list-text">{text}</span>
    </li>
  );
}

export function PlanList({ items }: { items: PlanItem[] }) {
  const [completedOpen, setCompletedOpen] = useState(false);
  if (items.length === 0) return null;
  const completed = items.filter((it) => it.status === 'completed');
  const currentId = items.find((it) => it.status === 'in_progress')?.id ?? null;
  return (
    <section className="plan-list" aria-label="进度清单">
      <div className="plan-list-head">
        <span className="plan-list-counter">
          进程 {items.length} 项 · 已完成 {completed.length} 项
        </span>
        {completed.length > 0 && (
          <button
            type="button"
            className="plan-list-toggle"
            aria-expanded={completedOpen}
            onClick={() => setCompletedOpen((v) => !v)}
          >
            {completedOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
            已完成 {completed.length} 项
          </button>
        )}
      </div>
      <ul className="plan-list-items">
        {items.map((it) => (
          <PlanRow
            key={it.id}
            item={it}
            current={it.id === currentId}
            collapsed={it.status === 'completed' && !completedOpen}
          />
        ))}
      </ul>
    </section>
  );
}
