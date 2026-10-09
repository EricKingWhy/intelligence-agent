# Third-party notices — `web/`

Parts of this package are adapted from MIT-licensed open-source projects. The
adapted files carry a header naming the upstream file, the exact commit, and the
source line range. This file holds the license texts and the provenance ledger.

## DeepSeek Harness (`deepseek-ai/deepseek-harness`)

- **Commit read for this adaptation:** `5badb15009ae1756c3afe0ae0cef1faafc290ccc`
- **License:** MIT
- **Upstream license text** (`LICENSE` at that commit):

```
MIT License

Copyright (c) 2026 DeepSeek

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### Adapted files — #825 (MM-04) image attachments

| Local file | Upstream file @ commit | Source lines | Nature |
| --- | --- | --- | --- |
| `src/lib/dropEvents.ts` | `packages/client/ui-attachment/src/client/drop-events.ts` @ `5badb15` | 1–87 | COPY (DSH type imports inlined; no other change) |
| `src/components/DropOverlay.tsx` | `packages/client/ui-attachment/src/DropOverlay.tsx` @ `5badb15` | 1–77 | COPY (CSS module → global classes; clipPath id de-prefixed). Both SVG illustrations (`UploadIllustration` / `UploadDisabledIllustration`) are upstream assets, reproduced verbatim as part of this copy |
| `src/components/ComposerAttachments.tsx` | `packages/client/ui-attachment/src/AttachmentRail.tsx` + `FileCard.tsx` + `client/ComposerAttachments.tsx` @ `5badb15` | rail/file-card/composer sections | ADAPT (DSH private icon + lightbox primitives → `lucide-react` + this repo's `ImageLightbox`; hidden-scroll + paging arrows → wrapping grid; no non-image file card — `@path` file refs are MM-05) |
| `src/components/MessageImages.tsx` | `packages/client/ui-attachment/src/MessageImage.tsx` + `client/MessageImages.tsx` @ `5badb15` | skeleton + gallery sections | ADAPT (private primitives → `lucide-react` + `ImageLightbox`; global class names) |
| `src/components/ImageLightbox.tsx` | `packages/client/ui-attachment/src/MessageImage.tsx` @ `5badb15` (usage of DSH's `ImageLightbox` primitive) | lightbox usage section | ADAPT (**usage pattern only**: thumbnail → modal viewer, Esc/backdrop close, body is the full-size image; primitive → `@radix-ui/react-dialog`). **Copy / download are this repo's own additions** (AC6) — upstream's `ImageLightbox` primitive has just `<img>` + a close button; the copy action re-encodes to PNG via `src/lib/clipboardImage.ts` |
| `src/lib/attachments.ts` | `packages/client/ui-conversation/src/client/skeleton/InputBar.tsx` @ `5badb15` | 207–247 | ADAPT (limit-check semantics on the image subset, whole-batch rejection, rejected files never reach the rail, host re-enforces at submit). Rejecting **non-image** files is this repo's own decision (no `@path` file-reference channel before MM-05) — upstream only limits the image subset and passes non-images to its `addFiles` |
| `src/lib/attachments.ts` | `packages/client/ui-conversation/src/client/input/editor/keymap.ts` @ `5badb15` | 161–187 | ADAPT (paste intake: `clipboardData.items` where `kind === 'file'`; text-paste fallthrough — no files ⇒ the event is never taken over; files + `text/plain` ⇒ text is inserted at the caret). Directory-entry detection (`webkitGetAsEntry`) is **not** ported — `filesFromClipboard` returns every `kind === 'file'` item and the drop path (`dropEvents.ts`) handles directories |
| `src/hooks/useDraftAttachments.ts` | `packages/client/ui-conversation/src/client/service.ts` @ `5badb15` | 329–400 | PORT DESIGN (upload state machine: per-item status/progress/retry; DSH's Cordis `ctx.fileUpload` worker pool is not reproduced — one XHR per image, browser-scheduled) |

### Adapted files — #826 (MM-05) desktop host-path intake

| Local file | Upstream file @ commit | Source lines | Nature |
| --- | --- | --- | --- |
| `src/lib/hostFiles.ts` | `packages/client/ui-conversation/src/client/apply.ts` (`HostPathBridge` + `hostPathBridge()` + the `addFiles` split) and `packages/context/file-reference/src/grammar.ts` (`formatFileMention`) @ `5badb15` | apply.ts 99–109 / 460–497; grammar.ts 45–59 | ADAPT (the split rule `path === '' \|\| isImage` → upload, else a reference, is kept verbatim; so are the quoted `@"path"` form and the give-up rule for control characters / `"`). **Not** ported: directory references (out of scope for #826 — directories are dropped as before), the rail chip (this repo has no file-reference channel in the backend yet, so the reference is inserted as message text), and `relativizeToCwd` (no session cwd at the Composer seam) |

`src/lib/attachmentThumbnail.ts`, `src/lib/attachmentRefs.ts`,
`src/lib/clipboardImage.ts`, `src/hooks/useAttachmentImage.ts` contain no
upstream code (written against this repo's CSP and controlled-endpoint contract).

> The upstream license text above is quoted verbatim from `LICENSE` at
> `5badb15009ae1756c3afe0ae0cef1faafc290ccc`; the copyright year was corrected
> from `2024` to `2026` in #826 after re-reading that file at the pinned commit.

## VS Code (`microsoft/vscode`) — reference only, no code copied

- **Commit read:** `a64c64ab9ce9136625cf080db3cd091263d90e0c` (`main`)
- **License:** MIT
- **Use:** second independent source for the **consumer side** of `src/lib/hostFiles.ts`
  (#826 / MM-05) — `src/vs/platform/dnd/browser/dnd.ts:566–585` reads its
  Electron-host bridge defensively (`typeof … === 'function'` on each level, and
  a missing bridge degrades to `undefined` instead of throwing), and
  `src/vs/base/parts/sandbox/electron-browser/preload.ts:184–190` exposes only a
  minimal `webUtils.getPathForFile` wrapper rather than Electron's `webUtils`
  object. No VS Code source is copied: this repo's bridge shape and its
  degrade-to-`''` rule are its own, written against `HOST_PATHS_GLOBAL`.

## LibreChat (`danny-avila/LibreChat`)

- **Commit read for this reference:** `e1dfc10449ff713faffacd60273fddcfe2c0a698`
- **License:** MIT
- **Use:** second independent source for the intake error-reporting semantics in
  `src/lib/attachments.ts` — `client/src/hooks/Files/useFileHandling.ts:175–204`
  (per-file oversize reporting that names *which* limit was exceeded, and the
  progress/recovery pass at :265–300) plus `client/src/components/Chat/Input/Files/FilePreview.tsx`
  (per-tile spinner while `progress < 1`). **No LibreChat code is copied**: only
  the behaviour (name the file and the breached limit instead of a generic
  "upload failed"; per-item retry rather than batch failure) is translated into
  this repo's own implementation.
