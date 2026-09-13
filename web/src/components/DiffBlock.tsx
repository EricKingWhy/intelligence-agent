/** DiffBlock — before/after 双栏 diff 视图。
 *
 *  UI-01 从 ToolCard.tsx **原样抽出**复用（审批卡的 old/new 预览与工具卡的
 *  diff 走同一渲染）；搬运不改逻辑不改样式，出处见 ToolCard 历史注释。 */
import { Archive } from 'lucide-react';
import type { ToolCall } from '../types';
import { CopyButton } from './CopyButton';

export function DiffBlock({ diff }: { diff: NonNullable<ToolCall['diff']> }) {
  // da394a9 批：before/after 已归档（>2000 字符截断摘要内嵌 inspect_artifact marker）
  // → 占位态而非把 marker 原文当 diff 渲染。「点击查看」暂不接线（artifact 深链
  // 是后端 Gap，提案 D）——诚实给出 artifact 引用复制，不造假链接。
  if (diff.archived && diff.artifactId) {
    return (
      <div className="diff-block">
        <div className="diff-archived">
          <Archive size={14} />
          <div className="diff-archived-text">
            <div className="diff-archived-title">Diff 内容已归档（超过 2000 字符）</div>
            <div className="diff-archived-hint">
              原始变更已存为 artifact，可用 <code>inspect_artifact</code> 查看完整内容
            </div>
          </div>
          <code className="diff-archived-id">{diff.artifactId.slice(0, 16)}…</code>
          <CopyButton text={`inspect_artifact(${diff.artifactId})`} label="复制 inspect_artifact 引用" />
        </div>
      </div>
    );
  }
  return (
    <div className="diff-block">
      {diff.truncated && <div className="diff-truncated">内容过长，已截断显示</div>}
      <div className="diff-cols">
        <div className="diff-col diff-before">
          <div className="diff-col-label">变更前</div>
          <pre>{diff.before || '（空）'}</pre>
        </div>
        <div className="diff-col diff-after">
          <div className="diff-col-label">变更后</div>
          <pre>{diff.after || '（空）'}</pre>
        </div>
      </div>
    </div>
  );
}
