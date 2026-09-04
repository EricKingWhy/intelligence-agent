/** useSession — orchestrates session list, history loading, and live streaming.
 *
 * State model: a single discriminated `mode` (SessionMode) is the source of truth —
 *   idle                    → no session (empty state), conversation is null
 *   live(sessionId?)        → a new task is streaming; events paint the conversation
 *   viewing(sessionId)      → a durable session is being viewed (history rebuild)
 * `selectedId` / `streaming` are derived from mode, so conversation.session_id and
 * the highlighted row can never disagree (invariant #22: no second truth).
 *
 * Migrations:
 *   submit    → live(null), conversation reset (a new task never folds into the
 *               previously viewed session's state)
 *   first SSE frame with session_id → live(sid)
 *   done/error/cancel → viewing(known sid) or idle (nothing durable yet) — the
 *               history loader re-reads the durable log so the view always
 *               comes from the fact source.
 *   selectSession → cancels any live stream (idempotent) and switches mode.
 *
 * Disconnect cleanup: SSE handle's cancel() is wired to unmount via useEffect ref.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type { AgentEvent, ConversationState, SessionMode, SessionSummary } from '../types';
import { listSessions, getSessionEvents, startSession, type StartSessionPayload } from '../lib/api';
import { consumeSSE, type SSEHandle } from '../lib/sse';
import { initConversation, applyEvent, projectHistory, deriveSessionTitle, extractSessionTitle } from '../lib/projection';

export function useSession() {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [mode, setMode] = useState<SessionMode>({ kind: 'idle' });
  const [conversation, setConversation] = useState<ConversationState | null>(null);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Session Rail 行标题缓存（Session Model E 轮）：查看某会话时从首条 user/message
  // 投影出标题，本地缓存供 SessionList 行渲染。无缓存时回退到短 ID——不伪造。
  const [titlesById, setTitlesById] = useState<Record<string, string>>({});

  const sseRef = useRef<SSEHandle | null>(null);
  // live 流中已知的首帧 sid——结束/出错/取消时决定迁移目标。
  const liveSidRef = useRef<string | null>(null);

  // Derived, so the UI can never observe a mismatch between them.
  const selectedId = mode.kind === 'idle' ? null : mode.sessionId;
  const streaming = mode.kind === 'live';

  // Load session list on mount.
  const refreshSessions = useCallback(async () => {
    try {
      const list = await listSessions();
      setSessions(list);
      // 后端 Gap 3：列表 payload 携带首条用户消息（截断 128）——零额外请求预填
      // 标题缓存。events 扫描（viewing 路径）保留为后端未返回时的 fallback；
      // 已有标题不覆盖（事件派生值优先，保持单一更新路径语义）。
      setTitlesById((m) => {
        let changed = false;
        const next = { ...m };
        for (const s of list) {
          if (s.first_user_message && !next[s.session_id]) {
            next[s.session_id] = s.first_user_message;
            changed = true;
          }
        }
        return changed ? next : m;
      });
    } catch (e) {
      setError(`加载会话列表失败：${(e as Error).message}`);
    }
  }, []);

  useEffect(() => {
    refreshSessions();
  }, [refreshSessions]);

  // History loading: viewing mode reads the durable log; live mode lets the
  // stream paint (a stale/partial disk read would overwrite in-flight state);
  // idle owns no conversation.
  useEffect(() => {
    if (mode.kind === 'idle') {
      setConversation(null);
      return;
    }
    if (mode.kind !== 'viewing') return;
    const sid = mode.sessionId;
    let cancelled = false;
    setLoadingHistory(true);
    setError(null);
    getSessionEvents(sid)
      .then((events: AgentEvent[]) => {
        if (cancelled) return;
        setConversation(projectHistory(sid, events));
        // 从真实事件流投影首条用户消息作为行标题（无则空串，回退短 ID）。
        const title = deriveSessionTitle(events);
        if (title) setTitlesById((m) => (m[sid] === title ? m : { ...m, [sid]: title }));
      })
      .catch((e) => {
        if (!cancelled) setError(`加载历史事件失败：${(e as Error).message}`);
      })
      .finally(() => {
        if (!cancelled) setLoadingHistory(false);
      });
    return () => {
      cancelled = true;
    };
  }, [mode]);

  // Cleanup SSE on unmount.
  useEffect(() => {
    return () => {
      sseRef.current?.cancel();
    };
  }, []);

  /** Submit a new task. Creates a fresh session and streams the response.
   *  The conversation is reset first — a live stream never folds into the
   *  previously viewed session's turns. */
  const submitTask = useCallback(
    async (payload: StartSessionPayload) => {
      setError(null);
      setConversation(null);
      liveSidRef.current = null;
      setMode({ kind: 'live', sessionId: null });
      try {
        const res = await startSession(payload);
        if (!res.ok || !res.body) {
          throw new Error(`Start failed: ${res.status}`);
        }

        let conv: ConversationState | null = null;

        const handle = consumeSSE(
          res,
          (event: AgentEvent) => {
            const sid = event.session_id ?? null;
            if (sid) {
              liveSidRef.current = sid;
              // 首帧确认：把 sid 写进 mode（仅从 null 迁移一次）
              setMode((m) =>
                m.kind === 'live' && m.sessionId === null ? { kind: 'live', sessionId: sid } : m,
              );
            }
            if (!conv) conv = initConversation(sid ?? 'streaming');
            conv = applyEvent(conv, event);
            // 首条用户消息到达时缓存行标题（Session Model E 轮）。
            if (sid) {
              const content = extractSessionTitle(event);
              if (content) setTitlesById((m) => (m[sid] ? m : { ...m, [sid]: content }));
            }
            setConversation({ ...conv });
          },
          () => {
            // Stream finished: view the session it produced (history loader
            // re-reads the durable log), and refresh the list for the new row.
            const sid = liveSidRef.current;
            setMode(sid ? { kind: 'viewing', sessionId: sid } : { kind: 'idle' });
            refreshSessions();
          },
          (err) => {
            const sid = liveSidRef.current;
            setMode(sid ? { kind: 'viewing', sessionId: sid } : { kind: 'idle' });
            setError(`流式错误：${(err as Error).message}`);
          },
        );
        sseRef.current = handle;
      } catch (e) {
        setMode({ kind: 'idle' });
        setError(`提交失败：${(e as Error).message}`);
      }
    },
    [refreshSessions],
  );

  /** Abort the live stream, if any. Falls back to the durable facts already
   *  on disk (viewing(sid)) — or idle when the stream never identified itself. */
  const cancelStream = useCallback(() => {
    sseRef.current?.cancel();
    sseRef.current = null;
    setMode(
      liveSidRef.current
        ? { kind: 'viewing', sessionId: liveSidRef.current }
        : { kind: 'idle' },
    );
  }, []);

  /** Switch sessions. Cancels any live stream first (idempotent, no-op when idle) —
   *  the UI only ever presents the session the mode points at. */
  const selectSession = useCallback((id: string | null) => {
    sseRef.current?.cancel();
    sseRef.current = null;
    liveSidRef.current = null;
    setMode(id ? { kind: 'viewing', sessionId: id } : { kind: 'idle' });
  }, []);

  return {
    sessions,
    selectedId,
    conversation,
    loadingHistory,
    streaming,
    error,
    titlesById,
    selectSession,
    submitTask,
    cancelStream,
    refreshSessions,
  };
}
