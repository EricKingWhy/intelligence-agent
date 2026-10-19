/**
 * #1364 stylelint —— 设计 token 的工程约束（本仓唯一强制面 = **颜色字面量**）。
 *
 * 两条规则：
 *   `color-no-hex`                             —— 禁 `#rgb` / `#rrggbb`（含 4/8 位）。
 *   `declaration-property-value-disallowed-list` —— 禁 `rgb()/rgba()/hsl()/hsla()` 字面量。
 *     为什么需要第二条：`color-no-hex` **不覆盖**函数式颜色（#1364 实测：149 种颜色字面量里
 *     一半是 `rgba(0,0,0,x)` 这种黑色透明度档位）。
 *
 * 放行面只有三类，且都写成**显式注释**，不用按文件 overrides ——
 * 文件级 overrides 会把整个文件的字面量一起放掉，而同一个文件里既有 token 定义、也有消费点：
 *   ① token 定义块：`src/index.css` 与 `src/styles/constraints.css` 的 `:root` /
 *      `:root[data-theme='light']`，用成对 `stylelint-disable` / `stylelint-enable` 包住；
 *   ② `mask-image` 的 alpha 停靠点：那里的 `#000` 是「全不透明」而非颜色，逐行
 *      `stylelint-disable-next-line color-no-hex`；
 *   ③ `var(--bg-app, #000)` 兜底：fallback 语义（token 缺失时的最后防线），
 *      #1364 改动 5 明列豁免，逐行 disable。
 *
 * 运行（仓库根）：
 *   node web/node_modules/stylelint/bin/stylelint.mjs \
 *     --config web/stylelint.config.mjs "web/src/**\/*.css"
 * 或 `cd web && node node_modules/stylelint/bin/stylelint.mjs "src/**\/*.css"`
 * （gate0 的 `stylelint` 车道用的就是后者，见 `scripts/gate0.py`）。
 */
export default {
  rules: {
    'color-no-hex': true,
    'declaration-property-value-disallowed-list': {
      '/.*/': ['/rgba?\\(/', '/hsla?\\(/'],
    },
  },
};
