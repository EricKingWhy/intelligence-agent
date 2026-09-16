/** #214：目录条目 `icon` 名 → 字形的三个分支（已知名 / 未知名 / 缺键）。
 *
 * 这里锁的是「按**后端声明的名**映射」这件事本身：未知名与缺键都必须留空槽——
 * 一旦有人在这里加"猜测兜底"（拿 id 猜、给未知名配默认图标），这两条会红。 */

import { describe, expect, it } from 'vitest';
import { CATALOG_ICON_NAMES, catalogIcon } from './catalogIcons';

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

  it('已知名集合是后端 CATALOG_ICON_NAMES 的镜像，且每个名都有字形', () => {
    // 逐值：这一串与后端 `web/app.py::CATALOG_ICON_NAMES` 同集（跨语言手工镜像，
    // 增删名必须两端一起改；后端侧由 tests/web/test_web_phase5_staged_endpoints.py
    // 的 TestCatalogIcons 锁同一串）
    expect([...CATALOG_ICON_NAMES].sort()).toEqual([
      'bolt', 'code', 'gauge', 'layers', 'lock', 'pencil', 'search', 'telescope', 'unlock',
    ]);
    // 上面那串字面量里的每个名都必须真的映射出字形。**遍历字面量而不是遍历集合**：
    // 集合就是 `Object.keys(ICONS)`，遍历它恒真、发现不了任何东西（写错过一版）。
    for (const name of ['bolt', 'code', 'gauge', 'layers', 'lock', 'pencil', 'search', 'telescope', 'unlock']) {
      expect(catalogIcon(name)).toBeDefined();
    }
  });
});
