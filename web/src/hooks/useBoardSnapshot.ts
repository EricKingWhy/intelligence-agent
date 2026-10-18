/** useBoardSnapshot — #1305 看板数据源：WS 板级快照 + 降级拉取。
 *
 * ## 主路径（WS）
 *
 * `subscribeBoardSnapshot` 上行 `subscribe_board`，服务端**订阅即推一帧全量快照**
 * （票面 §7），之后由投影更新点经 250ms leading/trailing 节流推送。节流只在服务端
 * 做一次 ⇒ 本 hook **不做第二次节流**，收到即 setState。
 *
 * ## 降级（票面「降级」）
 *
 * WS 建连失败（握手被拒 / CSP / 非 WS 环境）**或已收到帧之后连接异常断开**⇒
 * 置 `degraded` 并退回「拉一次 `GET /api/sessions` + 手动刷新按钮」，**不引入轮询
 * 定时器**——轮询会与 `wsStream.ts:372/402/426` 的既有会话列表轮询语义打架
 * （票面「不做边界」）。两条触发路径都只**拉一次**（不是轮询）。
 *
 * `refresh()` 在**两条路径下都真的拉**：它是显式刷新（用户点按钮 / 拖拽落点后
 * 的主动 refetch），语义是"现在去问一次服务端"，与"WS 推来的帧要不要再节流"
 * 无关。WS 随后推来的帧会覆盖同一份数据（两侧同源，后到者胜，不产生第二真相）。
 *
 * 失败**不清列表**（同 `KanbanPanel.reload` 的既有纪律）：把已经拿到的行说成
 * "没有会话"是假陈述（不变量 #21）。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { listSessions } from '../lib/api';
import { subscribeBoardSnapshot } from '../lib/wsStream';
import type { SessionSummary } from '../types';

export interface BoardSnapshotState {
  /** 当前行（WS 推送或降级拉取的结果）。 */
  sessions: SessionSummary[];
  /** 是否已退回「拉一次 + 手动刷新」路径（WS 不可用或已断）。`KanbanPanel` 据此
   *  渲染降级提示——它不是一个只写不读的状态。 */
  degraded: boolean;
  /** 降级路径的最近一次错误文案（null = 没出错）。 */
  error: string | null;
  /** 手动刷新 / 落点后主动 refetch：**总是**真的拉一次（两条路径同语义）。 */
  refresh: () => Promise<void>;
}

/** 缺省数据源：**总是**要全量（含已归档），可见性由视图开关决定而不是由请求决定
 *  （#171 的纪律，不变量 #22）。唯一一份——`KanbanPanel` 不再自带同款默认值。 */
export const defaultLoadSessions = (): Promise<SessionSummary[]> =>
  listSessions({ includeArchived: true });

export function useBoardSnapshot(
  open: boolean,
  loadSessions: () => Promise<SessionSummary[]> = defaultLoadSessions,
): BoardSnapshotState {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [degraded, setDegraded] = useState(false);
  const [error, setError] = useState<string | null>(null);

  /** 订阅还活着的镜像：降级回调与卸载之间的竞态由它兜住（关板后迟到的
   *  失败回调不该再 setState）。 */
  const aliveRef = useRef(false);

  const pullOnce = useCallback(async () => {
    try {
      const rows = await loadSessions();
      if (!aliveRef.current) return;
      setSessions(rows);
      setError(null);
    } catch (e) {
      // 失败**不清**列表：已拿到的行仍是真值（同 KanbanPanel.reload）。
      if (!aliveRef.current) return;
      setError((e as Error).message || '加载会话列表失败');
    }
  }, [loadSessions]);

  useEffect(() => {
    if (!open) return;
    aliveRef.current = true;
    // 关板即断开：WS 订阅是有成本的（服务端要为每个订阅者算全量快照）。
    const subscription = subscribeBoardSnapshot(
      (rows) => {
        if (!aliveRef.current) return;
        setSessions(rows);
        setError(null);
      },
      (e) => {
        if (!aliveRef.current) return;
        // WS 不可用 / 已断 ⇒ 降级：拉一次（手动刷新按钮与降级提示据此出现）。
        setDegraded(true);
        setError(e.message);
        void pullOnce();
      },
    );
    return () => {
      aliveRef.current = false;
      subscription.close();
    };
  }, [open, pullOnce]);

  // 关板时复位降级态：下次打开重新尝试 WS（一次建连失败不该永久钉住降级路径）。
  useEffect(() => {
    if (open) return;
    // 对外部信号（open 由 true → false）的同步，不是从渲染值派生状态。
    // oxlint-disable-next-line react/set-state-in-effect
    setDegraded(false);
    setError(null);
  }, [open]);

  // 返回值 useMemo 稳定引用：`KanbanPanel.reload` 的依赖是 `board.refresh`，对象
  // 字面量每次渲染都是新引用会让依赖数组失去意义。
  return useMemo(
    () => ({ sessions, degraded, error, refresh: pullOnce }),
    [sessions, degraded, error, pullOnce],
  );
}

