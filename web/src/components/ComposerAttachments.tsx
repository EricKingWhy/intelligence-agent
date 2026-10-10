/**
 * 草稿附图区（#825 / MM-04 / AC1–AC4）：缩略图列表 + 单张删除 + 预算提示 +
 * 逐张进度/失败重试 + 就地超限原因 + 整页拖拽遮罩。
 *
 * 来源：DeepSeek Harness `packages/client/ui-attachment/src/AttachmentRail.tsx`、
 * `FileCard.tsx`、`client/ComposerAttachments.tsx` @ commit `5badb15009ae1756c3afe0ae0cef1faafc290ccc`
 * （MIT，许可全文见 `web/THIRD_PARTY_NOTICES.md`）。判定 **ADAPT**（票面 D7）：
 * 上游依赖 DSH 私有原语（`IconCloseFillRegular` / `IconLoadingOutlineRegular` /
 * `IconRefreshOutlineRegular` / `IconChevronLeft|RightOutlineRegular` / `ImageLightbox`）
 * 与 CSS module；这里换成 `lucide-react` + 全局 class。
 *
 * 两处**有意的语义收敛**（不是简化掉的检查，而是能力范围不同）：
 * - 上游 rail 用隐藏滚动条 + 两端翻页箭头（上游可能一次挂几十个附件）；
 *   本仓上限 20 张（`attachments.ts::IMAGE_LIMITS`），改成换行网格——所有缩略图
 *   一眼可见，不再需要翻页手势，"有更多但看不见"的形态直接消失；
 * - 上游的 rail 也承载**通用文件**（`FileCard`）；本票只有图片（非图片文件的
 *   `@path` 引用属 MM-05 桌面桥），所以不做文件卡分支。
 */

import { ImagePlus, RotateCcw, X } from 'lucide-react';
import type { DraftAttachment } from '../hooks/useDraftAttachments';
import { formatBytes } from '../lib/format';
import { DropOverlay } from './DropOverlay';

interface Props {
  items: readonly DraftAttachment[];
  /** AC3：最近一次 intake 被拒的就地原因（常驻，直到用户关掉或下次成功）。 */
  intakeError: string | null;
  /** AC2：剩余数量/大小预算文案。 */
  budget: string;
  /** 当前有文件被拖到页面上。 */
  dragActive: boolean;
  /** 当前是否接受拖放（决定遮罩是"邀请"还是"禁止"两种插图）。 */
  canAcceptDrop: boolean;
  /** 不可接受时的原因（显示在遮罩上，不让用户对着禁止光标猜）。 */
  dropBlockedReason: string;
  /** 「选择图片」入口（错误态/空态复用同一个文件选择器）。 */
  onPickFiles: () => void;
  onRemove: (id: string) => void;
  onRetry: (id: string) => void;
  onDismissIntakeError: () => void;
}

export function ComposerAttachments({
  items,
  intakeError,
  budget,
  dragActive,
  canAcceptDrop,
  dropBlockedReason,
  onPickFiles,
  onRemove,
  onRetry,
  onDismissIntakeError,
}: Props) {
  return (
    <>
      {dragActive && (
        <DropOverlay
          disabled={!canAcceptDrop}
          labels={{
            title: canAcceptDrop ? '松开鼠标即可附图' : dropBlockedReason,
            desc: canAcceptDrop ? '支持 PNG / JPEG / WebP / GIF' : undefined,
          }}
        />
      )}
      {intakeError !== null && (
        <div className="composer-attach-error" role="alert">
          <span className="composer-attach-error-text">{intakeError}</span>
          <button type="button" className="composer-attach-error-btn" onClick={onPickFiles}>
            重新选择
          </button>
          <button
            type="button"
            className="composer-attach-error-btn"
            onClick={onDismissIntakeError}
            aria-label="关闭错误提示"
          >
            <X size={12} />
          </button>
        </div>
      )}
      {items.length > 0 && (
        /* `role="group"` 而非 `list`：栏内既有卡片，也有预算 `<span role="status">` 与
           「再加一张」按钮——`list` 的 required owned element 只有 `listitem`/`group`，
           把按钮塞进列表里要么违反 ARIA（axe `aria-required-children`），要么就得给
           按钮也标 `listitem` 而丢掉按钮语义（本仓 `DirectoryBrowser`/`ProjectDialogs`
           的既有惯例）。带名字的 `group` 只表达"这是一簇相关控件"，与实物一致。 */
        <div className="composer-attach-rail" role="group" aria-label="待发送图片">
          <span className="composer-attach-budget" role="status">
            {budget}
          </span>
          {items.map((item) => (
            <div key={item.id} className="composer-attach-card" data-status={item.status}>
              {item.thumb !== null ? (
                <img className="composer-attach-thumb" src={item.thumb} alt={item.file.name || '待发送图片'} />
              ) : (
                // 缩略图生成失败（浏览器解不了这张图）→ 如实显示文件名，不画假图。
                <span className="composer-attach-thumb composer-attach-thumb-fallback">
                  {item.file.name || '图片'}
                </span>
              )}
              <span className="composer-attach-meta">
                <span className="composer-attach-name" title={item.file.name}>
                  {item.file.name || '图片'}
                </span>
                <span className="composer-attach-size">
                  {item.status === 'uploading'
                    ? `上传中 ${Math.round((item.loaded / Math.max(1, item.total)) * 100)}%`
                    : formatBytes(item.file.size)}
                </span>
              </span>
              {item.status === 'failed' && (
                <span className="composer-attach-failed" role="alert" title={item.error}>
                  <span className="composer-attach-failed-text">
                    上传失败：{item.error ?? '未知原因'}
                  </span>
                  <button
                    type="button"
                    className="composer-attach-btn"
                    onClick={() => onRetry(item.id)}
                    aria-label={`重试上传 ${item.file.name || '这张图'}`}
                  >
                    <RotateCcw size={12} />
                    重试
                  </button>
                </span>
              )}
              <button
                type="button"
                className="composer-attach-btn composer-attach-remove"
                onClick={() => onRemove(item.id)}
                aria-label={`移除 ${item.file.name || '这张图'}`}
                title="移除这张图"
              >
                <X size={12} />
              </button>
            </div>
          ))}
          <button type="button" className="composer-attach-add" onClick={onPickFiles}>
            <ImagePlus size={14} />
            再加一张
          </button>
        </div>
      )}
    </>
  );
}
