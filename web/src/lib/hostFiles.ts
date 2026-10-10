/**
 * 桌面宿主文件入口（#826 / MM-05 / AC1–AC3）：宿主路径桥的**消费侧**、拖入分流与 `@path` 引用语法。
 *
 * 它跑在**同一份 React 应用**里（Web 与桌面共用，AC4/AC6）——所以「桌面行为」不是另一条代码
 * 路径，而是同一条路上多了一个桥：
 * - 桥缺席（浏览器里的 Web 页面，永远是这一条）⇒ 每个文件都进既有上传入口，行为逐字不变
 *   （非图片照旧得到 `attachments.ts::partitionIntake` 的「不支持的图片格式」拒绝）；
 * - 桥在场（Electron preload 暴露 `window.__IA_HOST_PATHS__`）⇒ **有真实路径且非图片**的文件
 *   转成 `@path` 引用（不上传字节），图片与剪贴板字节仍走上传（AC2）。
 *
 * ## 来源与判定（AGENTS.md §6.1）
 *
 * - **ADAPT** DeepSeek Harness（MIT，commit `5badb15009ae1756c3afe0ae0cef1faafc290ccc`）
 *   - `packages/client/ui-conversation/src/client/apply.ts:99-109`：`HostPathBridge` 的形状
 *     （单方法 `pathFor(File) → string`）与 `hostPathBridge()` 的读法（全局缺席即 undefined）。
 *   - `apply.ts:460-497`：分流判据 `path === '' || (!directory && isImage)` → 上传，否则
 *     生成引用。本仓逐字保留「空路径 / 图片 → 上传」这一条，它是 AC2 的全部内容。
 *   - `packages/context/file-reference/src/grammar.ts:45-59`：`formatFileMention` 的
 *     **引号形态**（含空白 ⇒ `@"path"`）与**放弃规则**（含控制字符或 `"` 的路径不生成引用）。
 * - 第二独立来源 **VS Code**（`microsoft/vscode`，MIT，main @ `a64c64ab`）
 *   `src/vs/platform/dnd/browser/dnd.ts:566-585`：消费侧「先判宿主再用、缺失即降级」的
 *   防御式访问器（非 native 或桥不在时返回 `undefined`，不抛、不崩 UI）。本文件的
 *   `hostPathFor` 沿用同一条纪律：**任何异常/怪形状都降级为「无路径」**。
 *   许可全文与改动说明：`web/THIRD_PARTY_NOTICES.md`。
 *
 * ## 与上游的三处有意差异
 *
 * 1. **不做目录引用**：票面「明确不做」含「目录选择等既有能力的扩大」，而 DSH 会给目录也
 *    生成引用（`kind: 'directory'`，并在无桥时报 `directoryDesktopOnly`）。本仓目录仍按
 *    `useDraftAttachments` 的既有行为**被丢弃**（`dropEvents.ts::droppedDirectories` 识别）。
 * 2. **引用以文本插入输入框**，不做 DSH 的 rail chip：本仓后端还没有文件引用通道
 *    （PRD：「通用文件只做 `@path`/引用，不进 provider」），chip 会变成"看着可删、实际没有
 *    任何序列化落点"的假 UI；文本引用可删、可编辑、随 `content` 一起发送，是这条通道上
 *    唯一真实存在的形态。
 * 3. **不移植 `relativizeToCwd`**（`packages/util/workspace-path`）：本仓 Composer 接缝上
 *    没有会话 cwd（那是创建期字段，不随会话快照下发到这里），而绝对路径信息更全、不会
 *    因为猜错根目录而把一个可用的路径改坏。
 */

import { getImageLimits } from './attachments';

/**
 * preload 暴露窄桥的全局名。与 `desktop/src/preload.cts` 的字面量**必须**同值——preload 在
 * 沙箱 renderer 里没有任何模块解析，两边无法共享一个 import；漂移守卫在
 * `desktop/test/preload-host-paths.test.ts`（它读得到本文件与 preload 两边）。改一边不改
 * 另一边不会红在 Web，而是**静默降级**：桌面拖入的非图片文件全部退回上传。
 */
export const HOST_PATHS_GLOBAL = '__IA_HOST_PATHS__';

/** 宿主路径桥的形状（= preload 里那个单方法对象）。 */
export interface HostPathsBridge {
  /** 该 File 的宿主绝对路径；对没有磁盘后端的 File（剪贴板字节、页面里构造的 File）返回 `''`。 */
  pathFor(file: File): string;
}

/**
 * 当前文档里的宿主路径桥；缺席或形状不对 → `undefined`（Web 页面永远走这条）。
 *
 * 形状用 `typeof` 逐层判而不是信全局：桥是 renderer 里可被页面脚本改写的全局（连键本身都
 * 可能是抛错访问器），一个坏形状——或一次抛错的探测——必须降级成"没有桥"，而不是让拖入
 * 路径炸在 `pathFor is not a function` 上（VS Code 同款纪律）。
 */
export function hostPathsBridge(scope: unknown = globalThis): HostPathsBridge | undefined {
  if (typeof scope !== 'object' || scope === null) return undefined;
  // Named assertion, one reason: `globalThis` is a page-script-writable bag with no
  // schema to parse; every member below is checked at runtime before it is used.
  const host = scope as Record<string, unknown>;
  // 探测整体包进 try——**包括全局键本身的那一次读**：键也可以由页面脚本控制（抛错 accessor、
  // Proxy 的 `get` 陷阱），而它同样是"坏形状"的一种。异常一旦逸出本函数，就会穿过
  // `routeHostFiles` → Composer `addFiles` → `dropEvents.onDrop` 把整次拖入静默吞掉（文件既不进
  // 引用也不进上传）；坏形状的唯一合法归宿还是"没有桥"（上一段承诺的降级纪律）。
  let pathFor: unknown;
  let target: object | undefined;
  try {
    // 形状探测本身也是**可抛点**：`in` 会走 Proxy 的 `has` 陷阱、属性读取会走 getter，
    // 而这两样都由页面脚本控制（暴力测试 R1/R3 实测：`has` 陷阱抛错会让拖入路径上抛出一个
    // 未捕获错误——静默吞掉整次投递）。
    const candidate = host[HOST_PATHS_GLOBAL];
    if (typeof candidate !== 'object' || candidate === null) return undefined;
    const probe = candidate as { pathFor?: unknown }; // Named assertion: 页面全局无 schema，逐成员运行期判。
    if (!('pathFor' in probe)) return undefined;
    pathFor = probe.pathFor;
    target = candidate;
  } catch {
    return undefined;
  }
  if (typeof pathFor !== 'function' || target === undefined) return undefined;
  const lookup = pathFor; // `const` 绑定：闭包里保住收窄后的函数类型。
  const self = target;
  return {
    // `call` binds back to the bridge object: a contextBridge proxy is not promised
    // to stay callable once detached. Non-string answers are flattened here so the
    // returned bridge keeps its declared shape — and the call itself is a throwable
    // point too (a detached proxy, a page-script wrapper), so it becomes "no path"
    // instead of escaping into the drag path.
    pathFor: (file: File) => {
      try {
        const path: unknown = lookup.call(self, file);
        return typeof path === 'string' ? path : '';
      } catch {
        return '';
      }
    },
  };
}

/**
 * 该 File 的宿主真实路径；**无桥 / 非磁盘后端 / 桥抛错 / 返回怪类型** 一律 `''`。
 *
 * 永不抛：它挂在拖放与粘贴这两条**用户动作**路径上，一个坏桥必须表现为"退化成上传"，
 * 而不是让文件拖进来时界面炸掉。`''` 正是分流判据里"没有真实路径"的那一侧（AC2）。
 */
export function hostPathFor(file: File, bridge: HostPathsBridge | undefined = hostPathsBridge()): string {
  if (bridge === undefined) return '';
  try {
    const path = bridge.pathFor(file);
    return typeof path === 'string' ? path : '';
  } catch {
    return '';
  }
}

/**
 * 把真实路径写成 `@path` 引用文本；**无法安全表示**的路径返回 `null`。
 *
 * 判据逐字移植 DSH `grammar.ts:45-59`：含控制字符（`\u0000-\u001f`、`\u007f-\u009f`）或
 * 双引号的路径既不能裸写、也不能加引号写（引号会破坏引用语法，控制字符会把换行/终端转义
 * 带进消息），因此**放弃**转引用——调用方必须回退到上传，不许把它当引用静默吞掉。
 */
export function formatFileMention(path: string): string | null {
  if (/[\u0000-\u001f\u007f-\u009f"]/u.test(path)) return null;
  return /\s/u.test(path) ? `@"${path}"` : `@${path}`;
}

/** 一次拖入/粘贴分流的结果：走上传的与转引用的（按用户给出的顺序各成一组）。 */
export interface HostFileRouting {
  /** 进入既有上传入口的文件（图片、剪贴板字节、无桥时的任何文件、目录、不可表示的路径）。 */
  uploads: File[];
  /** 有真实路径的非图片文件转成的 `@path` 引用文本。 */
  references: string[];
}

/**
 * 拖入分流（AC2）：`path === '' || 图片 → uploads`，否则 `@path 引用`。
 *
 * @param files - 本次投递的文件（`dropEvents.ts` 已剔除空目录壳之外的原始数组）。
 * @param options.directories - 被识别为目录的成员：**强制走 uploads**，由
 *   `useDraftAttachments` 按既有行为丢弃（本票不做目录引用，见模块头差异 ①）。
 * @param options.mediaTypes - 图片判定集合；默认取当前生效的 `getImageLimits().mediaTypes`
 *   （#937/M-08：服务端下发成功即服务端值，否则离线 fallback `IMAGE_LIMITS`），不在这里另立第二套。
 */
export function routeHostFiles(
  files: readonly File[],
  options: { directories?: ReadonlySet<File>; mediaTypes?: readonly string[] } = {},
): HostFileRouting {
  const mediaTypes = options.mediaTypes ?? getImageLimits().mediaTypes;
  // 一次拖入只查一次桥：同一次投递里每个文件看到的是**同一个**桥（页面脚本在循环中途
  // 改全局不该让同一批文件一半走引用、一半走上传）。
  const bridge = hostPathsBridge();
  const uploads: File[] = [];
  const references: string[] = [];
  for (const file of files) {
    // 目录拿不到"非图片"的判定（`File.type` 是空串）——直接归 uploads，让顶层按既有规则丢弃。
    const path = options.directories?.has(file) === true ? '' : hostPathFor(file, bridge);
    if (path === '' || mediaTypes.includes(file.type)) {
      uploads.push(file);
      continue;
    }
    const mention = formatFileMention(path);
    if (mention === null) {
      uploads.push(file);
      continue;
    }
    references.push(mention);
  }
  return { uploads, references };
}
