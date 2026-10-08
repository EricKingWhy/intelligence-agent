/**
 * 草稿缩略图（#825 / MM-04）。
 *
 * **为什么是 `data:` URL 而不是 `URL.createObjectURL`**：CSP（后端
 * `web/app.py:1654` / `:2064`，AC9 要求保持不变）是
 * `default-src 'self'; img-src 'self' data:` —— `blob:` **不在**白名单里，
 * 同源 object URL 也会被拦（`img-src 'self'` 只覆盖 http(s) 同源，不含 blob:）。
 * 所以草稿预览只能走 `data:`；这是"保持 CSP 不变"这条约束的直接代价。
 *
 * **为什么先缩图**：`data:` 是 base64，体积 ×4/3。单张上限 20 MiB、单条上限 200 MiB
 * （`attachments.ts::IMAGE_LIMITS`），20 张原图的 base64 会到数百 MB 常驻内存。
 * 按长边 320px 重绘后再编码，单张缩略图落在几 KB 量级；缩放同时给栅格化兜底
 * （`createImageBitmap` 失败 = 这张图浏览器解不了，此时返回 null，由调用方显示文件名占位，
 * 而不是拿一张假图糊弄）。
 */

/** 缩略图长边（px）。够 2x 屏的 rail 卡片清晰，又不至于让 data: 串变大。 */
const THUMB_MAX_EDGE = 320;

/** 生成草稿缩略图的 `data:` URL；无法生成（环境缺 API / 解码失败 / 无 2D 上下文）→ null。 */
export async function makeThumbnailDataUrl(file: File): Promise<string | null> {
  if (typeof createImageBitmap !== 'function' || typeof document === 'undefined') return null;
  let bitmap: ImageBitmap | null = null;
  try {
    bitmap = await createImageBitmap(file);
    const longEdge = Math.max(bitmap.width, bitmap.height);
    const scale = longEdge > THUMB_MAX_EDGE ? THUMB_MAX_EDGE / longEdge : 1;
    const width = Math.max(1, Math.round(bitmap.width * scale));
    const height = Math.max(1, Math.round(bitmap.height * scale));
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext('2d');
    if (ctx === null) return null;
    ctx.drawImage(bitmap, 0, 0, width, height);
    return canvas.toDataURL('image/webp', 0.8);
  } catch {
    return null;
  } finally {
    bitmap?.close();
  }
}
