/**
 * magic-bytes MIME 探测（零依赖纯函数）。
 *
 * 来源：Pi `packages/coding-agent/src/utils/mime.ts:1-116` @ commit
 * `28dcce2ba45ce4a9efeb0f5b686f0be830fd89b9`（MIT，许可全文见
 * `tui/THIRD_PARTY_NOTICES.md`）。判定 **COPY**：签名表与判定顺序逐条保留，
 * 仅把上游的 tab 缩进改成本包的两空格并把类型放宽到 `Uint8Array`（上游已是
 * `Uint8Array`，`Buffer` 是其子类，故调用面不变）。
 *
 * 为什么必须按字节判而不是看扩展名：服务端 `POST /api/sessions/{id}/attachments`
 * 同样按 magic bytes 判型，声明与字节不符直接拒（`IMAGE_TYPE_MISMATCH`）。客户端
 * 先按字节判，是为了把「发出去才发现被拒」提前成「本地明确错误」（PRD 用户故事 24）。
 */
const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];

export function detectSupportedImageMimeType(buffer: Uint8Array): string | null {
  if (startsWith(buffer, [0xff, 0xd8, 0xff])) {
    // 0xF7 是 jpeg-lossless；本仓服务端与 pi-tui 都不认它。
    return buffer[3] === 0xf7 ? null : "image/jpeg";
  }
  if (startsWith(buffer, PNG_SIGNATURE)) {
    return isPng(buffer) && !isAnimatedPng(buffer) ? "image/png" : null;
  }
  if (startsWithAscii(buffer, 0, "GIF87a") || startsWithAscii(buffer, 0, "GIF89a")) {
    return "image/gif";
  }
  if (startsWithAscii(buffer, 0, "RIFF") && startsWithAscii(buffer, 8, "WEBP")) {
    return "image/webp";
  }
  if (startsWithAscii(buffer, 0, "BM") && isBmp(buffer)) {
    return "image/bmp";
  }
  return null;
}

function isPng(buffer: Uint8Array): boolean {
  return (
    buffer.length >= 16 &&
    readUint32BE(buffer, PNG_SIGNATURE.length) === 13 &&
    startsWithAscii(buffer, 12, "IHDR")
  );
}

function isAnimatedPng(buffer: Uint8Array): boolean {
  let offset = PNG_SIGNATURE.length;
  while (offset + 8 <= buffer.length) {
    const chunkLength = readUint32BE(buffer, offset);
    const chunkTypeOffset = offset + 4;
    if (startsWithAscii(buffer, chunkTypeOffset, "acTL")) return true;
    if (startsWithAscii(buffer, chunkTypeOffset, "IDAT")) return false;

    const nextOffset = offset + 8 + chunkLength + 4;
    if (nextOffset <= offset || nextOffset > buffer.length) return false;
    offset = nextOffset;
  }
  return false;
}

function isBmp(buffer: Uint8Array): boolean {
  if (buffer.length < 26) return false;

  const declaredFileSize = readUint32LE(buffer, 2);
  const pixelDataOffset = readUint32LE(buffer, 10);
  const dibHeaderSize = readUint32LE(buffer, 14);
  if (declaredFileSize !== 0 && declaredFileSize < 26) return false;
  if (pixelDataOffset < 14 + dibHeaderSize) return false;
  if (declaredFileSize !== 0 && pixelDataOffset >= declaredFileSize) return false;

  let colorPlanes: number;
  let bitsPerPixel: number;
  if (dibHeaderSize === 12) {
    colorPlanes = readUint16LE(buffer, 22);
    bitsPerPixel = readUint16LE(buffer, 24);
  } else if (dibHeaderSize >= 40 && dibHeaderSize <= 124) {
    if (buffer.length < 30) return false;
    colorPlanes = readUint16LE(buffer, 26);
    bitsPerPixel = readUint16LE(buffer, 28);
  } else {
    return false;
  }

  return colorPlanes === 1 && [1, 4, 8, 16, 24, 32].includes(bitsPerPixel);
}

function readUint16LE(buffer: Uint8Array, offset: number): number {
  return (buffer[offset] ?? 0) + ((buffer[offset + 1] ?? 0) << 8);
}

function readUint32BE(buffer: Uint8Array, offset: number): number {
  return (
    (buffer[offset] ?? 0) * 0x1000000 +
    ((buffer[offset + 1] ?? 0) << 16) +
    ((buffer[offset + 2] ?? 0) << 8) +
    (buffer[offset + 3] ?? 0)
  );
}

function readUint32LE(buffer: Uint8Array, offset: number): number {
  return (
    (buffer[offset] ?? 0) +
    ((buffer[offset + 1] ?? 0) << 8) +
    ((buffer[offset + 2] ?? 0) << 16) +
    (buffer[offset + 3] ?? 0) * 0x1000000
  );
}

function startsWith(buffer: Uint8Array, bytes: number[]): boolean {
  if (buffer.length < bytes.length) return false;
  return bytes.every((byte, index) => buffer[index] === byte);
}

function startsWithAscii(buffer: Uint8Array, offset: number, text: string): boolean {
  if (buffer.length < offset + text.length) return false;
  for (let index = 0; index < text.length; index++) {
    if (buffer[offset + index] !== text.charCodeAt(index)) return false;
  }
  return true;
}
