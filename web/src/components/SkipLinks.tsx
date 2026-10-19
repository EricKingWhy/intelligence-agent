/** SkipLinks — 键盘用户的第一站（#1372 改动 2）。
 *
 *  三个 region（会话区 / 主工作区 / Inspector）+ composer 的跳转入口放在 Tab 序首位：
 *  `position: absolute` 的链接平时不可见，`:focus` 时才出现（WCAG 2.4.1 Bypass Blocks）。
 *  跳转后焦点落到 region 容器（`tabindex="-1"`），下一次 Tab 从该区域内部继续。
 *
 *  为什么多一个「跳到输入框」：验收要求「首焦点到 composer ≤10 次按键」。
 *  主区里还有 6 个 workspace tab 与消息卡片按钮，纯 Tab 不可控；composer 是
 *  本产品最高频的落点，给它一条直达键是同一个无障碍目标的最小实现，不是新增功能。
 */

const TARGETS = [
  { id: 'region-sessions', label: '跳到会话区' },
  { id: 'region-main', label: '跳到主工作区' },
  { id: 'region-inspector', label: '跳到 Inspector' },
  { id: 'composer-input', label: '跳到输入框' },
] as const;

export function SkipLinks() {
  return (
    <nav className="skip-links" aria-label="跳转">
      {TARGETS.map(({ id, label }) => (
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
