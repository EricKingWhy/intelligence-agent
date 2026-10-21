/** SkipLinks — 键盘用户的第一站（#1372 改动 2）。
 *
 *  三个 region（会话区 / 主工作区 / Inspector）+ composer 的跳转入口放在 Tab 序首位：
 *  `position: absolute` 的链接平时不可见，`:focus` 时才出现（WCAG 2.4.1 Bypass Blocks）。
 *  跳转后焦点落到 region 容器（`tabindex="-1"`），下一次 Tab 从该区域内部继续。
 *  只渲染**当前真的能落焦**的目标：Inspector 收起（`visibility:hidden`）、
 *  自动值班/Inspector 全屏（main 或 rail/workspace 隐藏）、非 chat 面（composer
 *  在 hidden 面板里）时，对应链接不渲染——否则按了没反应就是死路。
 *
 *  为什么多一个「跳到输入框」：验收要求「首焦点到 composer ≤10 次按键」。
 *  主区里还有 6 个 workspace tab 与消息卡片按钮，纯 Tab 不可控；composer 是
 *  本产品最高频的落点，给它一条直达键是同一个无障碍目标的最小实现，不是新增功能。
 */

const TARGETS = [
  { id: 'region-sessions', label: '跳到会话区' },
  { id: 'region-main', label: '跳到主工作区' },
  { id: 'region-inspector', label: '跳到 Inspector', requiresInspector: true },
  { id: 'composer-input', label: '跳到输入框' },
] as const;

export function SkipLinks({
  inspectorVisible,
  regionsVisible,
  composerVisible,
}: {
  inspectorVisible: boolean;
  /** 会话区 + 主工作区可落焦（自动值班 / Inspector 全屏时 main 隐藏）。 */
  regionsVisible: boolean;
  /** composer 可落焦（chat 面可见且未因待审批 / 待回答而禁用）。 */
  composerVisible: boolean;
}) {
  // 区域仍是 mounted，但隐藏时 focus() 会静默失败——指向看不见、也到不了的地方
  // 没有意义；状态变化时链接随出现/消失（region 本身不卸载）。
  const targets = TARGETS.filter((t) => {
    if ('requiresInspector' in t) return inspectorVisible;
    if (t.id === 'composer-input') return composerVisible;
    return regionsVisible;
  });
  // 四目标全隐藏（自动值班）时不留一个空 landmark。
  if (targets.length === 0) return null;
  return (
    <nav className="skip-links" aria-label="跳转">
      {targets.map(({ id, label }) => (
        <a
          key={id}
          className="skip-link"
          href={`#${id}`}
          onClick={(event) => {
            event.preventDefault();
            document.getElementById(id)?.focus();
          }}
        >
          {label}
        </a>
      ))}
    </nav>
  );
}
