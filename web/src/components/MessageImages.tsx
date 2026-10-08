/**
 * 消息内图片（#825 / MM-04 / AC5、AC6、AC7）。
 *
 * 来源：DeepSeek Harness `packages/client/ui-attachment/src/MessageImage.tsx`
 * 与 `…/client/MessageImages.tsx` @ commit `5badb15009ae1756c3afe0ae0cef1faafc290ccc`
 * （MIT，许可全文见 `web/THIRD_PARTY_NOTICES.md`）。判定 **ADAPT**（票面 D7）：
 * 上游走 DSH 私有原语（`IconLoadingOutlineRegular` / `ImageLightbox` / CSS module），
 * 这里换成 `lucide-react` + 本仓 `ImageLightbox`（Radix Dialog）+ 全局 class，
 * 保留其交互语义：缩略图 → 点开看原图 → 复制/下载；加载失败可重试。
 *
 * 数据来源是**事件的引用**（`Turn.user_attachments`，投影自 `user/message.data.attachments`），
 * 字节永远从受控端点取（`hooks/useAttachmentImage.ts`）——因此刷新/重放/换客户端后
 * 图仍在（AC5），而事件流里没有 base64（不变量 #15）。
 *
 * AC7 的「图已被省略」标注：模型不支持视觉时，后端会把图片引用投影成文本占位符
 * （`IMAGE_OMITTED_PLACEHOLDER`）**不发给模型**。界面据此显式告知，而不是让用户以为
 * 模型看见了。判据由调用方给（`omitted`），因为"这一轮用的是哪个模型、它支不支持视觉"
 * 是目录 + 所选模型的事实，不在事件里（`Turn` 不带模型能力位）。
 */

import { useState } from 'react';
import { RotateCcw } from 'lucide-react';
import type { ImageAttachmentRef } from '../lib/attachmentRefs';
import { IMAGE_OMITTED_PLACEHOLDER } from '../lib/attachments';
import { useAttachmentImage } from '../hooks/useAttachmentImage';
import { ImageLightbox } from './ImageLightbox';

interface Props {
  sessionId: string | null;
  images: readonly ImageAttachmentRef[];
  /** 这一轮附图会被模型省略（非视觉模型）——显示标注。 */
  omitted?: boolean;
}

export function MessageImages({ sessionId, images, omitted = false }: Props) {
  if (images.length === 0) return null;
  return (
    <div className="msg-images" role="group" aria-label="消息附图">
      {images.map((ref, index) => (
        // key 用 id + 序号：同一 id 在一轮里不会重复（内容寻址），序号只防坏数据。
        <MessageImageTile key={`${ref.attachment_id}#${index}`} sessionId={sessionId} image={ref} />
      ))}
      {omitted && (
        <span className="msg-images-omitted" title={IMAGE_OMITTED_PLACEHOLDER}>
          图已被省略（当前模型不支持视觉）
        </span>
      )}
    </div>
  );
}

/** 单张缩略图 + 它自己的大图浮层（浮层状态就地下沉，避免父层用下标索引 hooks）。 */
function MessageImageTile({ sessionId, image }: { sessionId: string | null; image: ImageAttachmentRef }) {
  const { src, loading, error, reload } = useAttachmentImage(sessionId, image.attachment_id);
  const [open, setOpen] = useState(false);
  /** 直连路上 `<img>` 的失败只有 onError 知道（token 路的失败在 `error` 里）。 */
  const [imgFailed, setImgFailed] = useState(false);
  const name = image.name && image.name.length > 0 ? image.name : '图片';
  const failed = error !== null || imgFailed;

  return (
    <span className="msg-image">
      <button
        type="button"
        className="msg-image-btn"
        onClick={() => setOpen(true)}
        disabled={failed || src === null}
        aria-label={`查看大图：${name}`}
        title={failed ? `${name}（加载失败）` : `查看大图：${name}`}
      >
        {failed ? (
          <span className="msg-image-state">加载失败</span>
        ) : src === null ? (
          <span className="msg-image-state">{loading ? '加载中…' : name}</span>
        ) : (
          <img
            className="msg-image-img"
            src={src}
            alt={name}
            width={image.width || undefined}
            height={image.height || undefined}
            onError={() => setImgFailed(true)}
          />
        )}
      </button>
      {failed && (
        <button
          type="button"
          className="msg-image-retry"
          onClick={() => {
            setImgFailed(false);
            reload();
          }}
          title={error ?? '重新从受控端点取回这张图'}
        >
          <RotateCcw size={11} />
          重试
        </button>
      )}
      <ImageLightbox open={open} onClose={() => setOpen(false)} src={failed ? null : src} name={name} />
    </span>
  );
}
