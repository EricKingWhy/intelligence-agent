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

Copyright (c) 2024 DeepSeek

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
