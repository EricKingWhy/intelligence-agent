/**
 * 受控端点图片的可渲染地址（#825 / MM-04 / AC5、AC9）。
 *
 * 两条路，取决于部署是否配了 Bearer token（`lib/auth.ts` 的接缝；后端 auth_seam
 * fail-closed）：
 * - **未配 token**（默认本地部署）：直接用同源受控端点 URL 交给 `<img>`。浏览器自己
 *   流式渲染，不复制字节；CSP `img-src 'self'`（`web/app.py:1654`）恰好放行，
 *   这也是"保持 CSP 不变"的落点——本票不新增任何外域或 `blob:`。
 * - **配了 token**：`<img src>` 带不上 `Authorization` 头（会 401），所以经 `apiFetch`
 *   取字节再转 `data:` URL（CSP 白名单里本就有 `data:`）。代价是字节进内存，
 *   但这是"配了 token 也要能看图"的唯一不破坏 CSP 的做法，且只在配了 token 的部署发生。
 *
 * 失败不吞：`error` 原样交给调用方就地显示（404 尤其是**真答案**——该 id 未被本会话
 * 任何 `user/message` 引用，后端刻意与"从未上传"不可区分）。
 */

import { useCallback, useEffect, useState } from 'react';
import { attachmentContentUrl, getAttachmentBytes } from '../lib/api';
import { getToken } from '../lib/auth';

export interface AttachmentImage {
  /** 可渲染地址；null = 尚未就绪（加载中或失败）。 */
  src: string | null;
  loading: boolean;
  error: string | null;
  /** 重新加载（`<img onError>` 之后由调用方触发）。 */
  reload: () => void;
}

export function useAttachmentImage(
  sessionId: string | null,
  attachmentId: string,
): AttachmentImage {
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<{ src: string | null; loading: boolean; error: string | null }>({
    src: null,
    loading: false,
    error: null,
  });

  useEffect(() => {
    if (sessionId === null) {
      setState({ src: null, loading: false, error: '缺少会话上下文' });
      return;
    }
    // `attempt` 参与 URL：直连路上换 key 才能让浏览器真的重发（同 URL 会复用失败结果）。
    const base = attachmentContentUrl(sessionId, attachmentId);
    const direct = attempt === 0 ? base : `${base}?reload=${attempt}`;
    if (!getToken()) {
      setState({ src: direct, loading: false, error: null });
      return;
    }
    let cancelled = false;
    setState({ src: null, loading: true, error: null });
    void getAttachmentBytes(sessionId, attachmentId)
      .then(
        (blob) =>
          new Promise<string>((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(String(reader.result));
            reader.onerror = () => reject(new Error('图片字节解码失败'));
            reader.readAsDataURL(blob);
          }),
      )
      .then((src) => {
        if (!cancelled) setState({ src, loading: false, error: null });
      })
      .catch((error: unknown) => {
        if (!cancelled) setState({ src: null, loading: false, error: (error as Error).message });
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, attachmentId, attempt]);

  const reload = useCallback(() => setAttempt((n) => n + 1), []);
  return { ...state, reload };
}
