/**
 * 原图查看浮层（#825 / MM-04 / AC6）：点开看大图 + 复制 / 下载原图。
 *
 * 来源：DeepSeek Harness `packages/client/ui-attachment/src/MessageImage.tsx` 的
 * `ImageLightbox` 用法（MIT，commit `5badb150`）——判定 **ADAPT**（票面 D7）：
 * 上游用的是 DSH 私有原语 `@deepseek-ai/dsh-client-ui-primitives` 的 `ImageLightbox`
 * 与其 CSS module（本仓没有这两个依赖）；这里换成**本仓既有原语**
 * `@radix-ui/react-dialog`（与 `DeleteSessionDialog`/`ApprovalModal` 同一套用法），
 * 只保留其交互语义：模态、Esc/遮罩关闭、正文是原始尺寸的图、动作是复制/下载。
 *
 * 两个动作的语义边界：
 * - **下载原图** = 受控端点的原始字节（`<a download>` 指向 `src` 本身，不做任何转码）；
 * - **复制原图** = 经 `lib/clipboardImage.ts` 重编码成 PNG 写剪贴板（浏览器剪贴板对
 *   WebP/JPEG 支持不一）；失败原因就地显示并引导走下载。
 */

import { useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { Check, Copy, Download, X } from 'lucide-react';
import { copyImageToClipboard } from '../lib/clipboardImage';

interface Props {
  open: boolean;
  onClose: () => void;
  /** 可渲染地址（同源受控端点或 `data:`，见 `hooks/useAttachmentImage.ts`）；null = 不可用。 */
  src: string | null;
  /** 展示名（写入路径不带 name 时由调用方回落，如「图片」）。 */
  name: string;
}

export function ImageLightbox({ open, onClose, src, name }: Props) {
  const [copyState, setCopyState] = useState<{ kind: 'idle' | 'busy' | 'done' } | { kind: 'failed'; message: string }>(
    { kind: 'idle' },
  );

  const copy = () => {
    if (src === null || copyState.kind === 'busy') return;
    setCopyState({ kind: 'busy' });
    void copyImageToClipboard(src)
      .then(() => setCopyState({ kind: 'done' }))
      .catch((error: unknown) => setCopyState({ kind: 'failed', message: (error as Error).message }));
  };

  return (
    <Dialog.Root
      open={open}
      onOpenChange={(next) => {
        if (!next) {
          setCopyState({ kind: 'idle' });
          onClose();
        }
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="palette-overlay image-lightbox-overlay" />
        <Dialog.Content className="image-lightbox" aria-label={`原图：${name}`}>
          <div className="image-lightbox-head">
            <Dialog.Title className="image-lightbox-title">{name}</Dialog.Title>
            <span className="image-lightbox-actions">
              <button
                type="button"
                className="image-lightbox-btn"
                onClick={copy}
                disabled={src === null || copyState.kind === 'busy'}
                title="复制原图到剪贴板（重编码为 PNG）"
              >
                {copyState.kind === 'done' ? <Check size={14} /> : <Copy size={14} />}
                {copyState.kind === 'busy' ? '复制中…' : copyState.kind === 'done' ? '已复制' : '复制原图'}
              </button>
              {/* 下载走 `src` 本身：受控端点的原始字节，不转码、不经内存。 */}
              <a
                className="image-lightbox-btn"
                href={src ?? undefined}
                download={name}
                aria-disabled={src === null}
                onClick={(event) => {
                  if (src === null) event.preventDefault();
                }}
              >
                <Download size={14} />
                下载原图
              </a>
              <Dialog.Close asChild>
                <button type="button" className="image-lightbox-btn" aria-label="关闭大图">
                  <X size={14} />
                </button>
              </Dialog.Close>
            </span>
          </div>
          <div className="image-lightbox-body">
            {src === null ? (
              <span className="image-lightbox-empty">图片不可用</span>
            ) : (
              <img className="image-lightbox-img" src={src} alt={name} />
            )}
          </div>
          {copyState.kind === 'failed' && (
            <p className="image-lightbox-error" role="alert">
              复制失败：{copyState.message}（可改用「下载原图」）
            </p>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
