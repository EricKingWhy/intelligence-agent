# Third-party notices — `desktop/`

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

### Adapted files

| Local file | Upstream file @ commit | Source lines | Nature |
| --- | --- | --- | --- |
| `src/single-instance.ts` | `apps/desktop/src/single-instance.ts` @ `5badb15` | 1–26 | ADAPT (behaviour preserved) |
| `src/backend-controller.ts` | `apps/desktop/src/backend-controller.ts` @ `5badb15` | 1–157 | ADAPT |
| `src/startup-error.ts` | `apps/desktop/src/startup-error.ts` @ `5badb15` | 1–13 | ADAPT |
| `src/tray.ts` | `apps/desktop/src/tray.ts` @ `5badb15` | 1–48 | ADAPT (menu trimmed to open/quit; locale seam reduced to plain labels) |
| `src/quit-confirmation.ts` | `apps/desktop/src/quit-confirmation.ts` @ `5badb15` | 1–96 | ADAPT (inspection type swapped to this repo's shape) |
| `src/background-notice.ts` | `apps/desktop/src/background-notice.ts` @ `5badb15` | 1–~60 | ADAPT (window-close hint marker) |
| `src/directory-picker.ts` | `apps/desktop/src/directory-picker.ts` @ `5badb15` | 1–30 | ADAPT (channel + sender guard) |
| `src/service-proxy.ts` | `apps/desktop/src/web-document.ts` @ `5badb15` | 75–100 | ADAPT (same authenticated-forwarding idea: shell attaches the credential, hop-by-hop response headers are withheld from the renderer; Node `http` server with an explicit socket splice for `upgrade` instead of a Request/Response handler, and a Bearer host token instead of a cookie — W-21 D3) |
| `src/service-proxy.ts` | `packages/client/connection/src/browser-auth.ts` @ `5badb15` | 25–36, 106–107, 243–261, 285–296 | ADAPT (#891 P2 caller verification: launch-token → HMAC-signed authority-bound session cookie → fail-closed verify; payload shape 25–36, authority binding 106–107, launch-token exchange 243–261, verify incl. timing-safe compare 285–296; per-start secret instead of DSH's persistent credential store) |
| `src/preload.cts` | `apps/desktop/src/preload-app.ts` @ `5badb15` | 76–84 | ADAPT (#826 / MM-05: the `__DSH_HOST_PATHS__` shape — a single `pathFor(file)` over Electron's `webUtils.getPathForFile` — becomes `__IA_HOST_PATHS__` on its own global. Upstream gates the whole exposure block on its own scheme (`dsh-app://app`); here the gate is the ownership check this preload already used (`isOwnLocalPage`), and an unowned frame gets **no** host-path global at all. Not ported: directory references and the rail chip — see `web/src/lib/hostFiles.ts` and `web/THIRD_PARTY_NOTICES.md`) |

> The upstream license text above is quoted verbatim from `LICENSE` at
> `5badb15009ae1756c3afe0ae0cef1faafc290ccc`; the copyright year was corrected
> from `2024` to `2026` in #826 after re-reading that file at the pinned commit.

## PI-Desktop (`vastsa/PI-Desktop`) — reference only, no code copied

- **Commit read:** `1e07bad33a298b7738e7abfaeefe528da6b4a378`
- **License:** LGPL-3.0. Because it is copyleft, this is a **read-only
  reference**: no PI-Desktop code was copied, adapted, or ported into this
  package, and none of its source is redistributed here.
- **Used for:** the preload constraint recorded in `src/preload.cts` —
  `apps/desktop/electron.vite.config.ts:85-97` states that a preload must be a
  fully bundled CJS file to run in a sandboxed renderer (and emits `format:
  'cjs'`). This repo reached the same conclusion by direct measurement on
  Electron 44 (see the header there) and satisfies it without a bundler.

## VS Code (`microsoft/vscode`) — reference only, no code copied

- **License:** MIT
- **Used for:** the `ia-tui.cmd` launcher shape (W-21 D5 / #817) —
  `bin/code.cmd` runs the Electron binary as plain Node
  (`set ELECTRON_RUN_AS_NODE=1` + `"%~dp0..\Code.exe" <cli.js> %*`) so one
  installed artifact carries both the GUI and the CLI without a second Node
  runtime. The launcher here is five conventional lines naming this product's
  own paths; no VS Code source is copied.

## OpenHands (`OpenHands/OpenHands`)

- **Commit read for this reference:** `b0a1a2d1368a50a890b69ef45c54e1a74140b677`
- **License:** MIT
- **Use:** the two-level readiness check in `src/health.ts` is a **PORT DESIGN**
  of `electron/main.mjs::waitForUrl` (lines 262–274, proxy bound / status < 500)
  followed by `electron/main.mjs::waitForAgentServer` (lines 287–310, HTTP 200).
  No OpenHands code is copied; the Python toolchain / `uvx` download path is not
  reproduced (this repo's Python service is owned by W-11).

## DeepSeek Harness `host-process.ts` / `quit-inspection.ts` (PORT DESIGN only)

`src/service-host.ts` and `src/quit-inspection.ts` borrow only the **request /
response shape** of `apps/desktop/src/host-process.ts` (`quit-inspection` /
`update-tasks` control-request correlation, `QUIT_INSPECTION_DEADLINE_MS = 2_000`)
and the fail-safe "unknown counts as busy" rule. No TypeScript is copied from
those files, and the DeepSeek account/Host coupling they carry is not reproduced:
the backend here is the repository's own Python service (W-11) reached over
loopback HTTP.

## #361 [W-16] installer adaptations

| Local file | Upstream file @ commit | Source lines | Nature |
| --- | --- | --- | --- |
| `installer/installer-directories.nsh` | `apps/desktop/scripts/installer-directories.nsh` @ `5badb15` | 1–135 | ADAPT (stage→promote→rollback protocol preserved; `dsh`→`ia` prefixes; adapted to stock electron-builder hooks — the stock template owns extraction so staging happens in `customInit`) |
| `installer/installer.nsh` | `apps/desktop/scripts/installer.nsh` @ `5badb15` | 1–218 | ADAPT (per-user refusal pattern; bilingual strings; DSH's custom-template extraction/window-frame.dll/brand machinery not reproduced — stock electron-builder hooks only) |
| `scripts/test-windows-installer.mjs` | `apps/desktop/scripts/test-windows-installer.mjs` @ `5badb15` | 1–125 | ADAPT (isolated GUID per run, win32/x64 guard, en_US+zh_CN, --compile-only/--uninstall-only; DSH signing/packaging infra not reproduced) |
| `src/installer/preflight.ts` | `apps/desktop/README.md` @ `5badb15` | L39 | PORT DESIGN (ask the Host about in-flight tasks before update; fail-closed abort) |

`scripts/build-windows-installer.mjs` resource layout (`asar: false`,
`extraResources` for runtimes, NSIS `perMachine: false`) is a PORT DESIGN of
OpenHands `electron-builder.config.mjs` @ `b0a1a2d` (no OpenHands code copied;
their `uvx` first-run download is explicitly not reproduced — this repo's
runtime is bundled offline per the lockfile).

## Bundled runtimes

### Node.js (`nodejs/node`) — terminal client runtime (W-21 D5)

- **Version:** `24.21.0` (win-x64), pinned in `installer/node-runtime.lock.json`
  with URL + SHA-256 from `https://nodejs.org/dist/v24.21.0/SHASUMS256.txt`.
- **License:** MIT. The upstream archive's `LICENSE` file is staged next to the
  binary (`installer/staging/node/LICENSE` → `<install>/resources/node/LICENSE`),
  which is the notice required by the license; the runtime additionally
  contains OpenSSL (Apache-2.0), ICU, V8, zlib and other components whose
  notices ship with the upstream distribution.
- **Shipped as:** `resources/node/node.exe`, run by `installer/ia-tui.cmd`.
  Only `node.exe` and the license are redistributed — no npm tree.
- **Why not the app's own Electron binary (the VS Code `bin/code.cmd` shape):**
  measured on Electron 44.5.1, `ELECTRON_RUN_AS_NODE=1` provides no TTY
  (`process.stdin.isTTY` undefined, no `setRawMode`, `node:tty` reopens fail
  with `ERR_TTY_INIT_FAILED`), so it can host a line-oriented CLI but not a
  raw-mode TUI. Pattern reference: DSH `primary-runtime` supplies Node for the
  surface that needs a real console (`.agents/notes/…/2026-09-11-desktop-electron-node-runtime.md`
  @ `5badb15`, MIT — read as a design reference, no code copied).

### CPython (`astral-sh/python-build-standalone`)

- **Version:** `3.12.14` (x86_64-pc-windows-msvc), pinned in
  `installer/python-runtime.lock.json`; the archive ships its own
  `LICENSE.txt`, staged alongside the interpreter.
