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
  it('一句话用**声明**口径（数字取自后端 tool_scope，措辞不得断言部署事实）', () => {
    const note = toolScopeNote(PROFILES, 'coding');
    // 措辞锁："声明开放"这几个字是批 2 Spec 轴 P1 的修复点——原稿写「只开放 12 个
    // 工具」，而本部署实际注册的是另一个集合（默认部署只注册 9 个内置工具，
    // 且本地 artifact 下 read_artifact 会被收窄却不在这份 excluded 里）。
    // 改成"某某声明了 N 个"后逐字为真。**不要**把它改回"开放 N 个"。
    expect(note?.text).toBe('该档位声明开放 12 个工具（全部档位声明 17 个）');
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

  it('tooltip 带档位名归属（footer 说的是当前档位，不是高亮那一行）', () => {
    // 列表里鼠标移动即高亮，footer 却描述**当前生效**档位；不带名字会被读成
    // "这是高亮那一行的信息"。夹具里 display_name === id，故这里等于 id。
    expect(toolScopeNote(PROFILES, 'coding')?.title).toContain('coding未声明开放：');
  });

  it('tooltip 列出被收窄掉的工具名，超过 6 个截断并加「、…」', () => {
    const coding = toolScopeNote(PROFILES, 'coding');
    expect(coding?.title).toBe('coding未声明开放：delegate、inspect_artifact、read_knowledge_source、retrieve_knowledge、web_search');

    const research = toolScopeNote(PROFILES, 'research_review');
    const listed = research!.title.replace('research_review未声明开放：', '').split('、');
    expect(listed[listed.length - 1]).toBe('…'); // 还有没说出来的
    expect(listed.slice(0, -1)).toHaveLength(MAX_LISTED_TOOLS); // 恰好 6 个名字
    expect(research?.title).toContain('apply_patch、bash');
  });

  it('恰好 6 个被收窄时不加「…」（多了这个符号会暗示还有没说出来的）', () => {
    const exact = [entry('p', { open: 11, total: 17, excluded: ['a', 'b', 'c', 'd', 'e', 'f'] })];
    const note = toolScopeNote(exact, 'p');
    expect(note?.title).toBe('p未声明开放：a、b、c、d、e、f');
  });
});
