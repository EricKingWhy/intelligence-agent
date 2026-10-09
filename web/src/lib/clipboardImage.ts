/**
 * 把一张图片写进系统剪贴板（#825 / MM-04 / AC6「复制原图」）。
 *
 * 统一转成 PNG 再写：`ClipboardItem` 的图片类型各家支持不一（Chromium 只稳吃
 * `image/png`；WebP 常被拒），而 `data:`/同源 URL 两种来源都要走同一条路——
 * 先在 canvas 上重绘、`toBlob('image/png')`，再 `clipboard.write`。
 *
 * 失败**原样抛出**（调用方就地显示原因 + 引导用"下载原图"）：剪贴板可能因权限策略
 * （非用户手势 / 无权限）被拒，或浏览器不支持 `ClipboardItem`——两种都不是"复制成功"，
 * 不允许静默。
 */
export async function copyImageToClipboard(src: string): Promise<void> {
  if (typeof ClipboardItem !== 'function' || navigator.clipboard?.write === undefined) {
    throw new Error('当前浏览器不支持写入图片到剪贴板');
  }
  const blob = await (await fetch(src)).blob();
  const bitmap = await createImageBitmap(blob);
  const canvas = document.createElement('canvas');
  canvas.width = bitmap.width;
  canvas.height = bitmap.height;
  const ctx = canvas.getContext('2d');
  if (ctx === null) throw new Error('无法创建画布（复制需要重编码为 PNG）');
  ctx.drawImage(bitmap, 0, 0);
  bitmap.close();
  const png = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, 'image/png'));
  if (png === null) throw new Error('PNG 转码失败');
  await navigator.clipboard.write([new ClipboardItem({ 'image/png': png })]);
}
