/**
 * 附图入口的**纯逻辑**（#825 / MM-04）：剪贴板取文件、整批预检、预算文案。
 *
 * 这里刻意不碰 React、不碰 DOM 事件对象之外的宿主 API——AC10 要求「粘贴取文件逻辑抽成
 * 独立纯函数，供桌面复用」，桌面（Electron 渲染同一套 React 应用）与本文件共用同一实现，
 * 桌面特有的「非图片文件 → `@path` 引用」分流属 MM-05，不在本模块。
 *
 * ## 来源与判定（AGENTS.md §6.1）
 *
 * - **ADAPT** DeepSeek Harness（MIT，commit `5badb150`）
 *   `packages/client/ui-conversation/src/client/skeleton/InputBar.tsx:207-247`（intake 预检：
 *   只对图片子集判限、**整批拒绝**、立即告知、被拒文件永不进入 rail；宿主在提交时重复强制）
 *   与 `packages/client/ui-conversation/src/client/input/editor/keymap.ts:161-187`（粘贴：
 *   `clipboardData.items` 取 `kind === 'file'` + 目录项识别；无文件时**不**吞掉文本粘贴）。
 *   本仓差异：DSH 用一次性 toast，本仓按 AC3 要求「就地显式原因」——错误串由本模块**返回**，
 *   由调用方常驻渲染（不自动消失），文案改为中文并带上文件名与具体上限。
 * - 第二独立来源 **LibreChat**（MIT，commit `e1dfc10449ff713faffacd60273fddcfe2c0a698`）
 *   `client/src/hooks/Files/useFileHandling.ts:175-204`（预检把「超限的文件名」逐个列出并说明
 *   是哪条上限，而不是只说一句"上传失败"）。两源各自解过同一问题，本模块只对译其语义。
 *
 * ## 上限常数的口径（PRD D11）
 *
 * 上限的**权威在服务端**（`src/agent_harness/config.py:162-170` 的 `attachment_max_*`，
 * 默认 20 MiB / 20 张 / 200 MiB / 四种 media type）；前端预检只是**同一份值的镜像**，
 * 沿用 PRD D11 指定的「后端为权威、前端镜像」模式。当前后端没有下发这些值的端点
 * （#824 / MM-03 只加服务端强制，不加 GET），所以镜像写在下面这一处：
 * 服务端改默认值时这里必须同步（漂移的后果是「预检放行、服务端 413/422」——
 * 消息仍会被权威拒绝，不会静默出错）。
 */

/** 与 `config.py:162-170` 同值的镜像（唯一的客户端副本；见模块头「上限常数的口径」）。 */
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

/** 人类可读字节数（预算文案用；1024 进制，与后端 `MiB` 口径一致）。 */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const kib = bytes / 1024;
  if (kib < 1024) return `${Math.round(kib)} KiB`;
  const mib = kib / 1024;
  if (mib < 1024) return `${mib >= 10 ? Math.round(mib) : Math.round(mib * 10) / 10} MiB`;
  return `${Math.round((mib / 1024) * 10) / 10} GiB`;
}

/**
 * 整批预检（AC2/AC3）：返回**要么整批进入上传、要么整批被拒并给出原因**。
 *
 * 语义（逐条移植 DSH `InputBar.tsx:208-243`）：
 * 1. 非图片（type 不在允许集）→ 报「不支持的图片格式」并列出文件名；
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
  limits: ImageIntakeLimits = IMAGE_LIMITS,
): IntakeOutcome {
  if (files.length === 0) return { accepted: [], error: null };

  const unsupported = files.filter((file) => !limits.mediaTypes.includes(file.type));
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

  const existingBytes = existing.reduce((sum, item) => sum + item.bytes, 0);
  const batchBytes = files.reduce((sum, file) => sum + file.size, 0);
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
  limits: ImageIntakeLimits = IMAGE_LIMITS,
): string {
  const usedBytes = existing.reduce((sum, item) => sum + item.bytes, 0);
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
