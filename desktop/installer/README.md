# Windows x64 installer — #361 [W-16]

Per-user NSIS installer for the Intelligence Agent desktop shell. Installs
with no admin rights, bundles its offline runtimes (Python for the service,
Node for the terminal client — no system runtime needed), keeps user data
outside the install dir, updates atomically with rollback, and never deletes
user data on uninstall.

## Layout

```
installer/
  installer.nsh               stock electron-builder custom include
                              (customHeader/customInit/customInstall/customUnInstall)
  installer-directories.nsh   atomic directory swap + rollback
                              (ADAPT DeepSeek Harness, MIT — see header)
  python-runtime.lock.json    pinned offline Python runtime + wheel closure
  node-runtime.lock.json      pinned offline Node runtime (terminal client)
  ia-tui.cmd                  terminal client launcher, installed at the root
  README.md                   this file
scripts/
  build-windows-installer.mjs electron-builder build (--compile-only for checks)
  prepare_python_runtime.py   stage the Python runtime from its lockfile
  prepare_node_runtime.py     stage the Node runtime from its lockfile
  test-windows-installer.mjs  install/uninstall smoke test (Windows x64 only)
  windows-installer-smoke.ps1   install assertions (driven by the above)
  windows-uninstall-smoke.ps1   uninstall assertions incl. data preservation
  clean-user-data.mjs         explicit, preview-first user-data cleanup
src/installer/
  paths.ts                    install/data/python path resolution (tested)
  preflight.ts                manual-update preflight (tested)
```

## Design (mature-product basis)

- **Offline runtime** (DSH `primary-runtime/lock.json` pattern, OpenHands
  `extraResources` layout): `python-runtime.lock.json` pins the CPython
  build + the full win_amd64 wheel closure with SHA-256. The runtime is
  extracted to `<install>\resources\python\`; the shell prefers
  `<resources>\python\python.exe` when present (`src/installer/paths.ts`
  `resolvePythonPath`), so the installed app never needs a system Python.
  First run extracts/copies — it never downloads.
- **Per-user install** (DSH / OpenHands / VS Code): `perMachine: false`,
  default `%LOCALAPPDATA%\Programs\Intelligence Agent`, no admin rights. A
  per-machine install of the same app id is refused with a message.
- **Atomic update + rollback** (DSH `installer-directories.nsh` protocol,
  adapted to stock electron-builder hooks): the new installer's `customInit`
  renames the live dir to `$INSTDIR.old-<guid>` before the install section
  (a running app locks its directory, so the rename also guards against
  updating over a live process); `customInstall` verifies the new files and
  then deletes the backup, records it, or rolls back. A delete that fails
  because a child cannot be removed (#901) leaves the install finished, the
  directory in place and its path in `IaLeftoverDir`, so the next promote can
  still name it; a backup that cannot be restored is left in place, never
  deleted.
- **User data isolation**: `%APPDATA%\intelligence-agent` (set via
  `app.setPath('userData', …)` in `src/main.ts`), always outside the install
  dir. Uninstall never touches it; explicit cleanup is
  `scripts/clean-user-data.mjs --preview` / `--confirm`.
- **Update preflight** (DSH README L39): `src/installer/preflight.ts`
  reuses the W-12 `POST /api/sessions/{id}/client-exit` seam to list
  in-flight Tasks, safe-pauses them, and aborts the update when any signal
  fails or the ledger is not settled (fail-closed).

## Preparing the Python runtime (build machine)

One command (idempotent; needs `uv`, the build backend pinned in `pyproject.toml`):

```powershell
python scripts/prepare_python_runtime.py
# download cache: <repo>/.scratch/python-runtime-cache by default (gitignored); --cache DIR to share it
```

It runs the steps below — download + verify the interpreter and the wheel
closure, extract under `installer/staging/`, build the product wheel from this
checkout and install everything offline — and finishes by importing the
product's CLI in the staged runtime. The manual steps, for reference:

```powershell
# 1. Read the pins
$lock = Get-Content installer/python-runtime.lock.json | ConvertFrom-Json

# 2. Download + verify the interpreter
Invoke-WebRequest -Uri $lock.python.url -OutFile python.tar.gz
if ((Get-FileHash python.tar.gz -Algorithm SHA256).Hash -ne $lock.python.sha256) { throw 'interpreter hash mismatch' }
New-Item -ItemType Directory -Force installer/staging/python | Out-Null
tar -xzf python.tar.gz -C installer/staging/python

# 3. Build the offline wheelhouse from the locked closure and verify hashes
New-Item -ItemType Directory -Force wheelhouse | Out-Null
foreach ($w in $lock.wheels) {
  $dest = "wheelhouse/$($w.filename)"
  Invoke-WebRequest -Uri $w.url -OutFile $dest
  if ((Get-FileHash $dest -Algorithm SHA256).Hash -ne $w.sha256) { throw "hash mismatch: $($w.filename)" }
}

# 4. Install offline into the bundled runtime (no index, no network at install time)
installer/staging/python/python.exe -m pip install --no-index --find-links wheelhouse `
  ( ($lock.wheels | ForEach-Object { "$($_.name)==$($_.version)" }) -join ' ' )

# 5. The closure of step 4 is dependencies only. The product itself must be in the
#    runtime too, or the bundled service has nothing to run (W-21 defect D2).
uv build --wheel --out-dir wheelhouse
installer/staging/python/python.exe -m pip install --no-index --no-deps `
  --force-reinstall wheelhouse/$($lock.product.wheel)
```

`--force-reinstall` is not optional. A rebuilt product keeps the same version
string, so pip's "requirement already satisfied" check would leave the previous
code in the runtime the installer ships — that is how a stale `web/app.py`
reached a packaged build. `scripts/prepare_python_runtime.py` does exactly this
in `install_product`; its dependency closure is installed separately and is
still cached by `install_offline`'s marker.

`scripts/build-windows-installer.mjs` re-asserts step 5 in `afterPack` (product
importable, version equal to `product.version`) and fails the build otherwise.
`product.wheel` must match `pyproject.toml` after a version bump — the prep
script refuses to install a wheel whose name disagrees with the pin.

Regenerating the closure (maintainer): on any machine with `uv`,
`uv pip compile --python-platform windows --python-version 3.12 <deps>`,
then refresh `wheels[]` (name/version/filename/url/sha256 from PyPI).
Bump `schemaVersion` if the shape changes.

## Preparing the Node runtime (build machine)

One command, mirroring the Python runtime above (idempotent; no `uv` needed):

```powershell
python scripts/prepare_node_runtime.py
# download cache: <repo>/.scratch/node-runtime-cache by default (gitignored)
```

It downloads the pinned nodejs.org zip, verifies its SHA-256, extracts
`node.exe` and the archive's `LICENSE` under `installer/staging/node/`, and
finishes by running the staged binary's `--version` against the pin. Manual
steps, for reference:

```powershell
$lock = Get-Content installer/node-runtime.lock.json | ConvertFrom-Json
Invoke-WebRequest -Uri $lock.node.url -OutFile node.zip
if ((Get-FileHash node.zip -Algorithm SHA256).Hash -ne $lock.node.sha256) { throw 'node hash mismatch' }
Expand-Archive node.zip -DestinationPath node-extract
New-Item -ItemType Directory -Force installer/staging/node | Out-Null
Copy-Item node-extract/node-v$($lock.node.version)-win-x64/node.exe installer/staging/node/
Copy-Item node-extract/node-v$($lock.node.version)-win-x64/LICENSE installer/staging/node/
installer/staging/node/node.exe --version   # must print v$($lock.node.version)
```

Only `node.exe` and the license are staged: the terminal client needs no `npm`
tree. Regenerate the pin with
`curl -s https://nodejs.org/dist/v<version>/SHASUMS256.txt`.

## Renderer build (W-21 defect D3)

The packaged window is served by the bundled service, so the frontend build has
to be in the package as well:

```powershell
cd ../web && npm run build      # produces web/dist (gitignored)
```

`extraResources` copies it to `<install>\resources\web`, and `afterPack` fails
the build when `resources/web/index.html` is missing. At runtime the shell tells
the service where that directory is (`WEB_DIST_DIR`) and serves the window
through its own loopback proxy, which attaches the host token — the token never
reaches the page, and the page's relative API/WebSocket URLs work unchanged
because the proxy origin *is* the page origin.

## Terminal client (W-21 defect D5)

The same artifact also carries the TUI, so a machine that installed the desktop
has a working `ia-tui` without a second installer:

```powershell
cd ../tui && npm run build   # produces tui/dist (gitignored)
python scripts/prepare_node_runtime.py   # stages installer/staging/node/node.exe
"%LOCALAPPDATA%\Programs\Intelligence Agent\ia-tui.cmd" --check
"%LOCALAPPDATA%\Programs\Intelligence Agent\ia-tui.cmd" --session new
```

`extraResources` copies `tui/dist`, the runtime closure of
`@earendil-works/pi-tui`, and the staged Node runtime to `<install>\resources\tui`
and `<install>\resources\node`; `extraFiles` puts `ia-tui.cmd` next to the app
exe. `afterPack` fails the build when the compiled entry, the launcher, the node
runtime, or any dependency read from the *shipped* `pi-tui` manifest is missing,
and the build refuses to pack a `tui/dist` older than `tui/src` (a stale `dist/`
passes every "file exists" check while shipping the old client).

The launcher runs `resources\node\node.exe` — a real Node, not the app exe in
node mode. VS Code's `bin/code.cmd` shape (`ELECTRON_RUN_AS_NODE=1` + the app
exe) is enough for a line-oriented CLI, but not for a raw-mode terminal, and
the difference is measured on Electron 44.5.1: in node mode `process.stdin.isTTY`
is undefined, `setRawMode` does not exist, and reopening fds 0–2 through
`node:tty` fails with `ERR_TTY_INIT_FAILED`, while the *same* console hands a
real Node those same descriptors as a TTY. This is DSH's `primary-runtime`
pattern — a runtime is bundled for the surface that needs a real console, rather
than reusing the Electron binary. `node-runtime.lock.json` pins the interpreter
(version, URL, SHA-256, staged members) and `assertNodeRuntime` re-probes
`--version` in `afterPack`; the pin must stay at or above the TUI's own
`engines.node` floor, which the lockfile validator enforces.

Both clients share one coordinate set — `%APPDATA%\intelligence-agent\workspace`
as the data root and `host-credentials.json` beside it as the credential
channel — so whichever starts first owns the service and the other attaches to
it (one writer per data root, enforced by the service's `InstanceLock`). A
client exit only signals `client-exit`; it never stops the service.

## Building

```powershell
# checks only (any OS): lockfile schema + config + NSIS inputs
node scripts/build-windows-installer.mjs --compile-only

# full build (Windows x64; needs electron-builder + the staged runtimes + web/dist
# + tui/dist — see "Preparing the Python runtime", "Preparing the Node runtime",
# "Renderer build" and "Terminal client" above)
node scripts/build-windows-installer.mjs
# artifacts: desktop/dist-installer/Intelligence-Agent-Setup-<version>.exe
#            desktop/dist-installer/SHA256SUMS.txt
#            desktop/dist-installer/installer-build.json
```

The build is unsigned (per ticket); every artifact is SHA-256 hashed and the
build record pins the source version and both lockfile hashes.

## Testing

```powershell
# full install/uninstall UI smoke on a real Windows x64 desktop
# (isolated app id per run, en_US + zh_CN, asserts user-data preservation)
node scripts/test-windows-installer.mjs
node scripts/test-windows-installer.mjs --compile-only   # build the installer only
node scripts/test-windows-installer.mjs --uninstall-only # uninstall assertions only
```

Unit tests (any OS): `npm test` in `desktop/` covers `src/installer/*` and
the build/cleanup scripts. NSIS script logic itself can only be verified on
Windows — see "Legacy items" below.

## Data cleanup (explicit only)

```powershell
node scripts/clean-user-data.mjs --preview   # list targets + evidence refs, delete nothing
node scripts/clean-user-data.mjs --confirm   # delete file targets
```

Windows Credential Manager entries are listed but never touched — remove
them by hand. Install/upgrade/uninstall logs never contain secrets.

## Legacy items (not verifiable on Linux)

- Clean Windows x64 VM: install → desktop start → TUI cold start →
  sessions/config visible → short real task → in-flight manual update
  (preflight lists Tasks, safe-pause) → new version reconciles → manual
  resume → uninstall (user data intact). Record SHA-256, VM software list,
  commands/screenshots.
- Failure injection on the VM: service-pause failure, migration
  interruption, disk-full rollback (exercises `iaRollbackApplication`).
- NSIS compile of `installer.nsh` (`--compile-only` mode of
  `test-windows-installer.mjs` on Windows covers this).
- Code signing (ticket explicitly defers public signing).
