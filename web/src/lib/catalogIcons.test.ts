/** #214：目录条目 `icon` 名 → 字形的三个分支（已知名 / 未知名 / 缺键）。
 *
 * 这里锁的是「按**后端声明的名**映射」这件事本身：未知名与缺键都必须留空槽——
 * 一旦有人在这里加"猜测兜底"（拿 id 猜、给未知名配默认图标），这两条会红。
 *
 * 名单本身**不在本文件里再抄一遍**（#217）：`catalogIcons.ts` 的 `CATALOG_ICON_NAMES` 字面量
 * 是前端唯一声明，"名单里每个名都配了字形"由 `Record<CatalogIconName, LucideIcon>` 在 `tsc`
 * 阶段保证（`tsc -b` 在门禁里），"与后端同集"由 `tests/web/test_web_phase5_staged_endpoints.py::
 * TestCatalogIcons` 的跨端对账保证。两件事都有各自的执行者，所以这里不再写运行时循环——
 * 那种循环在 `Record` 定型之后只能测到"有人改了 `KNOWN` 的构造"，属于**近似恒真**（REVIEW 时
 * 两个轴都指出过；同一形状此前已因恒真被删过一次，别再加回来）。 */

import { describe, expect, it } from 'vitest';
import { catalogIcon } from './catalogIcons';

describe('catalogIcon（#214：后端声明的语义名 → 字形）', () => {
  it('已知名 → 字形（三份目录各取一个代表）', () => {
    expect(catalogIcon('lock')).toBeDefined();
    expect(catalogIcon('code')).toBeDefined();
    expect(catalogIcon('telescope')).toBeDefined();
  });

  it('未知名 → undefined（不编字形）', () => {
    // 后端将来新增的名字，前端还没实现对应字形
    expect(catalogIcon('sparkles')).toBeUndefined();
    expect(catalogIcon('')).toBeUndefined();
  });

  it('缺键 / null → undefined（老载荷、部署自定义档位）', () => {
    expect(catalogIcon(undefined)).toBeUndefined();
    expect(catalogIcon(null)).toBeUndefined();
  });
});
