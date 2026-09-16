/** #201 档位收窄提示的纯函数单测。
 *
 * 为什么单独测：这段文案的全部价值是"如实"——数字错一位、把「通用」也提示一遍、
 * 或后端没给数据时自己编一个数，用户看到的都是一句像真话的假话，而界面上看不出来
 * （弹层内容在 vitest 里断言不了：本仓 vitest 是 SSR）。 */

import { describe, expect, it } from 'vitest';
import { MAX_LISTED_TOOLS, toolScopeNote } from './agentProfileScope';
import type { CatalogEntry } from './api';

const entry = (
  id: string,
  scope?: { open: number; total: number; excluded: string[] },
): CatalogEntry => ({
  id,
  display_name: id,
  description: '',
  ...(scope ? { tool_scope: scope } : {}),
});

const PROFILES: CatalogEntry[] = [
  entry('main', { open: 17, total: 17, excluded: [] }),
  entry('coding', {
    open: 12,
    total: 17,
    excluded: ['delegate', 'inspect_artifact', 'read_knowledge_source', 'retrieve_knowledge', 'web_search'],
  }),
  entry('research_review', {
    open: 7,
    total: 17,
    excluded: [
      'apply_patch', 'bash', 'delegate', 'edit', 'forget_memory',
      'git_diff', 'git_status', 'inspect_artifact', 'remember_this', 'write',
    ],
  }),
  entry('legacy'), // 老部署 / 老夹具：没有 tool_scope
];

describe('toolScopeNote', () => {
  it('逐字给出设计稿 §4 的那句话（数字取自后端 tool_scope）', () => {
    const note = toolScopeNote(PROFILES, 'coding');
    expect(note?.text).toBe('该档位只开放 12 个工具（共 17 个）');
  });

  it('未被收窄的档位（excluded 为空）不提示——「17 中开放 17」只是噪音', () => {
    expect(toolScopeNote(PROFILES, 'main')).toBeNull();
  });

  it('未选（null / 空串）按后端默认档位披露，而不是"不提示"', () => {
    // 未选的语义是"用后端默认值"，运行时落到 main（assembly.py:401）。
    // main 未被收窄 ⇒ 仍然不提示；这条锁的是"走的是 main 那条分支"——
    // 把默认档位写成 coding 就会凭空冒出一句提示（用户没选任何档位）。
    expect(toolScopeNote(PROFILES, null)).toBeNull();
    expect(toolScopeNote(PROFILES, '')).toBeNull();
    expect(toolScopeNote([entry('main', { open: 17, total: 17, excluded: [] })], null)).toBeNull();
  });

  it('后端没给 tool_scope（老部署）→ 不提示，也不编一个数', () => {
    expect(toolScopeNote(PROFILES, 'legacy')).toBeNull();
    expect(toolScopeNote([entry('main')], null)).toBeNull();
  });

  it('档位不在目录里（陈旧选中值）→ 不提示（不拿别的档位的数顶替）', () => {
    expect(toolScopeNote(PROFILES, 'ghost-profile')).toBeNull();
  });

  it('tooltip 列出被收窄掉的工具名，超过 6 个截断并加「…」', () => {
    const coding = toolScopeNote(PROFILES, 'coding');
    expect(coding?.title).toBe('未开放：delegate、inspect_artifact、read_knowledge_source、retrieve_knowledge、web_search');

    const research = toolScopeNote(PROFILES, 'research_review');
    const listed = research!.title.replace('未开放：', '').split('、');
    expect(listed).toHaveLength(MAX_LISTED_TOOLS); // 恰好 6 个名字
    expect(listed[listed.length - 1].endsWith('…')).toBe(true); // 还有没说出来的
    expect(research?.title).toContain('apply_patch、bash');
  });

  it('恰好 6 个被收窄时不加「…」（多了这个符号会暗示还有没说出来的）', () => {
    const exact = [entry('p', { open: 11, total: 17, excluded: ['a', 'b', 'c', 'd', 'e', 'f'] })];
    const note = toolScopeNote(exact, 'p');
    expect(note?.title).toBe('未开放：a、b、c、d、e、f');
  });
});
