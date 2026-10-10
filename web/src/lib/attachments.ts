/**
 * 附图入口的纯函数层（#825 / MM-04）与上限状态（#937 / M-08）。
 *
 * `filesFromClipboard` / `partitionIntake` / `budgetText` / `detectImageMediaType`
 * 是**纯函数**：不碰 React、不碰宿主 API（唯一的异步 I/O 是读传入 File 自身的前
 * 16 字节，属参数而非环境）——AC10 要求「粘贴取文件逻辑抽成独立纯函数，供桌面复用」，
 * 桌面（Electron 渲染同一套 React 应用）与本文件共用同一实现，桌面特有的
 * 「非图片文件 → `@path` 引用」分流属 MM-05，不在本模块。
 *
 * 本模块**唯一**的宿主 API 例外是模块级上限状态：`loadImageLimits()` 经 `./api`
 * 用 fetch 拉取服务端下发的上限并缓存（M-08 起），只读服务端配置、无其他副作用。
 *
 * ## 来源与判定（AGENTS.md §6.1）
 *
 * - **ADAPT** DeepSeek Harness（MIT，commit `5badb150`）
 *   `packages/client/ui-conversation/src/client/skeleton/InputBar.tsx:207-247`（intake 预检：
 *   只对图片子集判限、**整批拒绝**、立即告知、被拒文件永不进入 rail；宿主在提交时重复强制）
 *   与 `packages/client/ui-conversation/src/client/input/editor/keymap.ts:161-187`（粘贴：
 *   `clipboardData.items` 取 `kind === 'file'`，**无文件时不吞掉文本粘贴**）。
 *   本仓差异：DSH 用一次性 toast，本仓按 AC3 要求「就地显式原因」——错误串由本模块**返回**，
 *   由调用方常驻渲染（不自动消失），文案改为中文并带上文件名与具体上限。
 * - 第二独立来源 **LibreChat**（MIT，commit `e1dfc10449ff713faffacd60273fddcfe2c0a698`）
 *   `client/src/hooks/Files/useFileHandling.ts:175-204`（预检把「超限的文件名」逐个列出并说明
 *   是哪条上限，而不是只说一句"上传失败"）。两源各自解过同一问题，本模块只对译其语义。
 *
 * ## 上限常数的口径（PRD D11；#937 / M-08 起权威在服务端）
 *
 * 上限的**权威在服务端**（`src/agent_harness/config.py:162-170` 的 `attachment_max_*`，
 * 默认 20 MiB / 20 张 / 200 MiB / 四种 media type），经 `GET /api/attachments/limits`
 * 下发（后端 `app.py::get_attachment_limits`，camelCase）。前端在应用启动时由
 * `loadImageLimits()` 拉取并缓存进模块状态（`getImageLimits()` 取当前值；
 * App 层在 token 变更后补拉）。下面的 `IMAGE_LIMITS` 只是**离线 fallback**：
 * 拉取失败 / 端点缺席（旧服务端）时用，值必须与服务端默认值一致——
 * 跨端闸门 `tests/web/test_attachment_limits_contract.py` 逐字钉住这份对账。
 * 漂移的后果从「预检放行、服务端 413/422」降级为「fallback 与服务端不一致**时**
 * 才出现」：正常路径下预检用的就是服务端下发的值。
 */

import { getAttachmentLimits } from './api';
import { formatBytes } from './format';

/** 离线 fallback（服务端不可达 / 旧服务端时用；值 = `config.py:162-170` 的默认镜像，
 *  跨端闸门 `tests/web/test_attachment_limits_contract.py` 钉住；见模块头「上限常数的口径」）。 */
export const IMAGE_LIMITS = {
  maxImageBytes: 20 * 1024 * 1024,
  maxImagesPerMessage: 20,
  maxMessageImageBytes: 200 * 1024 * 1024,
  mediaTypes: ['image/png', 'image/jpeg', 'image/webp', 'image/gif'] as const,
} as const;

export interface ImageIntakeLimits {
  /** 单张字节上限。 */
  maxImageBytes: number;
  /** 单条消息图片数量上限。 */
  maxImagesPerMessage: number;
  /** 单条消息图片合计字节上限。 */
  maxMessageImageBytes: number;
  /** 允许的 media types（服务端按**字节**判定，这里用浏览器声明的 type 做预检）。 */
  mediaTypes: readonly string[];
}

// ── #937 / M-08：上限的模块级当前值（服务端下发，`IMAGE_LIMITS` 为 fallback）──
// 纯逻辑模块不引 React；状态就是这一个模块级变量。默认参数 `= getImageLimits()`
// 是**调用时求值**——刷新后的值对既有调用方（不传 limits 的那些）立刻生效，
// 不需要改任何调用点签名。
let currentLimits: ImageIntakeLimits = IMAGE_LIMITS;

/** 当前生效的附图上限（服务端下发成功后的值；拉取前 / 失败时 = `IMAGE_LIMITS`）。 */
export function getImageLimits(): ImageIntakeLimits {
  return currentLimits;
}

/** 直接设置当前上限（测试用；生产路径走 `loadImageLimits`）。 */
export function setImageLimits(limits: ImageIntakeLimits): void {
  currentLimits = limits;
}

let limitsInflight: Promise<ImageIntakeLimits> | null = null;

/**
 * 从服务端拉取附图上限并缓存进模块状态（应用启动 / token 变更后调用）。
 *
 * 成功：`currentLimits` = 服务端值并返回。失败（网络错 / 非 ok / 形状非法）：
 * **返回 `IMAGE_LIMITS` 且不抛**——「拿不到就用旧值」在这里落定，调用方
 * （Composer / App）可以 fire-and-forget；`currentLimits` 保持原样（已拉到的
 * 服务端值不因一次瞬时失败被冲掉）。并发调用共用同一个 in-flight promise
 * （App 补拉与 Composer 挂载同时发生时只打一次 GET）；promise 落定后清空，
 * 下一次调用是真重拉（token 变更后的补拉因此有效）。
 */
export function loadImageLimits(): Promise<ImageIntakeLimits> {
  if (limitsInflight) return limitsInflight;
  limitsInflight = getAttachmentLimits()
    .then((limits) => {
      currentLimits = limits;
      return limits;
    })
    .catch(() => IMAGE_LIMITS)
    .finally(() => {
      limitsInflight = null;
    });
  return limitsInflight;
}

/** 预检要看到的「已附图」最小形状：只用到字节数（张数由数组长度给）。 */
export interface IntakeExistingItem {
  bytes: number;
}

/** 一次 intake 的结局：`accepted` 进入上传，`error` 非空时**一个字都没进去**。 */
export interface IntakeOutcome {
  accepted: File[];
  error: string | null;
}

/**
 * 从粘贴事件取文件（AC10 的独立纯函数）。
 *
 * 判据与 DSH `keymap.ts:161-173` 一致：只看 `items` 里 `kind === 'file'` 的项，
 * 目录项也返回（`File` 形状下目录与空文件不可区分，交给上层决定丢弃）。
 * **无文件时返回空数组**——调用方据此让文本粘贴走原生路径，不得 `preventDefault`。
 */
export function filesFromClipboard(data: DataTransfer | null): File[] {
  if (data === null) return [];
  const files: File[] = [];
  for (const item of data.items) {
    if (item.kind !== 'file') continue;
    const file = item.getAsFile();
    if (file === null) continue;
    files.push(file);
  }
  return files;
}

/** 字节求和（#937 / M-23 收口：此前 234/235/253 行三处手写 reduce，逐字重复）。 */
export function sumBytes<T>(items: readonly T[], pick: (item: T) => number): number {
  return items.reduce((sum, item) => sum + pick(item), 0);
}

/**
 * 字节探测：读文件头魔数判定真实 media type（#937 / M-20）。
 *
 * 四种签名对齐后端 `src/agent_harness/attachments/probe.py::_detect`（判据又对译自
 * Pi `packages/coding-agent/src/utils/mime.ts`，MIT）：PNG `89 50 4E 47 0D 0A 1A 0A`、
 * GIF `GIF87a`/`GIF89a`、JPEG `FF D8 FF`、WebP `RIFF`+`WEBP`。前端只做**头签名**判定
 * （尺寸/像素上限、APNG 排除等深解析是服务端 `detect_image` 的职责），口径与后端一致。
 *
 * 为什么客户端声明不是权威：文件名与 `file.type` 由浏览器按扩展名/系统猜测、调用方可
 * 任意伪造（`.txt` 改名 `.png` 并把 type 填成 `image/png` 就能骗过），而「这段字节是
 * 不是图片、是哪种图片」只能由字节自身回答。返回 null = 字节不是受支持的图片（或读
 * 不到字节）——调用方按不支持处理（**fail-closed**）。
 */
export async function detectImageMediaType(file: File): Promise<string | null> {
  let head: Uint8Array;
  try {
    head = new Uint8Array(await file.slice(0, 16).arrayBuffer());
  } catch {
    return null;
  }
  // 字节不够任何一种签名的最短判据（JPEG 3 / GIF 6 / PNG 8 / WebP 12）→ 不是。
  const at = (index: number): number => head[index] ?? -1;
  const ascii = (from: number, to: number): string => String.fromCharCode(...head.subarray(from, to));
  if (
    at(0) === 0x89 && at(1) === 0x50 && at(2) === 0x4e && at(3) === 0x47 &&
    at(4) === 0x0d && at(5) === 0x0a && at(6) === 0x1a && at(7) === 0x0a
  ) {
    return 'image/png';
  }
  if (ascii(0, 6) === 'GIF87a' || ascii(0, 6) === 'GIF89a') return 'image/gif';
  if (at(0) === 0xff && at(1) === 0xd8 && at(2) === 0xff) return 'image/jpeg';
  if (ascii(0, 4) === 'RIFF' && ascii(8, 12) === 'WEBP') return 'image/webp';
  return null;
}

/**
 * 整批预检（AC2/AC3）：返回**要么整批进入上传、要么整批被拒并给出原因**。
 *
 * 语义（限值判定移植 DSH `InputBar.tsx:208-243`；第 1 条是本仓特有的范围决定）：
 * 1. **本仓特有（MM-05 前不支持非图片）**：非图片（type 不在允许集）→ 报「不支持的图片
 *    格式」并列出文件名。「是不是图片」的判定：有 `probedTypes` 条目时**字节说了算**
 *    （#937 / M-20 的魔数探测，`null` = 字节证伪 → 不支持）；没有条目时回退浏览器声明
 *    的 `file.type`（兼容不探针的调用方）。上游只对**图片子集**判限，非图片照常进它的
 *    `addFiles`（`@path` 文件引用通道）；本票没有那条通道，所以拒收，而非照搬上游；
 * 2. `已附图张数 + 本批张数 > maxImagesPerMessage` → 报数量上限（含当前张数）；
 * 3. 任一张 `size > maxImageBytes` → 报单张上限并列出**是哪几个文件**；
 * 4. 合计 `已附图字节 + 本批字节 > maxMessageImageBytes` → 报单条总上限。
 *
 * 只有全部通过才 `accepted = files`。不做「部分接受」——半进半退会让用户看不清到底附上了
 * 哪几张，而失败的几张还得自己找回来（DSH 同判据）。所有上限服务端仍会权威强制，
 * 客户端预检只负责「不等发送失败就告知」。
 */
export function partitionIntake(
  files: readonly File[],
  existing: readonly IntakeExistingItem[] = [],
  limits: ImageIntakeLimits = getImageLimits(),
  probedTypes?: ReadonlyMap<File, string | null>,
): IntakeOutcome {
  if (files.length === 0) return { accepted: [], error: null };

  // 有探测条目 → 字节说了算（null = 字节证伪，判成空串必不在允许集）；没有 → 回退声明。
  const effectiveType = (file: File): string =>
    (probedTypes?.has(file) ? probedTypes.get(file) : file.type) ?? '';
  const unsupported = files.filter((file) => !limits.mediaTypes.includes(effectiveType(file)));
  if (unsupported.length > 0) {
    const names = unsupported.map((file) => file.name || '（未命名）').join('、');
    return {
      accepted: [],
      error: `不支持的图片格式：${names}（只支持 PNG / JPEG / WebP / GIF）`,
    };
  }

  const total = existing.length + files.length;
  if (total > limits.maxImagesPerMessage) {
    return {
      accepted: [],
      error:
        `最多 ${limits.maxImagesPerMessage} 张/条，已附 ${existing.length} 张 + 本次 ${files.length} 张` +
        `，请先删掉多余的再试`,
    };
  }

  const oversized = files.filter((file) => file.size > limits.maxImageBytes);
  if (oversized.length > 0) {
    const names = oversized.map((file) => `${file.name || '（未命名）'}（${formatBytes(file.size)}）`).join('、');
    return {
      accepted: [],
      error: `超过单张上限 ${formatBytes(limits.maxImageBytes)}：${names}`,
    };
  }

  const existingBytes = sumBytes(existing, (item) => item.bytes);
  const batchBytes = sumBytes(files, (file) => file.size);
  if (existingBytes + batchBytes > limits.maxMessageImageBytes) {
    return {
      accepted: [],
      error:
        `超过单条总上限 ${formatBytes(limits.maxMessageImageBytes)}：` +
        `已附 ${formatBytes(existingBytes)} + 本次 ${formatBytes(batchBytes)}`,
    };
  }

  return { accepted: [...files], error: null };
}

/** 剩余预算文案（AC2）：张数与字节两条都要看得见。 */
export function budgetText(
  existing: readonly IntakeExistingItem[],
  limits: ImageIntakeLimits = getImageLimits(),
): string {
  const usedBytes = sumBytes(existing, (item) => item.bytes);
  const leftImages = Math.max(0, limits.maxImagesPerMessage - existing.length);
  const leftBytes = Math.max(0, limits.maxMessageImageBytes - usedBytes);
  return `已附 ${existing.length}/${limits.maxImagesPerMessage} 张 · 剩余 ${leftImages} 张 / ${formatBytes(leftBytes)}`;
}

/**
 * 非视觉模型下 `user/message` 附件被投影成占位符的**文案**（AC7 的「图已被省略」标注）。
 *
 * 逐字对齐后端 `attachments/projection.py::IMAGE_OMITTED_PLACEHOLDER`（该文案又逐字移植
 * 自 Pi）——前端不另写一份中文改写：这条串是后端真的会放进模型上下文的那一句，
 * 界面标注与它同源，用户才知道"模型看到的就是这句"。
 */
export const IMAGE_OMITTED_PLACEHOLDER = '(image omitted: model does not support images)';
