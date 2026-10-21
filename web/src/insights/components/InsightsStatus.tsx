/** 洞察页共用的错误条与加载骨架。两页的取数互相独立，但状态外观必须一致；
 *  文案 / 重试动作由调用方给，组件不猜是哪一路请求失败。 */

import type { ReactElement } from 'react';

export function InsightsError({
  title,
  message,
  onRetry,
}: {
  title: string;
  message: string;
  onRetry: () => void;
}): ReactElement {
  return (
    <div className="ctx-insights-error" role="alert">
      <div>
        <b>{title}</b>
        <span>{message}</span>
      </div>
      <button type="button" onClick={onRetry}>
        重试
      </button>
    </div>
  );
}

/** `blocks` 决定骨架块数：跨会话页对齐 6 格 KPI，单会话页对齐三张卡片。 */
export function InsightsSkeleton({
  label,
  blocks,
}: {
  label: string;
  blocks: number;
}): ReactElement {
  return (
    <div className="ctx-insights-skeleton" role="status">
      <span className="ctx-insights-sr">{label}</span>
      {Array.from({ length: blocks }, (_, index) => (
        <span key={index} />
      ))}
    </div>
  );
}
