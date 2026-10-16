/** useDelegations — #1256 子智能体 roster 数据源。
 *
 * 数据来自后端只读端点 `GET /api/sessions/{id}/delegations`（该端点自身从**父会话
 * 事件流**派生——UI 不建第二套真相，不变量 #22）。父流委派事件（started / finished）
 * 到达时重拉：子智能体完成 → 投影 status 翻转 → 刷新（不刷新页面，靠父流事件）。
 * **不订阅子会话 ws**（分栏内子会话实时流式放二期）。
 *
 * 读失败 → 空 roster（不伪造；PRODUCT「真实优先于好看」）。
 */

import { useEffect, useMemo, useState } from 'react';
import { getSessionDelegations, type DelegationEntry } from '../lib/api';
import type { ConversationState } from '../types';

export function useDelegations(
  sessionId: string | null,
  conversation: ConversationState | null,
): DelegationEntry[] {
  const [entries, setEntries] = useState<DelegationEntry[]>([]);

  /** 父流委派状态签名：started 出现、finished 到达（status 翻转）都改变它。
   *  只数投影里的委派（O(turns)），**不扫全量 events**——流式期间每个 delta 都会
   *  重算，全量 filter 会随事件数线性变贵。 */
  const signature = useMemo(() => {
    const parts: string[] = [];
    for (const turn of conversation?.turns ?? []) {
      for (const d of turn.delegations ?? []) {
        parts.push(`${d.child_session_id}:${d.status}`);
      }
    }
    return parts.join('|');
  }, [conversation]);

  useEffect(() => {
    if (!sessionId) {
      setEntries([]);
      return;
    }
    let alive = true;
    getSessionDelegations(sessionId)
      .then((rows) => {
        // 非数组形态（老后端 / 代理回整块对象 / 测试桩回 `{}`）= 拿不到 roster ⇒
        // 空，不把畸形响应当行列表喂给视图（`SubagentRoster` 只接受数组）。
        if (alive) setEntries(Array.isArray(rows) ? rows : []);
      })
      .catch(() => {
        if (alive) setEntries([]);
      });
    return () => {
      alive = false;
    };
  }, [sessionId, signature]);

  return entries;
}
