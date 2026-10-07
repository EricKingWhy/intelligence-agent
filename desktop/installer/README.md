# Windows x64 installer — #361 [W-16]

Per-user NSIS installer for the Intelligence Agent desktop shell. Installs
with no admin rights, bundles an offline Python runtime (no system
Python/Node needed), keeps user data outside the install dir, updates
atomically with rollback, and never deletes user data on uninstall.

## Layout

```
installer/
  installer.nsh               stock electron-builder custom include
                              (customHeader/customInit/customInstall/customUnInstall)
  installer-directories.nsh   atomic directory swap + rollback
                              (ADAPT DeepSeek Harness, MIT — see header)
  python-runtime.lock.json    pinned offline Python runtime + wheel closure
  README.md                   this file
scripts/
  build-windows-installer.mjs electron-builder build (--compile-only for checks)
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
  either deletes the backup or rolls back. A backup that cannot be restored
  is left in place, never deleted.
- **User data isolation**: `%APPDATA%\intelligence-agent` (set via
  `app.setPath('userData', …)` in `src/main.ts`), always outside the install
  dir. Uninstall never touches it; explicit cleanup is
  `scripts/clean-user-data.mjs --preview` / `--confirm`.
- **Update preflight** (DSH README L39): `src/installer/preflight.ts`
  reuses the W-12 `POST /api/sessions/{id}/client-exit` seam to list
  in-flight Tasks, safe-pauses them, and aborts the update when any signal
  fails or the ledger is not settled (fail-closed).

## Preparing the Python runtime (build machine)

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
```

Regenerating the closure (maintainer): on any machine with `uv`,
`uv pip compile --python-platform windows --python-version 3.12 <deps>`,
then refresh `wheels[]` (name/version/filename/url/sha256 from PyPI).
Bump `schemaVersion` if the shape changes.

## Building

```powershell
# checks only (any OS): lockfile schema + config + NSIS inputs
node scripts/build-windows-installer.mjs --compile-only

# full build (Windows x64; needs electron-builder + staged runtime + npm run build)
node scripts/build-windows-installer.mjs
# artifacts: desktop/dist-installer/Intelligence-Agent-Setup-<version>.exe
#            desktop/dist-installer/SHA256SUMS.txt
#            desktop/dist-installer/installer-build.json
```

The build is unsigned (per ticket); every artifact is SHA-256 hashed and the
build record pins the source version and the lockfile hash.

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
