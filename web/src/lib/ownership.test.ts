/** #1028：归属 chip 的文案纯函数。
 *
 *  归属判据的**唯一权威是后端**（会话 cwd 与项目路径 canonical 精确相等）；本函数
 *  只负责把「项目名 / cwd」这两个已知量翻成给用户看的一句话，不做任何路径比较——
 *  前端自己比对路径就是第二套真相（不变量 #22）。因此这里锁的是**文案分支**：
 *  - 有项目 + 有 cwd → 项目名与 cwd 都在（AC2 的「项目名 + cwd」）；
 *  - 未归属 + 有 cwd → 「未分组（cwd: …）」（AC3 的警示载体）；
 *  - 未归属 + 无 cwd → 明说未指定目录，不编一个字面路径冒充。 */

import { describe, expect, it } from 'vitest';
import { describeOwnership } from './ownership';

describe('describeOwnership（#1028）', () => {
  it('归属项目且有 cwd：项目名与 cwd 都在，kind=project', () => {
    const o = describeOwnership('kb-proj', '/home/hatch/kb-proj');
    expect(o.kind).toBe('project');
    expect(o.projectTitle).toBe('kb-proj');
    expect(o.cwd).toBe('/home/hatch/kb-proj');
    expect(o.label).toBe('项目「kb-proj」 · /home/hatch/kb-proj');
  });

  it('归属项目但 cwd 未知：只说项目名，不编路径', () => {
    const o = describeOwnership('kb-proj', null);
    expect(o.kind).toBe('project');
    expect(o.label).toBe('项目「kb-proj」');
  });

  it('未归属但有 cwd：明示「未分组（cwd: …）」（chip 即警示）', () => {
    const o = describeOwnership(null, '/home/hatch');
    expect(o.kind).toBe('ungrouped');
    expect(o.label).toBe('未分组（cwd: /home/hatch）');
  });

  it('未归属且 cwd 未知：说未指定目录，不编绝对路径', () => {
    const o = describeOwnership(null, null);
    expect(o.kind).toBe('ungrouped');
    expect(o.label).toBe('未分组（未指定目录）');
    expect(o.label).not.toContain('/');
  });

  it('title 携带完整上下文（悬停可读到完整 cwd）', () => {
    expect(describeOwnership('p', '/x/y').title).toContain('/x/y');
    expect(describeOwnership(null, '/x/y').title).toContain('/x/y');
  });
});
