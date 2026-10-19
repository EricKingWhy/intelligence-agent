/** SessionListSkeleton — #1371：会话列表首次加载期的骨架。
 *
 * 由 `SessionList` 在 `sessionsPending`（首次请求未 settle 且没有行）时挂载；
 * 首次请求 settle 后由真实列表替换。骨架不渲染「暂无会话」等空态文案。
 * 同 ConversationSkeleton：`role="status"` + `aria-label`，刻意不加 `aria-busy`
 * （busy 期间 live region 抑制播报，而本区域 settle 即卸载）。
 */

const ROWS = [0, 1, 2, 3, 4, 5] as const;

export function SessionListSkeleton() {
  return (
    <div
      className="rail-skeleton"
      role="status"
      aria-label="正在加载会话列表"
    >
      <div className="skeleton skeleton-rail-section" aria-hidden="true" />
      <div aria-hidden="true">
        {ROWS.map((row) => (
          <div className="skeleton-rail-row" key={row}>
            <div className="skeleton skeleton-rail-icon" />
            <div className="skeleton skeleton-rail-line" />
          </div>
        ))}
      </div>
    </div>
  );
}
