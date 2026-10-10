/**
 * 终端能力 -> 待发图片的渲染决定（AC6 / AC7）。
 *
 * **不复制任何一家上游**：判定是 REUSE -- 直接用 pi-tui 既有的
 * `getCapabilities()` / `Image` 组件 / `imageFallback()`（`SPEC_ROOT/13_OPEN_SOURCE_REUSE_MATRIX.md`
 * 的 REUSE 档；本票票面也写死"按既有 pi-tui 能力"）。本文件只做「能力位 -> 组件 or
 * 文本占位」这一个分派，不重写协议编码。
 *
 * AC6 的判定依据是**上游事实**（阶段一实读）：`pi-tui/dist/terminal-image.js:68-70`
 * 在 `WT_SESSION` 下自报 `{images: null}` -- Windows Terminal 不支持 inline image
 * 协议（要 SIXEL，pi-tui 不支持），所以那里**必须**退化为文本占位，绝不能渲染假缩略图。
 */
import {
  Image,
  getCapabilities,
  getImageDimensions,
  imageFallback,
  type Component,
} from "@earendil-works/pi-tui";

import type { PendingImage } from "./pending-images.ts";
import type { TurnImageRef } from "../adapter.ts";

export interface DraftImageRenderers {
  /** pi-tui `Image` 组件的降级文字色（协议不可用时由它着色，AC6）。 */
  fallbackColor: (text: string) => string;
}

export type DraftImageRender =
  | { kind: "image"; component: Component }
  | { kind: "text"; lines: string[] };

const MAX_THUMBNAIL_WIDTH_CELLS = 40;

/**
 * 一张待发图片的渲染决定：终端支持图片协议 => pi-tui `Image` 缩略图（AC7）；
 * 否则 => `imageFallback()` 文本占位（文件名 + 尺寸 + 可打开的原图路径，AC6）。
 */
export function renderDraftImage(
  image: PendingImage,
  renderers: DraftImageRenderers,
): DraftImageRender {
  const base64 = Buffer.from(image.bytes).toString("base64");
  const dimensions = getImageDimensions(base64, image.mimeType);
  const capabilities = getCapabilities();
  if (capabilities.images !== null) {
    return {
      kind: "image",
      component: new Image(
        base64,
        image.mimeType,
        { fallbackColor: renderers.fallbackColor },
        { maxWidthCells: MAX_THUMBNAIL_WIDTH_CELLS, filename: displayPath(image) },
        dimensions ?? undefined,
      ),
    };
  }
  return { kind: "text", lines: [imageFallback(image.mimeType, dimensions ?? undefined, displayPath(image))] };
}

/**
 * M-09：历史附图（transcript 里的引用）的渲染决定 -- 与 `renderDraftImage` **同一个分派**：
 * transcript 是历史的视图、draft 是待发的视图，两者对"图片 -> 像素或文本"的判定是同一个
 * 决策，不复制第二套。
 *
 * 字节来源（M-09 可行性一手结论）：事件只带引用（不变量 #15：事件流里没有 base64），字节
 * 经受控端点 `GET /api/sessions/{id}/attachments/{aid}/content` 异步取回（web 端
 * `getAttachmentBytes` 同款通道）。`bytes === null`（还没取回 / 取回失败）时先给文本占位，
 * 字节到手后由调用方重投影换缩略图；**绝不**在字节不在手时渲染假缩略图。
 *
 * 产品溯源（成熟产品一手来源，2026-10-10 实读 docs.openclaw.ai/web/tui，逐字引文）：
 * "Terminals without a supported graphics protocol keep text output."（无协议终端保持
 * 文本输出：本文件 `bytes === null` 与 `getCapabilities().images === null` 两条降级路径
 * 的判定依据）；"Previews also appear when reopening a conversation or reconnecting."
 * （重开会话或重连时也要显示预览：本函数的存在动机，历史附图渲染）。
 */
export function renderHistoryImage(
  ref: TurnImageRef,
  bytes: Uint8Array | null,
  renderers: DraftImageRenderers,
): DraftImageRender {
  // 无展示名是服务端结构性缺省（上传回执的 name 未持久化）：占位里给真实 attachment_id，
  // 不编造文件名。
  const displayName = ref.name ?? ref.attachment_id;
  if (bytes === null) {
    // pi-tui 的维度字段是 widthPx/heightPx（ImageDimensions）；引用里是 width/height，
    // 这里做唯一一次字段名转接。
    const dimensions =
      ref.width > 0 && ref.height > 0
        ? { widthPx: ref.width, heightPx: ref.height }
        : undefined;
    return {
      kind: "text",
      lines: [imageFallback(ref.media_type, dimensions, displayName)],
    };
  }
  // 字节在手 => 装配成与待发图同形状，走同一条分派（同一判定、同一 maxWidth、同一占位文案）。
  const pending: PendingImage = {
    bytes,
    mimeType: ref.media_type,
    name: displayName,
    path: null,
  };
  return renderDraftImage(pending, renderers);
}

/**
 * 占位里出现的路径：有真实文件路径就给真路径（`imageFallback` 会把绝对路径缩短成
 * `~/...`，并在支持 OSC 8 的终端上链到 `file://`，这正是 AC6 要的"可打开的原图路径"）；
 * 剪贴板字节没有路径，只能用文件名，**不编造**一个假路径。
 */
function displayPath(image: PendingImage): string {
  return image.path ?? image.name;
}
