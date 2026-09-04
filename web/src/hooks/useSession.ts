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

/** 流式帧 vs 当前模式一致性判别（不变量 #22：UI 不维护第二套真相）。
 *
 * SSE 流在 submitTask 时开启，回调闭包持有当时的 conv。若用户在流结束前
 * cancel / 切换会话 / 触发 error，mode 迁移到 viewing/idle，但 SSE 在途帧
 * 仍可能晚于这次 mode 变更到达——回调再写 conversation 就会用旧流残留
 * 覆盖刚加载的目标会话视图。
 *
 * 守护：每帧应用前先问「当前模式是否仍是这条流的权威消费者」。提取为
 * 纯函数是为了锁不变量——React 状态机本身的回归需 testing-library，
 * 引入它会扩大 scope；这一层契约纯函数就能锁住。 */
export function shouldApplyStreamFrame(mode: SessionMode, event: AgentEvent): boolean {
  // 离开 live 即丧失权威——viewing 由持久事件源重建，idle 无会话。
  if (mode.kind !== 'live') return false;
  // 首帧尚未确定 sid（sessionId === null）：任意帧都接受。
  if (mode.sessionId === null) return true;
  // 帧未带 sid（理论只会在首帧前出现）：宽容接受，避免误丢首帧。
  const eventSid = event.session_id ?? null;
  if (eventSid === null) return true;
  // sid 不匹配——旧流迟到帧窜入新会话，明确拒绝。
  return mode.sessionId === eventSid;
}

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
  // mode 的实时镜像——SSE 回调闭包在 submitTask 时定型，读到的 mode 是
  // 提交瞬间快照而非当前真相。镜像让每帧应用前能问「当前模式是否仍是
  // 这条流的权威消费者」（不变量 #22），从而拒绝 cancel / selectSession
  // 之后才到达的迟到帧。
  const modeRef = useRef<SessionMode>(mode);
  modeRef.current = mode;

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
            // 不变量 #22 守护：cancel / selectSession / error 之后才到达的
            // 在途帧不再具有权威——若放行会用旧流残留覆盖刚加载的目标视图。
            if (!shouldApplyStreamFrame(modeRef.current, event)) return;
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
