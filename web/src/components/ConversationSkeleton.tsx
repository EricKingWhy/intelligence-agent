/** ConversationSkeleton — #1371：历史加载期的对话区骨架。
 *
 * 只负责“数据未到”的形状，不持有任何状态：挂载/卸载由 `Conversation` 的
 * `loadingHistory` 分支决定。`role="status"` + `aria-label` 保留原来那句
 * 「正在加载历史…」的可访问性语义，但不再渲染任何空态文案（票面红线：
 * loading 期只渲染骨架）。
 *
 * ⚠ 不加 `aria-busy`：live region 在 busy 期间抑制播报，而本区域
 * 在 settle 时立即卸载——标签很可能一次都读不出来。
 */

const TURNS = [0, 1, 2] as const;

export function ConversationSkeleton() {
  return (
    <div
      className="conversation conversation-loading"
      role="status"
      aria-label="正在加载对话历史"
    >
      <div className="conversation-scroll" aria-hidden="true">
        {TURNS.map((turn) => (
          <div className="skeleton-turn" key={turn}>
            <div className="skeleton skeleton-avatar" />
            <div className="skeleton-turn-body">
              <div className="skeleton skeleton-line" />
              <div className="skeleton skeleton-line skeleton-line-w-82" />
              <div className="skeleton skeleton-line skeleton-line-w-58" />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
