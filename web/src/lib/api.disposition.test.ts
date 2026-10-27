/** #1538：`web/src/lib/api.ts` 零引用导出的处置钉子。
 *
 * Q19 的三个导出逐项核过：`getHealth`、`createWorktree` 在 `web/src` 与 `web/e2e`
 * 均无引用（`api.ts` 是 `private: true` 的前端包，不构成对外发布面），已删；
 * `getContextProviders` 源码注释里带明文保留理由（端点仍在、看板 #200 与供应商
 * 管理 #203 会再评估），保留。
 *
 * 反向纪律：两条删除断言在改动前是红的，改动后转绿——本文件即反控证据。
 */

import { describe, expect, it } from 'vitest';
import * as api from './api';

describe('#1538 已删导出', () => {
  it('getHealth 不再导出', () => {
    expect('getHealth' in api).toBe(false);
  });

  it('createWorktree 不再导出', () => {
    expect('createWorktree' in api).toBe(false);
  });
});

describe('#1538 保留导出', () => {
  it('getContextProviders 仍在（注释里写明的保留决定）', () => {
    expect(typeof api.getContextProviders).toBe('function');
  });
});
