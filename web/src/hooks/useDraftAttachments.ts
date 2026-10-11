/**
 * 附图草稿的上传状态机（#825 / MM-04 / AC2–AC4）。
 *
 * 状态与 DSH 的同构（来源：DeepSeek Harness `packages/client/ui-conversation/src/client/service.ts:329-400`
 * @ commit `5badb150`，MIT）但**重写为本仓 hook**（票面 D7 明确：`service.ts` 的编排
 * 属「重写」，不 COPY——它绑 Cordis 的 `ctx.fileUpload` worker 池）。本仓差异与理由：
 * - 无 worker 池/无并发上限：一次 intake 至多 20 张（`attachments.ts::IMAGE_LIMITS`），
 *   浏览器自身按 host 连接数排队，自己再排一层池只会多一份要维护的状态；
 * - **上传失败不阻塞文本发送**（AC4）：`readyIds` 只收"已就绪"的引用；在途/失败的草稿
 *   不参与发送，发送按钮的可用性只看文本（Composer 侧单点判断）；
 * - 会话切换即清空并中止在途上传：草稿属于**某个会话**（上传端点 per-session），
 *   跟着 UI 换会话继续传只会得到另一个会话名下的孤儿字节。
 *
 * 状态载体是 ref + 强制重渲染（而不是 `useState` 数组）：所有变更必须**同步**可见——
 * 预检的判据是"当前已附了几张、共几字节"，同一 tick 里的第二次 intake（粘贴 + 拖放）
 * 若读到旧列表就会各放行一次，把上限算少；而把副作用（发起上传）放进 `setState`
 * updater 又会在 React 严格模式下被执行两次（重复上传）。ref 让"判定 → 入列 → 上传"
 * 是一个同步动作，两条路都堵死。
 */

import { useCallback, useEffect, useReducer, useRef } from 'react';
import {
  uploadAttachment,
  type AttachmentUpload,
  type AttachmentUploadReceipt,
} from '../lib/api';
import { makeThumbnailDataUrl } from '../lib/attachmentThumbnail';
import { partitionIntake, detectImageMediaType } from '../lib/attachments';

export interface DraftAttachment {
  /** 客户端 key（React key + 状态机身份）。与 `attachment_id` **无关**：上传成功才有引用。 */
  id: string;
  file: File;
  status: 'uploading' | 'ready' | 'failed';
  /** 已上传字节（`xhr.upload.onprogress` 转述；`total` = 文件字节）。 */
  loaded: number;
  total: number;
  /** 本地缩略图（`data:` URL，见 `lib/attachmentThumbnail.ts`）；null = 显示文件名占位。 */
  thumb: string | null;
  receipt?: AttachmentUploadReceipt;
  /** 失败原因（后端 detail 原样）。 */
  error?: string;
}

export interface DraftAttachmentsApi {
  items: DraftAttachment[];
  /** 可进发送请求的引用 id，顺序 = 附图顺序。 */
  readyIds: string[];
  /** 最近一次 intake 被拒的原因（AC3：常驻就地显示，直到用户关掉或下次 intake 成功）。 */
  intakeError: string | null;
  addFiles: (files: readonly File[], directories?: ReadonlySet<File>) => void;
  remove: (id: string) => void;
  retry: (id: string) => void;
  /** 提交成功后清掉**已进请求**的那些草稿（在途/失败的留在栏里，等用户重试或下次带上）。 */
  clearSent: (attachmentIds: readonly string[]) => void;
  dismissIntakeError: () => void;
}

/** 草稿的本地 key（模块内单调递增：确定性、无 crypto 依赖，测试可直接断言顺序）。 */
let draftSeq = 0;

/** 附图草稿状态机。`sessionId` 为 null（尚无会话）时 `addFiles` 一律被拒——
 *  上传端点是 per-session 的（`POST /api/sessions/{id}/attachments`），没有会话就没有落点。 */
export function useDraftAttachments(sessionId: string | null): DraftAttachmentsApi {
  const itemsRef = useRef<DraftAttachment[]>([]);
  const [, bump] = useReducer((n: number) => n + 1, 0);
  const errorRef = useRef<string | null>(null);
  /** 在途上传句柄（按草稿 id）：移除草稿 / 换会话 / 卸载时中止，避免孤儿上传。 */
  const uploadsRef = useRef(new Map<string, AttachmentUpload>());

  const commit = useCallback((next: DraftAttachment[]) => {
    itemsRef.current = next;
    bump();
  }, []);

  const setIntakeError = useCallback(
    (next: string | null) => {
      errorRef.current = next;
      bump();
    },
    [],
  );

  const abortAll = useCallback(() => {
    for (const upload of uploadsRef.current.values()) upload.abort();
    uploadsRef.current.clear();
  }, []);

  // 换会话 = 换附件命名空间：清空草稿 + 中止在途上传（模块头最后一条）。
  useEffect(() => {
    abortAll();
    itemsRef.current = [];
    errorRef.current = null;
    bump();
  }, [sessionId, abortAll, bump]);

  // 卸载时不留悬挂 XHR。
  useEffect(() => abortAll, [abortAll]);

  /** 只更新仍然存在的那条草稿（已被移除 / 已换会话 → 静默丢弃这次结果）。 */
  const patch = useCallback(
    (id: string, next: Partial<DraftAttachment>) => {
      if (!itemsRef.current.some((item) => item.id === id)) return;
      commit(itemsRef.current.map((item) => (item.id === id ? { ...item, ...next } : item)));
    },
    [commit],
  );

  const startUpload = useCallback(
    (id: string, file: File) => {
      if (sessionId === null) return;
      const upload = uploadAttachment(sessionId, file, (loaded, total) => patch(id, { loaded, total }));
      uploadsRef.current.set(id, upload);
      upload.promise
        .then((receipt) => {
          uploadsRef.current.delete(id);
          patch(id, { status: 'ready', receipt, loaded: file.size, total: file.size });
        })
        .catch((error: unknown) => {
          uploadsRef.current.delete(id);
          patch(id, { status: 'failed', error: (error as Error).message });
        });
      void makeThumbnailDataUrl(file).then((thumb) => {
        if (thumb !== null) patch(id, { thumb });
      });
    },
    [sessionId, patch],
  );

  // 签名保持同步 `(files, directories?) => void`（调用方都是事件处理器里直接调用、
  // 不 await）。内部是 async IIFE：#937 / M-20 的字节探测是异步的，拒绝原因仍经
  // `setIntakeError` 同一通道呈现（AC3），不新增第二条用户可见的错误路径。
  const addFiles = useCallback(
    (files: readonly File[], directories?: ReadonlySet<File>) => {
      // 会话缺失时**静默返回**，不给文案：用户可见的原因由 Composer 单点给出
      // （`attachBlockedReason`：门禁 + 拖放遮罩文案 + 按钮 title 三处同源）。这里
      // 再写一份只会漂移，而它是**结构性**守卫——上传端点 per-session，没有会话就
      // 没有落点，放行会留下一排永远"上传中"的卡片。
      if (sessionId === null) return;
      // 目录拖拽产出的"文件"是空壳（`dropEvents.ts::droppedDirectories`），直接丢弃。
      const candidates = files.filter((file) => !directories?.has(file));
      if (candidates.length === 0) return;
      void (async () => {
        // 字节探测先行（#937 / M-20）：`file.type` 可伪造，预检的「是不是图片」
        // 以文件头魔数为准（对齐后端 `probe.py`）；探测后 `partitionIntake` 仍
        // 用默认 limits（`getImageLimits()`，M-08 起权威在服务端下发）。
        const probed = new Map(
          await Promise.all(
            candidates.map(async (file) => [file, await detectImageMediaType(file)] as const),
          ),
        );
        const existing = itemsRef.current.map((item) => ({ bytes: item.file.size }));
        const outcome = partitionIntake(candidates, existing, undefined, probed);
        if (outcome.error !== null || outcome.accepted.length === 0) {
          setIntakeError(outcome.error);
          return;
        }
        setIntakeError(null);
        const drafts: DraftAttachment[] = outcome.accepted.map((file) => ({
          id: `draft-${++draftSeq}`,
          file,
          status: 'uploading' as const,
          loaded: 0,
          total: file.size,
          thumb: null,
        }));
        commit([...itemsRef.current, ...drafts]);
        for (const draft of drafts) startUpload(draft.id, draft.file);
      })();
    },
    [sessionId, commit, setIntakeError, startUpload],
  );

  const remove = useCallback(
    (id: string) => {
      uploadsRef.current.get(id)?.abort();
      uploadsRef.current.delete(id);
      commit(itemsRef.current.filter((item) => item.id !== id));
    },
    [commit],
  );

  const retry = useCallback(
    (id: string) => {
      const target = itemsRef.current.find((item) => item.id === id);
      if (!target || target.status !== 'failed') return;
      patch(id, { status: 'uploading', loaded: 0, error: undefined });
      startUpload(id, target.file);
    },
    [patch, startUpload],
  );

  const dismissIntakeError = useCallback(() => setIntakeError(null), [setIntakeError]);

  const clearSent = useCallback(
    (attachmentIds: readonly string[]) => {
      const sent = new Set(attachmentIds);
      const remaining = itemsRef.current.filter(
        (item) => item.receipt === undefined || !sent.has(item.receipt.attachment_id),
      );
      if (remaining.length !== itemsRef.current.length) commit(remaining);
    },
    [commit],
  );

  const items = itemsRef.current;
  return {
    items,
    readyIds: items.flatMap((item) =>
      item.status === 'ready' && item.receipt ? [item.receipt.attachment_id] : [],
    ),
    intakeError: errorRef.current,
    addFiles,
    remove,
    retry,
    clearSent,
    dismissIntakeError,
  };
}
