/** #1321 验收 3：慢负载下连续 20 次提交认领，零「未认领到新会话」误报。
 *
 * 负载定义（写死为票面复现步骤 3 的方式 a）：
 *  - `GET /api/sessions` 每次往返 **1500ms**（票面要求 ≥1500ms/请求）；
 *  - 新会话在「提交开始」后 **12s** 才对列表可见（共享后端并发提交 /
 *    create_and_launch 排队时的落盘延迟；票面实测 6s 内列表里看不到它）；
 *  - 每次提交按真实时序走：`sessionIdBaseline()`（1.5s）→ 模拟
 *    `raceEarlyResponse` 的 1200ms 判别窗 → `discoverNewSessionId()`。
 *
 * 这个文件刻意**只使用旧版已有的 API**（不传 options）：同一份脚本在基线
 * 6s 预算上必须红、在修复后的 20s 预算上必须绿——红绿证据不依赖新常量。
 *
 * 默认跳过（20 × ~17s ≈ 6 分钟），显式运行：
 *   cd web && IA_CLAIM_LOAD_1321=1 node node_modules/vitest/vitest.mjs run \
 *     src/lib/wsStream.claimLoad.test.ts
 */

import { afterEach, describe, expect, it, vi } from 'vitest';

import { discoverNewSessionId, sessionIdBaseline } from './wsStream';

const ENV = (globalThis as { process?: { env?: Record<string, string | undefined> } })
  .process?.env;
const RUN = ENV?.IA_CLAIM_LOAD_1321 === '1';
const SUBMISSIONS = Number(ENV?.IA_CLAIM_SUBMISSIONS ?? 20);
const LIST_LATENCY_MS = 1500;
const CREATE_VISIBLE_MS = 12_000;

describe.runIf(RUN)('#1321 慢负载验收（20 次连续提交）', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it(
    '20/20 零「未认领到新会话」；记录列表延迟读数',
    async () => {
      // 空跑防线：IA_CLAIM_SUBMISSIONS 非正整数（如 `abc` → NaN）会让循环整体
      // 跳过、failures 保持 []，脚本假绿。先失败，再谈 20/20。
      expect(
        Number.isInteger(SUBMISSIONS) && SUBMISSIONS > 0,
        `IA_CLAIM_SUBMISSIONS 必须是正整数（实际：${String(ENV?.IA_CLAIM_SUBMISSIONS)}）`,
      ).toBe(true);
      /** session_id → 对列表可见的绝对时刻（base 行恒可见）。 */
      const visibleAt = new Map<string, number>();
      for (let i = 0; i < 40; i += 1) visibleAt.set(`base-${i}`, 0);

      const listLatencies: number[] = [];
      vi.stubGlobal(
        'fetch',
        vi.fn(async () => {
          const startedAt = Date.now();
          await new Promise((r) => setTimeout(r, LIST_LATENCY_MS));
          listLatencies.push(Date.now() - startedAt);
          const now = Date.now();
          const rows = [...visibleAt.entries()]
            .filter(([, at]) => at <= now)
            .map(([session_id]) => ({ session_id }));
          return new Response(JSON.stringify(rows), { status: 200 });
        }),
      );

      const elapsed: number[] = [];
      const failures: string[] = [];
      for (let i = 0; i < SUBMISSIONS; i += 1) {
        const submitStartedAt = Date.now();
        const baseline = await sessionIdBaseline();
        expect(baseline, `第 ${i + 1} 次基线读不到`).not.toBeNull();

        const fresh = `fresh-${i}`;
        visibleAt.set(fresh, submitStartedAt + CREATE_VISIBLE_MS);
        // 交付层攒包判别窗：窗外才判定为正常流式并转入认领（useSession）。
        await new Promise((r) => setTimeout(r, 1200));

        try {
          const sid = await discoverNewSessionId(baseline!);
          if (sid !== fresh) failures.push(`#${i + 1} 认领到 ${sid}（应为 ${fresh}）`);
        } catch (e) {
          failures.push(`#${i + 1} ${(e as Error).message}`);
        }
        elapsed.push(Date.now() - submitStartedAt);
        // 下一次提交的基线必须已经包含本次会话（真实用户看到失败后会先刷新/
        // 等下一条列表刷新；否则上一轮迟到的会话会被下一轮差分误认成新会话，
        // 那是另一个缺陷，不能混进本票的 20/20 读数）。
        const visible = visibleAt.get(fresh)!;
        const wait = visible - Date.now();
        if (wait > 0) await new Promise((r) => setTimeout(r, wait));
      }

      const sorted = [...listLatencies].sort((a, b) => a - b);
      const p50 = sorted[Math.floor(sorted.length * 0.5)];
      const p99 = sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.99))];
      // 机器可读读数：沙箱日志直接取这一行。
      console.log(
        `[#1321] submissions=${SUBMISSIONS} passed=${SUBMISSIONS - failures.length} ` +
          `list_calls=${listLatencies.length} p50_ms=${p50} p99_ms=${p99} ` +
          `submit_elapsed_ms=${JSON.stringify(elapsed)} failures=${JSON.stringify(failures)}`,
      );

      // 有提交就必然有列表往返；为零说明 fetch 桩未生效或循环空跑。
      expect(listLatencies.length).toBeGreaterThan(0);
      expect(failures, failures.join('; ')).toEqual([]);
    },
    600_000,
  );
});
