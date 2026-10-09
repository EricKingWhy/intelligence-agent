# Third-party notices — `tui/`

Parts of this package are adapted from MIT- / Apache-2.0-licensed open-source
projects. Every adapted file carries a header naming the upstream file, the exact
commit, and the source line range. This file holds the license texts and the
provenance ledger.

## Pi (`badloop/pi`)

- **Commit read for this adaptation:** `28dcce2ba45ce4a9efeb0f5b686f0be830fd89b9`
- **License:** MIT
- **Upstream license text** (`LICENSE` at that commit):

```
MIT License

Copyright (c) 2025 Mario Zechner

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

### Adapted files — #827 (MM-06) TUI image input

| Local file | Upstream file @ commit | Source lines | Nature |
| --- | --- | --- | --- |
| `src/lib/mime.ts` | `packages/coding-agent/src/utils/mime.ts` @ `28dcce2b` | 1–116 | COPY (signature table and detection order kept verbatim; tab indentation → 2 spaces; the `IMAGE_TYPE_SNIFF_BYTES` read path is unchanged) |
| `src/lib/wsl.ts` | `packages/coding-agent/src/utils/wsl.ts` @ `28dcce2b` | 1–15 | COPY (`WSL_DISTRO_NAME`/`WSLENV`, then `/proc/version` matching `microsoft\|wsl`; the `env` default is made explicit for injection) |
| `src/lib/clipboard-command.ts` | `packages/coding-agent/src/utils/clipboard-command.ts` @ `28dcce2b` | 1–44 | ADAPT (**observable behaviour identical**: failure → `undefined`, empty output → empty `Buffer`, over-limit/timeout → `undefined`). Two changes: the timeout is enforced by `execFile({timeout, maxBuffer})` instead of a hand-rolled `spawn` + timer + kill — this repo's `tui/test/host.test.ts` single-writer guard forbids any termination-signal shape under `src/` — and upstream's unused `input` option is dropped (no clipboard hop writes to the child's stdin) |
| `src/lib/clipboard-image.ts` | `packages/coding-agent/src/utils/clipboard-image.ts` @ `28dcce2b` | 1–240 | COPY with three declared deviations: (1) **no Photon** — upstream converts unsupported clipboard payloads to PNG via the optional native `photon` dependency; #827 forbids new TUI dependencies, so a payload outside this repo's accepted media types returns `null` ("no usable image") instead of being transcoded; (2) the accepted media-type set is imported from `src/lib/image-paste.ts` so it cannot drift from the server contract (`attachments/types.py::SUPPORTED_IMAGE_MEDIA_TYPES`); (3) the child-process runner, the native clipboard reader and the temp dir are injectable — the Linux sandbox has no Windows clipboard, so AC1's PowerShell hop is covered by an injected fake `powershell.exe` (registered verification gap, see the dispatch checkpoint). The platform ladder (Termux → `wl-paste` → `xclip` → `powershell.exe` → native) and the `Wayland`/WSL branch conditions are kept verbatim |

`src/lib/pending-images.ts`, `src/lib/vision.ts`, `src/lib/image-view.ts`,
`src/lib/image-paste.ts`, `src/app.ts`, `src/api.ts`, `src/adapter.ts`,
`src/index.ts` contain no Pi code.

## Cline (`cline/cline`)

- **Commit read for this adaptation:** `cd80a20e96481f5f5d413789f6847accf846487b`
- **License:** Apache-2.0 (`apps/cli/package.json` `"license": "Apache-2.0"`)
- **Upstream license text** (`LICENSE` at that commit):

```
                                 Apache License
                           Version 2.0, January 2004
                        http://www.apache.org/licenses/

   TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION

   1. Definitions.

      "License" shall mean the terms and conditions for use, reproduction,
      and distribution as defined by Sections 1 through 9 of this document.

      "Licensor" shall mean the copyright owner or entity authorized by
      the copyright owner that is granting the License.

      "Legal Entity" shall mean the union of the acting entity and all
      other entities that control, are controlled by, or are under common
      control with that entity. For the purposes of this definition,
      "control" means (i) the power, direct or indirect, to cause the
      direction or management of such entity, whether by contract or
      otherwise, or (ii) ownership of fifty percent (50%) or more of the
      outstanding shares, or (iii) beneficial ownership of such entity.

      "You" (or "Your") shall mean an individual or Legal Entity
      exercising permissions granted by this License.

      "Source" form shall mean the preferred form for making modifications,
      including but not limited to software source code, documentation
      source, and configuration files.

      "Object" form shall mean any form resulting from mechanical
      transformation or translation of a Source form, including but
      not limited to compiled object code, generated documentation,
      and conversions to other media types.

      "Work" shall mean the work of authorship, whether in Source or
      Object form, made available under the License, as indicated by a
      copyright notice that is included in or attached to the work
      (an example is provided in the Appendix below).

      "Derivative Works" shall mean any work, whether in Source or Object
      form, that is based on (or derived from) the Work and for which the
      editorial revisions, annotations, elaborations, or other modifications
      represent, as a whole, an original work of authorship. For the purposes
      of this License, Derivative Works shall not include works that remain
      separable from, or merely link (or bind by name) to the interfaces of,
      the Work and Derivative Works thereof.

      "Contribution" shall mean any work of authorship, including
      the original version of the Work and any modifications or additions
      to that Work or Derivative Works thereof, that is intentionally
      submitted to Licensor for inclusion in the Work by the copyright owner
      or by an individual or Legal Entity authorized to submit on behalf of
      the copyright owner. For the purposes of this definition, "submitted"
      means any form of electronic, verbal, or written communication sent
      to the Licensor or its representatives, including but not limited to
      communication on electronic mailing lists, source code control systems,
      and issue tracking systems that are managed by, or on behalf of, the
      Licensor for the purpose of discussing and improving the Work, but
      excluding communication that is conspicuously marked or otherwise
      designated in writing by the copyright owner as "Not a Contribution."

      "Contributor" shall mean Licensor and any individual or Legal Entity
      on behalf of whom a Contribution has been received by Licensor and
      subsequently incorporated within the Work.

   2. Grant of Copyright License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      copyright license to reproduce, prepare Derivative Works of,
      publicly display, publicly perform, sublicense, and distribute the
      Work and such Derivative Works in Source or Object form.

   3. Grant of Patent License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      (except as stated in this section) patent license to make, have made,
      use, offer to sell, sell, import, and otherwise transfer the Work,
      where such license applies only to those patent claims licensable
      by such Contributor that are necessarily infringed by their
      Contribution(s) alone or by combination of their Contribution(s)
      with the Work to which such Contribution(s) was submitted. If You
      institute patent litigation against any entity (including a
      cross-claim or counterclaim in a lawsuit) alleging that the Work
      or a Contribution incorporated within the Work constitutes direct
      or contributory patent infringement, then any patent licenses
      granted to You under this License for that Work shall terminate
      as of the date such litigation is filed.

   4. Redistribution. You may reproduce and distribute copies of the
      Work or Derivative Works thereof in any medium, with or without
      modifications, and in Source or Object form, provided that You
      meet the following conditions:

      (a) You must give any other recipients of the Work or
          Derivative Works a copy of this License; and

      (b) You must cause any modified files to carry prominent notices
          stating that You changed the files; and

      (c) You must retain, in the Source form of any Derivative Works
          that You distribute, all copyright, patent, trademark, and
          attribution notices from the Source form of the Work,
          excluding those notices that do not pertain to any part of
          the Derivative Works; and

      (d) If the Work includes a "NOTICE" text file as part of its
          distribution, then any Derivative Works that You distribute must
          include a readable copy of the attribution notices contained
          within such NOTICE file, excluding those notices that do not
          pertain to any part of the Derivative Works, in at least one
          of the following places: within a NOTICE text file distributed
          as part of the Derivative Works; within the Source form or
          documentation, if provided along with the Derivative Works; or,
          within a display generated by the Derivative Works, if and
          wherever such third-party notices normally appear. The contents
          of the NOTICE file are for informational purposes only and
          do not modify the License. You may add Your own attribution
          notices within Derivative Works that You distribute, alongside
          or as an addendum to the NOTICE text from the Work, provided
          that such additional attribution notices cannot be construed
          as modifying the License.

      You may add Your own copyright statement to Your modifications and
      may provide additional or different license terms and conditions
      for use, reproduction, or distribution of Your modifications, or
      for any such Derivative Works as a whole, provided Your use,
      reproduction, and distribution of the Work otherwise complies with
      the conditions stated in this License.

   5. Submission of Contributions. Unless You explicitly state otherwise,
      any Contribution intentionally submitted for inclusion in the Work
      by You to the Licensor shall be under the terms and conditions of
      this License, without any additional terms or conditions.
      Notwithstanding the above, nothing herein shall supersede or modify
      the terms of any separate license agreement you may have executed
      with Licensor regarding such Contributions.

   6. Trademarks. This License does not grant permission to use the trade
      names, trademarks, service marks, or product names of the Licensor,
      except as required for reasonable and customary use in describing the
      origin of the Work and reproducing the content of the NOTICE file.

   7. Disclaimer of Warranty. Unless required by applicable law or
      agreed to in writing, Licensor provides the Work (and each
      Contributor provides its Contributions) on an "AS IS" BASIS,
      WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
      implied, including, without limitation, any warranties or conditions
      of TITLE, NON-INFRINGEMENT, MERCHANTABILITY, or FITNESS FOR A
      PARTICULAR PURPOSE. You are solely responsible for determining the
      appropriateness of using or redistributing the Work and assume any
      risks associated with Your exercise of permissions under this License.

   8. Limitation of Liability. In no event and under no legal theory,
      whether in tort (including negligence), contract, or otherwise,
      unless required by applicable law (such as deliberate and grossly
      negligent acts) or agreed to in writing, shall any Contributor be
      liable to You for damages, including any direct, indirect, special,
      incidental, or consequential damages of any character arising as a
      result of this License or out of the use or inability to use the
      Work (including but not limited to damages for loss of goodwill,
      work stoppage, computer failure or malfunction, or any and all
      other commercial damages or losses), even if such Contributor
      has been advised of the possibility of such damages.

   9. Accepting Warranty or Additional Liability. While redistributing
      the Work or Derivative Works thereof, You may choose to offer,
      and charge a fee for, acceptance of support, warranty, indemnity,
      or other liability obligations and/or rights consistent with this
      License. However, in accepting such obligations, You may act only
      on Your own behalf and on Your sole responsibility, not on behalf
      of any other Contributor, and only if You agree to indemnify,
      defend, and hold each Contributor harmless for any liability
      incurred by, or claims asserted against, such Contributor by reason
      of your accepting any such warranty or additional liability.

   END OF TERMS AND CONDITIONS

   APPENDIX: How to apply the Apache License to your work.

      To apply the Apache License to your work, attach the following
      boilerplate notice, with the fields enclosed by brackets "[]"
      replaced with your own identifying information. (Don't include
      the brackets!)  The text should be enclosed in the appropriate
      comment syntax for the file format. We also recommend that a
      file or class name and description of purpose be included on the
      same "printed page" as the copyright notice for easier
      identification within third-party archives.

   Copyright 2026 Cline Bot Inc.

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.
```

### Adapted files — #827 (MM-06) TUI image input

| Local file | Upstream file @ commit | Source lines | Nature |
| --- | --- | --- | --- |
| `src/lib/image-paste.ts` | `apps/cli/src/tui/utils/image-paste.ts` @ `cd80a20e` | 35–83 (`normalizePasteText` / `stripWrappingQuotes` / `unescapeTerminalPath` / `resolvePastedImagePath`) | ADAPT (lead-in rule kept: normalize `\r\n`/`\r`, trim, **exactly one** non-empty line, strip wrapping quotes, reject `^(https?):`, `file://` → `fileURLToPath`, non-Windows unescape `\\(.)`). Not ported: `stripAnsiSequences` + the `PasteLikeEvent{bytes,metadata}` intake (that type is OpenTUI's; this repo drives the same logic from pi-tui's bracketed-paste chunk in `src/app.ts`), and `readImageDataUrlFromPastedText` (upstream returns a data URL for OpenTUI; this repo's contract is a **byte-stream** upload, so the reader returns bytes; the reader also **stats first and accepts regular files only**, so a FIFO/device path cannot block the synchronous read and freeze the TUI loop) |
| `src/lib/image-paste.ts` | `apps/cli/src/utils/image-attachments.ts` @ `cd80a20e` | 4–29 (`IMAGE_EXTENSIONS` + `isImagePath`) | ADAPT (extension set aligned to this repo's server contract — png/jpg/jpeg/webp/gif only; upstream additionally accepts bmp/svg, which `POST /api/sessions/{id}/attachments` rejects). **Not** ported: `resolveExistingFilePath` (upstream's `@cline/shared/storage` module has no counterpart here; existence is decided by actually reading the file in `readImageFile`, whose `missing` / `unsupported` reasons become user-visible errors) |

## oh-my-pi (`can1357/oh-my-pi`)

- **Commit read for this design port:** `579da1d661c5cb8d43bc2ddd429ab72e67165ad8`
- **License:** MIT

```
MIT License

Copyright (c) 2025 Mario Zechner
Copyright (c) 2025-2026 Can Bölük
Copyright (c) 2026 Stencil Labs, Inc.

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

| Local file | Upstream file @ commit | Source lines | Nature |
| --- | --- | --- | --- |
| `src/lib/pending-images.ts` | `packages/tui/src/prompt/composer-attachments.ts` @ `579da1d6` | 267–306 (`compactImageMarkers`), 221–229 (`formatVisionMarker`) | PORT DESIGN (**no code copied**: same positional rule — `[Image #N]` indexes the attachment array, submission drops markers above the array length, retained markers are densely renumbered, `keep` holds 0-based original indices, `null` means "no compaction needed"). Deliberately dropped: video placeholders, the `, WxH` tail, `attachment://N` references, the `byAppearance` renumbering mode, and the shared `VISION_MARKER_REGEX` (this repo matches bare `[Image #N]` only — the request-body reference is the separate `attachments` field, not a text token) |

> Upstream `packages/coding-agent/src/modes/controllers/input-controller.ts`
> (`#insertPendingImage` / `#compactDraftImages`) was read as a second source for
> the same rule; no code from it is reproduced.

## Sized-down reference reading (no code copied)

- **pi-tui** (`@earendil-works/pi-tui` 1.0.4, MIT) — **REUSE only, no copy**:
  `getCapabilities()` / `Image` / `imageFallback()` / `getImageDimensions()` drive
  AC6/AC7 in `src/lib/image-view.ts`; `getNativeClipboard()` drives the native hop
  in `src/lib/clipboard-image.ts`; `Editor.insertTextAtCursor()` inserts the
  `[Image #N]` marker. Imported from `node_modules`, not vendored.
