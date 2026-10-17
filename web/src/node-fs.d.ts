// web 的 tsconfig.app 只带 vite/client 类型（不含 @types/node 的全局声明）。
// 本测试从仓库根读取 docs/ 下的静态样例，这里只为用到的 node:fs API 补最小声明。
declare module 'node:fs' {
  export function readFileSync(path: string | URL, encoding: 'utf8'): string;
}
