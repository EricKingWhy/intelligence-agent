/** F9：会话列表**加载失败**不得被渲染成「暂无会话，提交任务即可开始。」
 *
 *  机制与真机证据见 `docs/LIVE_BROWSER_TEST_20260917.md` §8.5：后端不可达时项目区挂着
 *  「项目列表加载失败：… 重试」，同一屏的会话区却写着一句**可被执行的假陈述**——
 *  用户会据此以为会话丢了。空态判据只有 `sessions.length === 0`，而失败时
 *  `sessions` 就是一个空数组，所以它无法自己分辨"真没有"与"没拿到"。
 *
 *  这里锁四件事，缺一条就会有假绿：
 *   - 失败 + 空数组 → **不出现**空态那句话，且出现错误条与重试；
 *   - 成功 + 真空   → 空态那句话**照旧出现**（证明上一条不是"把空态整片删了"）；
 *   - 失败 + 有旧数据 → 列表行照旧渲染（失败不清列表，也不改口说"暂无会话"）；
 *   - 项目与会话**同时**失败 → **两条**错误条都在（此前是 else-if 链，只显示第一条，
 *     第二条那个假空态就没人挡）。
 *
 *  断言取**结构**（`class="rail-error"` 计数、`.empty-hint` 的整句文本），不用
 *  子串碰运气：`toContain('重试')` 之类的弱断言在别处也会被满足。 */

import { describe, expect, it } from 'vitest';
import { createElement } from 'react';
import { renderToString } from 'react-dom/server';
import { SessionList } from './SessionList';
import type { Project, SessionSummary } from '../types';

function session(id: string, archived = false): SessionSummary {
  return {
    session_id: id,
    event_count: 3,
    first_event_time: '2026-09-17T10:00:00+08:00',
    last_event_time: '2026-09-17T10:01:00+08:00',
    first_user_message: `任务 ${id}`,
    trace_id: null,
    trace_url: null,
    workspace: null,
    archived,
  };
}

const noop = () => {};
/** 异步 handler 的统一替身。返回类型写 `Promise<never>`：本文件只做 SSR 渲染、
 *  从不触发这些回调，而各 handler 的返回类型互不相同（`onDeleteSession`
 *  → `Promise<SessionDeleted>`、`onSetArchived` → `Promise<string | null>`）——
 *  任何具体返回类型都会与其中之一冲突（`tsc -b` 就是这么红的）。`never` 对全部成立。 */
const noopAsync = async (): Promise<never> => {
  throw new Error('SSR 渲染不应触发回调');
};

function render(props: {
  sessions?: SessionSummary[];
  projects?: Project[];
  projectsError?: string | null;
  sessionsError?: string | null;
}): string {
  return renderToString(
    createElement(SessionList, {
      sessions: props.sessions ?? [],
      projects: props.projects ?? [],
      selectedId: null,
      liveSessionId: null,
      titlesById: {},
      onSelect: noop,
      onNew: noop,
      projectActions: {
        create: noopAsync as never,
        rename: noopAsync as never,
        remove: noopAsync as never,
        attach: noopAsync as never,
        detach: noopAsync as never,
        reorder: noopAsync as never,
      },
      onSessionsChanged: noop,
      projectsError: props.projectsError ?? null,
      sessionsError: props.sessionsError ?? null,
      onRetryProjects: noop,
      onStartTask: noopAsync,
      permissionModes: [],
      onDeleteSession: noopAsync,
      onSetArchived: noopAsync,
    }) as never,
  );
}

const EMPTY_HINT = '暂无会话，提交任务即可开始。';
const barCount = (html: string) => (html.match(/class="rail-error"/g) ?? []).length;
/** 去掉 SSR 在 `{表达式}` 与相邻字面量之间插的 `<!-- -->` 分隔符。
 *
 *  不是为了让断言"好过"：`项目列表加载失败：{projectsError}` 在 HTML 里逐字是
 *  `项目列表加载失败：<!-- -->加载项目失败（502）`，直接 `toContain` 整句会**永远红**
 *  ——那与「子串碰运气」相反，是另一种形式的假信号（断言没在测它声称测的东西）。 */
const plain = (html: string) => html.replace(/<!-- -->/g, '');

describe('会话列表加载失败（F9）', () => {
  it('失败 + 空数组：不说「暂无会话」，改说失败并给重试', () => {
    const html = render({ sessionsError: '加载会话列表失败：get sessions 502' });

    expect(html).not.toContain(EMPTY_HINT);
    expect(barCount(html)).toBe(1);
    expect(html).toContain('加载会话列表失败：get sessions 502');
    expect(html).toContain('重试');
    // 错误条必须有 role=alert（屏幕阅读器要被告知，不能只是视觉上的红）
    expect(html).toContain('role="alert"');
  });

  it('成功且真为空：空态那句话照旧出现（反证上一条不是因为把空态删了）', () => {
    const html = render({ sessionsError: null });

    expect(html).toContain(EMPTY_HINT);
    expect(barCount(html)).toBe(0);
  });

  it('失败 + 有旧数据：列表行照旧渲染（失败不清列表）', () => {
    const html = render({
      sessions: [session('s-1'), session('s-2')],
      sessionsError: '加载会话列表失败：get sessions 502',
    });

    expect(html).toContain('s-1');
    expect(html).toContain('s-2');
    expect(barCount(html)).toBe(1);
    // 这条**不**断言「不出现空态那句话」：sessions 非空时 `showEmpty` 本来就是 false，
    // 那句话无论如何都不会渲染——那样的断言永远绿，是在冒充证据（红证阶段实测发现：
    // 把本文件的空态守卫整条去掉，这条断言**照样通过**，只有下面那条「失败 + 空数组」
    // 会变红）。分辨"真没有"与"没拿到"的守卫只由那一条覆盖。
  });

  it('项目与会话同时失败：两条错误条都在（不是只显示第一条）', () => {
    const html = render({
      projectsError: '加载项目失败（502）',
      sessionsError: '加载会话列表失败：get sessions 502',
    });

    expect(barCount(html)).toBe(2);
    expect(plain(html)).toContain('项目列表加载失败：加载项目失败（502）');
    expect(plain(html)).toContain('加载会话列表失败：get sessions 502');
    // 两侧都有重试入口（同一个回调，但两个区域各自要有可点的东西）
    expect((html.match(/>重试<\/button>/g) ?? []).length).toBe(2);
  });
});
