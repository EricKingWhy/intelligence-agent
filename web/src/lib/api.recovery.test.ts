/** #357 W-13 数据层契约测试（第一批：api.ts）。
 *
 * 覆盖：
 *   - PendingDecision 富化（default_action / risk_level / probe）解析与整组回落；
 *   - recoverSession(sid, decisions?) 请求体形状（旧空体行为保持）；
 *   - verdict 五值 / source 可选与 ≤2000 上限；
 *   - listInterruptedRecoveries 只读列表解析与脏形状整组回落。
 *
 * 后端口径以 `src/agent_harness/session/service.py::_reconcile_pending` 与
 * `interrupted_recovery_rows` + `web/app.py::list_interrupted_recoveries` 为准。
 * fetch 全局 mock（与 api.test.ts 同款），不发真实请求。
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  listInterruptedRecoveries,
  recoverSession,
  RecoverError,
  type PendingDecision,
  type RecoverDecisionInput,
} from './api';

/** 捕获 fetch（url + raw init），body 解析为 JSON（无 body 时为 undefined）。 */
function captureFetch(status = 200, body: unknown = {}) {
  const calls: { url: string; init: RequestInit | undefined; body: unknown }[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({
        url: String(url),
        init,
        body:
          typeof init?.body === 'string'
            ? (JSON.parse(init.body) as unknown)
            : undefined,
      });
      return new Response(JSON.stringify(body), { status });
    }),
  );
  return { calls };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

/** 富化后的 409 载荷条目（两种默认动作各一：replay_safe=RETRY / unsafe=DEFER）。 */
const RICH_PENDING: PendingDecision[] = [
  {
    tool_call_id: 'call_1',
    tool_name: 'read',
    state: 'RUNNING',
    default_action: 'RETRY',
    risk_level: 'low',
    probe: { verifiable: true, suggested_action: '重读目标文件确认内容是否已写入' },
  },
  {
    tool_call_id: 'call_2',
    tool_name: 'bash',
    state: 'UNKNOWN',
    default_action: 'DEFER',
    risk_level: 'high',
    probe: { verifiable: false, suggested_action: null },
  },
];

function conflictBody(pending: unknown) {
  return {
    detail: {
      message: '存在需要人工裁决的 UNKNOWN Operation',
      pending_decisions: pending,
    },
  };
}

async function catchRecover(p: Promise<unknown>): Promise<RecoverError> {
  const err = (await p.then(
    () => { throw new Error('should have thrown'); },
    (e: unknown) => e,
  )) as RecoverError;
  expect(err).toBeInstanceOf(RecoverError);
  return err;
}

describe('#357 PendingDecision 富化解析（契约 1/2/6）', () => {
  it('正常：default_action / risk_level / probe 逐字段透出（RETRY/low 与 DEFER/high 两类）', async () => {
    captureFetch(409, conflictBody(RICH_PENDING));
    const err = await catchRecover(recoverSession('s1'));
    expect(err.status).toBe(409);
    expect(err.pendingDecisions).toEqual(RICH_PENDING);
  });

  it('未知工具 fail-closed：default_action=DEFER / risk_level=high / verifiable=false 被如实承载', async () => {
    captureFetch(
      409,
      conflictBody([
        {
          tool_call_id: 'call_x',
          tool_name: 'mystery_tool',
          state: 'UNKNOWN',
          default_action: 'DEFER',
          risk_level: 'high',
          probe: { verifiable: false, suggested_action: null },
        },
      ]),
    );
    const err = await catchRecover(recoverSession('s1'));
    expect(err.pendingDecisions).toEqual([
      {
        tool_call_id: 'call_x',
        tool_name: 'mystery_tool',
        state: 'UNKNOWN',
        default_action: 'DEFER',
        risk_level: 'high',
        probe: { verifiable: false, suggested_action: null },
      },
    ]);
  });

  it('probe.suggested_action 为 null 合法（工具无 hint，零伪造）', async () => {
    captureFetch(
      409,
      conflictBody([
        {
          tool_call_id: 'call_1', tool_name: 'read', state: 'RUNNING',
          default_action: 'RETRY', risk_level: 'low',
          probe: { verifiable: false, suggested_action: null },
        },
      ]),
    );
    const err = await catchRecover(recoverSession('s1'));
    expect(err.pendingDecisions?.[0].probe.suggested_action).toBeNull();
  });

  it('新字段缺失（无 default_action）⇒ 整组回落 undefined（不造半真半假清单）', async () => {
    captureFetch(
      409,
      conflictBody([
        { tool_call_id: 'call_1', tool_name: 'read', state: 'RUNNING', risk_level: 'low', probe: { verifiable: true, suggested_action: null } },
      ]),
    );
    const err = await catchRecover(recoverSession('s1'));
    expect(err.message).toBe('存在需要人工裁决的 UNKNOWN Operation');
    expect(err.pendingDecisions).toBeUndefined();
  });

  it('risk_level 类型不对 ⇒ 整组回落 undefined', async () => {
    captureFetch(
      409,
      conflictBody([
        { tool_call_id: 'call_1', tool_name: 'read', state: 'RUNNING', default_action: 'RETRY', risk_level: 42, probe: { verifiable: true, suggested_action: null } },
      ]),
    );
    const err = await catchRecover(recoverSession('s1'));
    expect(err.pendingDecisions).toBeUndefined();
  });

  it('default_action 越出枚举（ABANDON）⇒ 整组回落 undefined', async () => {
    captureFetch(
      409,
      conflictBody([
        { tool_call_id: 'call_1', tool_name: 'read', state: 'RUNNING', default_action: 'ABANDON', risk_level: 'low', probe: { verifiable: true, suggested_action: null } },
      ]),
    );
    const err = await catchRecover(recoverSession('s1'));
    expect(err.pendingDecisions).toBeUndefined();
  });

  it('probe 子字段类型错（verifiable 非 bool）⇒ 整组回落 undefined', async () => {
    captureFetch(
      409,
      conflictBody([
        { tool_call_id: 'call_1', tool_name: 'read', state: 'RUNNING', default_action: 'RETRY', risk_level: 'low', probe: { verifiable: 'yes', suggested_action: null } },
      ]),
    );
    const err = await catchRecover(recoverSession('s1'));
    expect(err.pendingDecisions).toBeUndefined();
  });

  it('一条坏条目污染整组：两条里有一条缺 probe ⇒ 整组 undefined（禁止逐项过滤）', async () => {
    captureFetch(
      409,
      conflictBody([
        { tool_call_id: 'call_1', tool_name: 'read', state: 'RUNNING', default_action: 'RETRY', risk_level: 'low', probe: { verifiable: true, suggested_action: null } },
        { tool_call_id: 'call_2', tool_name: 'bash', state: 'UNKNOWN', default_action: 'DEFER', risk_level: 'high' },
      ]),
    );
    const err = await catchRecover(recoverSession('s1'));
    expect(err.pendingDecisions).toBeUndefined();
  });

  it('state 越出非终态集合 ⇒ 整组回落 undefined（回归：既有严格性保持）', async () => {
    captureFetch(
      409,
      conflictBody([
        { tool_call_id: 'call_1', tool_name: 'read', state: 'SUCCEEDED', default_action: 'RETRY', risk_level: 'low', probe: { verifiable: true, suggested_action: null } },
      ]),
    );
    const err = await catchRecover(recoverSession('s1'));
    expect(err.pendingDecisions).toBeUndefined();
  });
});

describe('#357 recoverSession 请求体（契约 3/4）', () => {
  const okEvents = [{ type: 'session/started', seq: 1 }];

  it('无 decisions ⇒ 旧行为：不发 body（保持纯恢复尝试语义）', async () => {
    const { calls } = captureFetch(200, okEvents);
    await recoverSession('s1');
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe('/api/sessions/s1/recover');
    expect(calls[0].init?.method).toBe('POST');
    expect(calls[0].init?.body).toBeUndefined();
  });

  it('decisions 非空 ⇒ body = {decisions:[{tool_call_id,verdict}]}（source 省略时不带键）', async () => {
    const { calls } = captureFetch(200, okEvents);
    await recoverSession('s1', [
      { tool_call_id: 'call_1', verdict: 'RETRY' },
      { tool_call_id: 'call_2', verdict: 'DEFER' },
    ]);
    expect(calls[0].body).toEqual({
      decisions: [
        { tool_call_id: 'call_1', verdict: 'RETRY' },
        { tool_call_id: 'call_2', verdict: 'DEFER' },
      ],
    });
  });

  it('source 有值 ⇒ 逐字进 body；空 decisions 数组 ⇒ 仍按无 decisions（空体）', async () => {
    const { calls } = captureFetch(200, okEvents);
    await recoverSession('s1', [
      { tool_call_id: 'call_1', verdict: 'CONFIRM_SUCCESS', source: '我查了外部系统，写库成功' },
    ]);
    expect(calls[0].body).toEqual({
      decisions: [
        { tool_call_id: 'call_1', verdict: 'CONFIRM_SUCCESS', source: '我查了外部系统，写库成功' },
      ],
    });

    const { calls: c2 } = captureFetch(200, okEvents);
    await recoverSession('s1', []);
    expect(c2[0].init?.body).toBeUndefined();
  });

  it('verdict 五值均可透传', async () => {
    const verdicts = ['CONFIRM_SUCCESS', 'CONFIRM_FAILURE', 'RETRY', 'ABANDON', 'DEFER'] as const;
    for (const verdict of verdicts) {
      const { calls } = captureFetch(200, okEvents);
      await recoverSession('s1', [{ tool_call_id: 'call_1', verdict }]);
      expect((calls[0].body as { decisions: { verdict: string }[] }).decisions[0].verdict).toBe(verdict);
    }
  });

  it('重复提交：同一 decisions 连发两次，请求体逐字相同（客户端幂等）', async () => {
    const { calls } = captureFetch(200, okEvents);
    const decisions: RecoverDecisionInput[] = [
      { tool_call_id: 'call_1', verdict: 'RETRY' },
      { tool_call_id: 'call_2', verdict: 'DEFER', source: '稍后再看' },
    ];
    await recoverSession('s1', decisions);
    await recoverSession('s1', decisions);
    expect(calls).toHaveLength(2);
    expect(calls[0].body).toEqual(calls[1].body);
  });

  it('source 恰好 2000 字符 ⇒ 合法发送', async () => {
    const { calls } = captureFetch(200, okEvents);
    const source = 'x'.repeat(2000);
    await recoverSession('s1', [{ tool_call_id: 'call_1', verdict: 'CONFIRM_SUCCESS', source }]);
    expect((calls[0].body as { decisions: { source: string }[] }).decisions[0].source).toHaveLength(2000);
  });

  it('source 超 2000 字符 ⇒ 抛错且不发请求（审计来源不得静默截断）', async () => {
    const { calls } = captureFetch(200, okEvents);
    await expect(
      recoverSession('s1', [
        { tool_call_id: 'call_1', verdict: 'CONFIRM_SUCCESS', source: 'x'.repeat(2001) },
      ]),
    ).rejects.toThrow(/2000/);
    expect(calls).toHaveLength(0);
  });

  it('409 仍抛 RecoverError（带 pendingDecisions），非 2xx 非 404/409 抛恢复失败', async () => {
    captureFetch(409, conflictBody(RICH_PENDING));
    const err = await catchRecover(recoverSession('s1', [{ tool_call_id: 'call_1', verdict: 'RETRY' }]));
    expect(err.status).toBe(409);

    captureFetch(500, { detail: 'boom' });
    await expect(recoverSession('s1')).rejects.toThrow('恢复失败（500）');
  });
});

describe('#357 listInterruptedRecoveries（契约 5，只读列表）', () => {
  /** 后端 `interrupted_recovery_rows` 的完整一行（可读进度文件形态）。 */
  const ITEM = {
    session_id: 's-1',
    recovery: 'needs_manual_reconcile',
    detail: '有 1 个 UNKNOWN Operation',
    interrupted_runs: [
      { run_id: 'r-1', interrupted_seq: 7, step_id: 3, agent_id: 'main' },
      { run_id: null, interrupted_seq: 9, step_id: null, agent_id: null },
    ],
    resume_available: true,
    task: '把账单导出到 CSV',
    workspace_root: '/home/user/work',
    progress: { schema_version: '1', source_event_seq: 42 },
  };

  it('正常：snapshot_available + items 逐字段透传（未知字段不伪造、不补默认）', async () => {
    const { calls } = captureFetch(200, { snapshot_available: true, items: [ITEM] });
    const data = await listInterruptedRecoveries();
    expect(calls[0].url).toBe('/api/recovery/interrupted');
    expect(data).toEqual({ snapshot_available: true, items: [ITEM] });
  });

  it('snapshot_available=false + items=[]：合法快照缺失态', async () => {
    captureFetch(200, { snapshot_available: false, items: [] });
    await expect(listInterruptedRecoveries()).resolves.toEqual({
      snapshot_available: false,
      items: [],
    });
  });

  it('进度文件缺失/不可读态：{status, reason} 形状如实透传（不伪造"最新"）', async () => {
    captureFetch(200, {
      snapshot_available: true,
      items: [
        { ...ITEM, progress: { status: 'missing', reason: '进度文件不存在' } },
        { ...ITEM, session_id: 's-2', progress: { status: 'unreadable', reason: '权限不足' } },
      ],
    });
    const data = await listInterruptedRecoveries();
    expect(data?.items[0].progress).toEqual({ status: 'missing', reason: '进度文件不存在' });
    expect(data?.items[1].progress).toEqual({ status: 'unreadable', reason: '权限不足' });
  });

  it('信封脏形状：snapshot_available 非 bool / items 非数组 ⇒ 整份回落 undefined', async () => {
    captureFetch(200, { snapshot_available: 'yes', items: [] });
    await expect(listInterruptedRecoveries()).resolves.toBeUndefined();

    captureFetch(200, { snapshot_available: true, items: {} });
    await expect(listInterruptedRecoveries()).resolves.toBeUndefined();

    captureFetch(200, null);
    await expect(listInterruptedRecoveries()).resolves.toBeUndefined();
  });

  it('条目脏形状（缺 session_id / resume_available 非 bool / progress 越形状）⇒ 整组回落 undefined', async () => {
    captureFetch(200, { snapshot_available: true, items: [{ ...ITEM, session_id: undefined }] });
    await expect(listInterruptedRecoveries()).resolves.toBeUndefined();

    captureFetch(200, { snapshot_available: true, items: [{ ...ITEM, resume_available: 1 }] });
    await expect(listInterruptedRecoveries()).resolves.toBeUndefined();

    captureFetch(200, { snapshot_available: true, items: [{ ...ITEM, progress: { weird: true } }] });
    await expect(listInterruptedRecoveries()).resolves.toBeUndefined();
  });

  it('一条坏条目污染整组（另一条合法也不返回）', async () => {
    captureFetch(200, {
      snapshot_available: true,
      items: [ITEM, { ...ITEM, session_id: '' }],
    });
    await expect(listInterruptedRecoveries()).resolves.toBeUndefined();
  });

  it('非 2xx ⇒ 抛错（不静默当空列表）', async () => {
    captureFetch(500, { detail: 'boom' });
    await expect(listInterruptedRecoveries()).rejects.toThrow(/500/);
  });
});
