/**
 * `user/message.data.attachments` 的窄化解析（#825 / MM-04 / AC5）。
 *
 * 后端投影层的同名容错纪律（`src/agent_harness/attachments/projection.py::parse_image_refs`）：
 * 坏形状**逐条**跳过，不 brick 整个会话——投影/恢复必经节点上，一行坏数据不能拖垮一张
 * 会话视图。这里同样只看形状（`kind === 'image'`、id 为串、三数为有限数），
 * **不查存在性**（那是读端点的责任：未被引用的 id 一律 404）。
 *
 * 为什么不在 `projection.ts` 里内联：它同时被投影层与渲染层的类型边界使用，
 * 且必须能被 vitest 直接钉住（`attachmentRefs.test.ts`），而 `projection.ts` 是
 * 2000+ 行的聚合模块。
 */

/** 事件里一条图片引用的形状（= 后端 `attachments.types.ImageRef`）。 */
export interface ImageAttachmentRef {
  kind: 'image';
  attachment_id: string;
  media_type: string;
  bytes: number;
  width: number;
  height: number;
  /**
   * 展示名（去本地路径）。**当前写入路径恒缺省**（后端注释：上传回执的 name 未持久化，
   * 属结构性缺省——`src/agent_harness/attachments/types.py` 的 `name` 字段注释）。
   * 界面据此回落显示"图片"而不是伪造一个文件名。
   */
  name?: string | null;
}

/** 从事件 `data.attachments` 解析图片引用；坏形状逐条跳过。 */
export function parseImageRefs(raw: unknown): ImageAttachmentRef[] {
  if (!Array.isArray(raw)) return [];
  const refs: ImageAttachmentRef[] = [];
  for (const item of raw) {
    if (typeof item !== 'object' || item === null) continue;
    const r = item as Record<string, unknown>;
    if (r.kind !== 'image') continue;
    if (typeof r.attachment_id !== 'string' || !r.attachment_id) continue;
    if (typeof r.media_type !== 'string' || !r.media_type) continue;
    const numbers = [r.bytes, r.width, r.height];
    if (!numbers.every((n) => typeof n === 'number' && Number.isFinite(n))) continue;
    refs.push({
      kind: 'image',
      attachment_id: r.attachment_id,
      media_type: r.media_type,
      bytes: r.bytes as number,
      width: r.width as number,
      height: r.height as number,
      name: typeof r.name === 'string' ? r.name : null,
    });
  }
  return refs;
}
