/**
 * 文档级拖拽监听（一个挂载中的附图视图一份）。
 *
 * 来源：DeepSeek Harness `packages/client/ui-attachment/src/client/drop-events.ts:1-87`
 * @ commit `5badb15009ae1756c3afe0ae0cef1faafc290ccc`（MIT，许可全文见
 * `web/THIRD_PARTY_NOTICES.md`）。判定 **COPY**（整文件复制，行为逐行保留；
 * 仅把上游对 `@deepseek-ai/dsh-client-ui-conversation/client` 的类型 import 换成本文件的
 * 内联函数类型，剥掉 DSH 包依赖——原文件没有其它 import）。
 *
 * 语义要点（审查时不要"简化"掉的那几条）：
 * - 用 `dragDepth` 计数而不是布尔：子元素间的 enter/leave 成对到达，布尔会在第一个
 *   leave 上闪掉遮罩；
 * - `dropEffect` 随 `canAcceptDrop`：不可接受时给 `none`，让浏览器自己显示禁止光标；
 * - 目录项只能用 `webkitGetAsEntry()` 识别（目录拖拽产出的 `File` 与空文件不可区分）；
 * - `window` 上的 `dragend` 复位兜住"拖出窗口后没收到 drop/leave"的悬挂态。
 */

/** 是否接受本次拖放（不可接受时 drop 不投递文件，且光标显示禁止）。 */
type CanAcceptDrop = boolean;

/** 一次拖放投递的入口：文件数组 + 其中被识别为目录的项。 */
type AddFiles = (files: readonly File[], directories: ReadonlySet<File>) => void;

/**
 * 本次拖放里属于目录的成员。目录拖拽产出的 `File` 与空文件不可区分，entry API 是
 * 唯一的事实来源；不支持该 API 的浏览器一律报告"没有目录"。
 */
function droppedDirectories(dataTransfer: DataTransfer, files: readonly File[]): ReadonlySet<File> {
  const directories = new Set<File>();
  let fileIndex = 0;
  for (const item of dataTransfer.items) {
    if (item.kind !== 'file') continue;
    const file = files[fileIndex++];
    if (typeof item.webkitGetAsEntry !== 'function') continue;
    if (item.webkitGetAsEntry()?.isDirectory !== true) continue;
    if (file !== undefined) directories.add(file);
  }
  return directories;
}

/**
 * 安装一个附图视图的文件拖放监听。
 * @param canAcceptDrop - 该视图是否接受被拖入的文件。
 * @param onAddFiles - 附图 intake 回调。
 * @param dragDepth - 该视图保留的嵌套拖拽计数。
 * @param setDragActive - 发布"当前有文件拖拽"。
 * @returns 只清理这些监听的清理函数。
 */
export function installDocumentDropEvents(
  canAcceptDrop: CanAcceptDrop,
  onAddFiles: AddFiles,
  dragDepth: { current: number },
  setDragActive: (active: boolean) => void,
): () => void {
  const fileTransfer = (event: globalThis.DragEvent): DataTransfer | null => {
    const dataTransfer = event.dataTransfer;
    if (dataTransfer === null || !dataTransfer.types.includes('Files')) return null;
    return dataTransfer;
  };
  const reset = (): void => {
    dragDepth.current = 0;
    setDragActive(false);
  };
  const onDragEnter = (event: globalThis.DragEvent): void => {
    if (fileTransfer(event) === null) return;
    event.preventDefault();
    dragDepth.current += 1;
    setDragActive(true);
  };
  const onDragOver = (event: globalThis.DragEvent): void => {
    const dataTransfer = fileTransfer(event);
    if (dataTransfer === null) return;
    event.preventDefault();
    dataTransfer.dropEffect = canAcceptDrop ? 'copy' : 'none';
  };
  const onDragLeave = (event: globalThis.DragEvent): void => {
    if (fileTransfer(event) === null) return;
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (dragDepth.current === 0) setDragActive(false);
    const leftViewport =
      event.clientX <= 0 ||
      event.clientY <= 0 ||
      event.clientX >= window.innerWidth ||
      event.clientY >= window.innerHeight;
    if (
      (event.target === document.documentElement || event.target === document.body) &&
      leftViewport
    ) {
      reset();
    }
  };
  const onDrop = (event: globalThis.DragEvent): void => {
    const dataTransfer = fileTransfer(event);
    if (dataTransfer === null) return;
    event.preventDefault();
    reset();
    if (canAcceptDrop) {
      const files = [...dataTransfer.files];
      onAddFiles(files, droppedDirectories(dataTransfer, files));
    }
  };
  document.addEventListener('dragenter', onDragEnter);
  document.addEventListener('dragover', onDragOver);
  document.addEventListener('dragleave', onDragLeave);
  document.addEventListener('drop', onDrop);
  window.addEventListener('dragend', reset);
  return () => {
    document.removeEventListener('dragenter', onDragEnter);
    document.removeEventListener('dragover', onDragOver);
    document.removeEventListener('dragleave', onDragLeave);
    document.removeEventListener('drop', onDrop);
    window.removeEventListener('dragend', reset);
  };
}
