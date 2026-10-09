/**
 * #361 [W-16] Windows x64 installer build.
 *
 * Usage:
 *   node scripts/build-windows-installer.mjs [--compile-only] [--out <dir>]
 *
 * --compile-only validates the runtime lockfile, generates the
 * electron-builder config and checks the NSIS includes exist, without
 * invoking electron-builder (runs on any OS; used by CI-adjacent checks).
 *
 * Full build (Windows x64 only) additionally requires:
 *   - electron-builder (npm i -D electron-builder; lazy-required)
 *   - installer/staging/python/python.exe prepared per installer/README.md
 *     (offline Python runtime pinned by installer/python-runtime.lock.json)
 *   - installer/staging/node/node.exe, the same way
 *     (offline Node runtime pinned by installer/node-runtime.lock.json)
 *   - the compiled desktop app (npm run build -> dist/)
 *
 * Resource layout (PORT DESIGN from OpenHands electron-builder.config.mjs,
 * MIT, commit b0a1a2d1368a50a890b69ef45c54e1a74140b677 — asar:false so
 * spawned files are real on disk; extraResources for runtimes; NSIS
 * perMachine:false):
 *   <install>/Intelligence Agent.exe
 *   <install>/resources/app/...          (this package)
 *   <install>/resources/python/python.exe (bundled runtime, from the lockfile)
 *   <install>/resources/node/node.exe     (terminal client runtime, W-21 D5)
 * User data is NOT in the install dir: %APPDATA%\\intelligence-agent.
 */
import { createHash } from 'node:crypto'
import { spawnSync } from 'node:child_process'
import { existsSync, readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const scriptDir = join(fileURLToPath(new URL('.', import.meta.url)))
const desktopDir = resolve(scriptDir, '..')
const repoDir = resolve(desktopDir, '..')
const installerDir = join(desktopDir, 'installer')

/** Minimal well-formed lock, used by tests and as documentation. */
export const GOOD_LOCK = {
  schemaVersion: 1,
  target: { platform: 'win32', arch: 'x64' },
  python: {
    implementation: 'cpython',
    version: '3.12.14',
    url: 'https://github.com/astral-sh/python-build-standalone/releases/download/20260901/cpython-3.12.14%2B20260901-x86_64-pc-windows-msvc-install_only.tar.gz',
    sha256: 'e90c1b6419da3bd812dd73bb3de40287a21abf153438147639ec5e20375ea93f',
  },
  wheels: [
    {
      name: 'pip',
      version: '26.2.1',
      filename: 'pip-26.2.1-py3-none-any.whl',
      url: 'https://files.pythonhosted.org/packages/f3/6e/1736e5b4ae2b778ef2f81c47d797de9f891d4d8acb047a24ca37a60294dd/pip-26.2.1-py3-none-any.whl',
      sha256: '71138adf1f4ca900cdb7d289c21b7494329f2332b6d85f0e1c42108c0384ed3e',
    },
  ],
  product: {
    name: 'intelligence-agent',
    version: '1.0.0',
    module: 'agent_harness',
    wheel: 'intelligence_agent-1.0.0-py3-none-any.whl',
  },
}

const SHA256_RE = /^[0-9a-f]{64}$/
const BACKEND_MIN_PYTHON = [3, 11]

function fail(message) {
  throw new Error(`python-runtime.lock.json: ${message}`)
}

/**
 * Validate the runtime lockfile schema. Throws on the first violation.
 * Pure (testable); the real file lives at installer/python-runtime.lock.json.
 */
export function validateRuntimeLockfile(lock) {
  if (lock === null || typeof lock !== 'object') fail('not a JSON object')
  if (lock.schemaVersion !== 1) fail(`unsupported schemaVersion ${String(lock.schemaVersion)}`)
  if (lock.target?.platform !== 'win32' || lock.target?.arch !== 'x64') {
    fail('target must be { platform: "win32", arch: "x64" }')
  }
  const python = lock.python
  if (python === null || typeof python !== 'object') fail('missing "python" pin')
  const [major, minor] = String(python.version ?? '').split('.').map(Number)
  if (!Number.isInteger(major) || !Number.isInteger(minor)) fail(`bad python.version ${String(python.version)}`)
  if (major < BACKEND_MIN_PYTHON[0] || (major === BACKEND_MIN_PYTHON[0] && minor < BACKEND_MIN_PYTHON[1])) {
    fail(`python ${python.version} is below the backend minimum 3.11 (pyproject requires-python)`)
  }
  if (typeof python.url !== 'string' || !python.url.startsWith('https://')) fail('python.url must be https')
  if (!SHA256_RE.test(python.sha256 ?? '')) fail('python.sha256 must be 64 lowercase hex chars')
  if (!Array.isArray(lock.wheels)) fail('"wheels" must be an array')
  for (const wheel of lock.wheels) {
    for (const field of ['name', 'version', 'filename', 'url', 'sha256']) {
      if (typeof wheel[field] !== 'string' || wheel[field] === '') fail(`wheel is missing ${field}`)
    }
    if (!wheel.url.startsWith('https://')) fail(`wheel ${wheel.name}: url must be https`)
    if (!SHA256_RE.test(wheel.sha256)) fail(`wheel ${wheel.name}: bad sha256`)
    // The filename must be consistent with name+version (PEP 427/503), so a
    // hand-edited lock cannot silently point at a different wheel.
    const norm = (s) => s.replace(/[-_.]+/g, '-').toLowerCase()
    const fileDist = wheel.filename.split('-')[0]
    if (norm(fileDist) !== norm(wheel.name)) {
      fail(`wheel ${wheel.name}: filename ${wheel.filename} does not match name`)
    }
    if (!wheel.filename.includes(`-${wheel.version}-`)) {
      fail(`wheel ${wheel.name}: filename ${wheel.filename} does not match version ${wheel.version}`)
    }
  }
  // W-21 D2: the closure above is dependencies only — the product itself must be
  // pinned too, or the bundled runtime cannot start the service at all.
  validateProductPin(lock.product)
}

/** A bare wheel filename (no path separators) — the name/version checks follow. */
const WHEEL_FILENAME_RE = /^[^/\\]+\.whl$/

/** Validate the `product` pin (name/version/module/wheel). Throws on violations. */
export function validateProductPin(product) {
  if (product === null || typeof product !== 'object') fail('missing "product" pin')
  for (const field of ['name', 'version', 'module', 'wheel']) {
    if (typeof product[field] !== 'string' || product[field] === '') {
      fail(`product is missing ${field}`)
    }
  }
  if (!/^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$/.test(product.module)) {
    fail(`product.module ${product.module} is not a dotted Python module path`)
  }
  // PEP 427 filename must agree with name+version, so a version bump that
  // forgets this pin fails the build instead of installing the wrong wheel.
  const norm = (s) => s.replace(/[-_.]+/g, '-').toLowerCase()
  if (!WHEEL_FILENAME_RE.test(product.wheel)) {
    fail(`product.wheel ${product.wheel} is not a wheel filename`)
  }
  if (norm(product.wheel.split('-')[0]) !== norm(product.name)) {
    fail(`product.wheel ${product.wheel} does not match product.name ${product.name}`)
  }
  if (!product.wheel.includes(`-${product.version}-`)) {
    fail(`product.wheel ${product.wheel} does not match product.version ${product.version}`)
  }
}

/**
 * Probe run by the bundled interpreter: print the installed distribution
 * version and import the CLI entry point (pyproject [project.scripts]).
 */
export function runtimeProductProbe(product) {
  return [
    'import importlib, importlib.metadata as m',
    `mod = importlib.import_module(${JSON.stringify(product.module)} + '.cli')`,
    `assert callable(mod.main), ${JSON.stringify(`${product.module}.cli:main is not callable`)}`,
    `print(m.version(${JSON.stringify(product.name)}))`,
  ].join('; ')
}

/**
 * Assert the bundled runtime contains the product package (not just its
 * dependencies) at the pinned version. `run` is injectable for unit tests;
 * on Windows it is node's spawnSync against the staged interpreter.
 */
export function assertRuntimeProduct({ pythonExe, product, run = spawnSync }) {
  if (product === null || typeof product !== 'object') {
    throw new Error('python-runtime.lock.json: missing "product" pin')
  }
  const result = run(pythonExe, ['-c', runtimeProductProbe(product)], { encoding: 'utf8' })
  if (result.error) throw new Error(`bundled python failed to start (${pythonExe}): ${result.error.message}`)
  const stderr = String(result.stderr ?? '').trim()
  if (result.status !== 0) {
    const detail = stderr.split('\n').filter((line) => line.trim() !== '').pop() ?? ''
    throw new Error(
      `bundled runtime lacks the product package ${product.name}==${product.version} ` +
        `(${pythonExe} exited ${String(result.status)}): ${detail} — ` +
        'stage the runtime with desktop/scripts/prepare_python_runtime.py (installer/README.md)',
    )
  }
  const version = String(result.stdout ?? '').trim().split('\n').pop().trim()
  if (version !== product.version) {
    throw new Error(
      `bundled ${product.name} is ${version}, python-runtime.lock.json pins ${product.version}`,
    )
  }
  return version
}

/** SHA-256 hex of a file. */
export function sha256File(path) {
  return createHash('sha256').update(readFileSync(path)).digest('hex')
}

/** Minimal well-formed Node runtime lock, used by tests and as documentation. */
export const GOOD_NODE_LOCK = {
  schemaVersion: 1,
  target: { platform: 'win32', arch: 'x64' },
  node: {
    version: '24.21.0',
    url: 'https://nodejs.org/dist/v24.21.0/node-v24.21.0-win-x64.zip',
    sha256: '158f7685b44de51f6c0df1d153526cbcd3e1bc739a8dfc607721cef75de9e541',
  },
  requirements: { engines: '>=22', minimumMajor: 22 },
  layout: { resourcesDir: 'node', executable: 'node/node.exe' },
}

function failNode(message) {
  throw new Error(`node-runtime.lock.json: ${message}`)
}

/**
 * Validate the Node runtime lockfile (W-21 D5 / #817). Throws on violations.
 * Pure (testable); the real file lives at installer/node-runtime.lock.json.
 *
 * The pin has to satisfy the TUI's own `engines.node` (tui/package.json), so a
 * hand-edited lock cannot ship a runtime older than the client needs.
 */
export function validateNodeRuntimeLockfile(lock) {
  if (lock === null || typeof lock !== 'object') failNode('not a JSON object')
  if (lock.schemaVersion !== 1) failNode(`unsupported schemaVersion ${String(lock.schemaVersion)}`)
  if (lock.target?.platform !== 'win32' || lock.target?.arch !== 'x64') {
    failNode('target must be { platform: "win32", arch: "x64" }')
  }
  const node = lock.node
  if (node === null || typeof node !== 'object') failNode('missing "node" pin')
  const [major] = String(node.version ?? '').split('.').map(Number)
  if (!Number.isInteger(major)) failNode(`bad node.version ${String(node.version)}`)
  const minimum = lock.requirements?.minimumMajor
  if (!Number.isInteger(minimum)) failNode('requirements.minimumMajor must be an integer')
  if (major < minimum) {
    failNode(`node ${node.version} is below requirements.minimumMajor ${String(minimum)} (tui engines)`)
  }
  if (typeof node.url !== 'string' || !node.url.startsWith('https://')) failNode('node.url must be https')
  if (!SHA256_RE.test(node.sha256 ?? '')) failNode('node.sha256 must be 64 lowercase hex chars')
  if (typeof lock.layout?.resourcesDir !== 'string' || lock.layout.resourcesDir === '') {
    failNode('layout.resourcesDir is required')
  }
  if (lock.layout?.executable !== `${lock.layout.resourcesDir}/node.exe`) {
    failNode(`layout.executable must be ${lock.layout.resourcesDir}/node.exe`)
  }
}

/**
 * Assert the staged Node runtime is the pinned one. `run` is injectable for
 * unit tests; on Windows it is node's spawnSync against the staged binary.
 */
export function assertNodeRuntime({ nodeExe, version, run = spawnSync }) {
  const result = run(nodeExe, ['--version'], { encoding: 'utf8' })
  if (result.error) {
    throw new Error(
      `bundled node failed to start (${nodeExe}): ${result.error.message} — ` +
        'stage it with desktop/scripts/prepare_node_runtime.py (installer/README.md)',
    )
  }
  const actual = String(result.stdout ?? '').trim()
  if (result.status !== 0 || actual !== `v${version}`) {
    const detail = String(result.stderr ?? '').trim().split('\n').pop() ?? ''
    throw new Error(
      `bundled node reports ${actual === '' ? `exit ${String(result.status)}` : actual} ` +
        `(${nodeExe}): node-runtime.lock.json pins v${version} ${detail}`,
    )
  }
  return actual
}

/** Index document of the packaged renderer build (W-21 D3). */
const WEB_INDEX = join('web', 'index.html')

/** Terminal client launcher shipped at the install root (W-21 D5). */
const TUI_LAUNCHER = 'ia-tui.cmd'

/** The TUI's only runtime dependency; its own dependencies follow it (W-21 D5). */
const TUI_ROOT_PACKAGE = '@earendil-works/pi-tui'

/**
 * Assert the packaged app carries the renderer build (W-21 D3 / #815).
 *
 * The shell loads its window from the local service, which serves this
 * directory; without `index.html` the window renders nothing (the frozen
 * defect). `existsSync` is injectable so the check is unit-testable off-Windows.
 */
export function assertBundledWebAssets({ resourcesDir, existsSync: exists = existsSync }) {
  const index = join(resourcesDir, WEB_INDEX)
  if (!exists(index)) {
    throw new Error(
      `bundled renderer build missing: ${index} — build it with \`npm run build\` in web/ ` +
        '(the installer ships web/dist as resources/web)',
    )
  }
  return index
}

/**
 * Assert the NSIS custom include's placement rules (#816 / W-21 D1).
 *
 * Both rules come from measured failures of the real build, and neither is
 * visible in a plain "file exists" check:
 *   1. `!include "${__FILEDIR__}…"` inside a macro body is substituted when the
 *      macro is inserted — in the *inserting* file's context, i.e.
 *      `app-builder-lib/templates/nsis` — so the build died with
 *      `!include: could not find: …\templates\nsis\installer-directories.nsh`.
 *      The path has to be captured by a top-level `!define` instead (measured:
 *      #816).
 *   2. That include must be installer-only (`!ifndef BUILD_UNINSTALLER`): the
 *      uninstaller build inserts `customHeader` too but references none of those
 *      functions, so including them there fails with
 *      `warning 6010: install function "iaPromoteApplication" not referenced`
 *      (fatal under electron-builder's makensis settings).
 *
 * @param source - text of the custom include file (`installer/installer.nsh`).
 * @param includedFile - basename of the file the installer build must pull in.
 */
export function assertNsisIncludePlacement(source, includedFile) {
  const isComment = (line) => /^\s*(;|$)/.test(line)
  let inMacro = false
  let installerOnly = false
  let sawInclude = false
  for (const line of source.split(/\r?\n/)) {
    if (isComment(line)) continue
    if (/^\s*!macro\b/.test(line)) inMacro = true
    else if (/^\s*!macroend\b/.test(line)) inMacro = false
    if (inMacro && /!include\s+"\$\{__FILEDIR__\}/.test(line)) {
      throw new Error(
        'installer.nsh: a ${__FILEDIR__} include inside a macro body resolves against the stock ' +
          'template directory at insertion time — capture the path in a top-level !define instead (#816)',
      )
    }
    if (/!ifndef\s+BUILD_UNINSTALLER/.test(line)) installerOnly = true
    if (line.includes('!include') && line.includes(includedFile)) {
      sawInclude = true
      if (inMacro && !installerOnly) {
        throw new Error(
          `installer.nsh: ${includedFile} is included from a macro body without an enclosing ` +
            '!ifndef BUILD_UNINSTALLER guard — the uninstaller build inserts the same macro and ' +
            'fails on unreferenced install functions (#816)',
        )
      }
    }
  }
  if (!sawInclude) {
    throw new Error(`installer.nsh does not include ${includedFile}`)
  }
}

/**
 * Assert the packaged TUI runs from the artifact (W-21 D5 / #817).
 *
 * The TUI is shipped as `<resources>/tui` (compiled `dist/` + its runtime
 * dependency closure) and started by `ia-tui.cmd` with the bundled Node runtime
 * at `<resources>/node/node.exe`. `pi-tui` is the one runtime dependency; its
 * own dependencies are read from the *shipped* manifest, so a version bump that
 * adds a dependency fails the build instead of producing a TUI that cannot
 * import (`marked` / `get-east-asian-width` today).
 *
 * @param options.resourcesDir - `<appOutDir>/resources`.
 * @param options.appOutDir - directory the app exe and the launcher live in.
 * @param options.readJson - injectable file reader (unit-testable off-Windows).
 * @param options.existsSync - injectable existence check.
 */
/**
 * Assert every `LangString` in installer.nsh is behind its own language guard
 * (W-21 D7 / #831).
 *
 * `LangString <name> ${LANG_X}` names a language; when that language is not
 * loaded in the compile at hand, makensis emits `warning 7025` and
 * electron-builder turns makensis warnings into errors. The W-16 smoke builds
 * one language per run (`installerLanguages: [language]`), which is how a
 * bilingual, unguarded block aborted the uninstaller pass of its en_US run.
 * `!ifdef LANG_X` is the portable guard: `customHeader` is inserted after
 * `addLangs` (app-builder-lib installer.nsi:43 then :45) and
 * `LoadLanguageFile` defines `${LANG_<NAME>}` (measured on makensis 3.0.4.1).
 * Only a real makensis run exposes a missing guard, so it is asserted here.
 */
export function assertNsisLangStringGuards(source) {
  // Each open !if/!ifdef/!ifndef pushes the language its body is conditional
  // on, or null for unrelated conditions; !else clears the current level.
  const guards = []
  const unguarded = []
  for (const [index, raw] of source.split(/\r?\n/).entries()) {
    if (/^\s*(;|$)/.test(raw)) continue
    const languageGuard = /^\s*!\s*ifn?def\s+(LANG_[A-Z0-9_]+)\s*$/.exec(raw)
    if (languageGuard) {
      guards.push(languageGuard[1])
    } else if (/^\s*!\s*if(n?def)?\b/.test(raw)) {
      guards.push(null)
    } else if (/^\s*!\s*else\b/.test(raw)) {
      if (guards.length > 0) guards[guards.length - 1] = null
    } else if (/^\s*!\s*endif\b/.test(raw)) {
      guards.pop()
    } else {
      const declaration = /^\s*LangString\s+\S+\s+\$\{(LANG_[A-Z0-9_]+)\}/.exec(raw)
      if (declaration && !guards.includes(declaration[1])) {
        unguarded.push(`line ${index + 1}: ${declaration[1]}`)
      }
    }
  }
  if (unguarded.length > 0) {
    throw new Error(
      `installer.nsh: LangString declarations missing their own language guard ` +
        `(${unguarded.join(', ')}) — a language this build does not load fails the makensis ` +
        'run with `warning 7025: is not a valid language id`, which electron-builder treats as ' +
        'an error (#831). Wrap each language in `!ifdef LANG_<NAME>`',
    )
  }
}

export function assertTuiRuntimeClosure({
  resourcesDir,
  appOutDir,
  readJson = (path) => JSON.parse(readFileSync(path, 'utf8')),
  existsSync: exists = existsSync,
}) {
  const entry = join(resourcesDir, 'tui', 'dist', 'src', 'index.js')
  if (!exists(entry)) {
    throw new Error(
      `bundled TUI missing: ${entry} — build it with \`npm run build\` in tui/ ` +
        '(the installer ships tui/dist as resources/tui/dist)',
    )
  }
  const launcher = join(appOutDir, TUI_LAUNCHER)
  if (!exists(launcher)) {
    throw new Error(`TUI launcher missing: ${launcher} — installer extraFiles must ship it`)
  }
  const nodeExe = join(resourcesDir, 'node', 'node.exe')
  if (!exists(nodeExe)) {
    throw new Error(
      `bundled node runtime missing: ${nodeExe} — the TUI needs a real console, which the app's ` +
        'own Electron binary cannot provide; stage it with scripts/prepare_node_runtime.py',
    )
  }
  const modulesDir = join(resourcesDir, 'tui', 'node_modules')
  const pending = [TUI_ROOT_PACKAGE]
  const seen = new Set()
  while (pending.length > 0) {
    const name = pending.pop()
    if (seen.has(name)) continue
    seen.add(name)
    const manifest = join(modulesDir, ...name.split('/'), 'package.json')
    if (!exists(manifest)) {
      throw new Error(
        `bundled TUI dependency missing: ${manifest} — add it to the installer's tui ` +
          'extraResources filter (npm ls --omit=dev in tui/)',
      )
    }
    for (const dependency of Object.keys(readJson(manifest).dependencies ?? {})) {
      pending.push(dependency)
    }
  }
}

/**
 * Assert a compiled entry point is newer than the sources it was built from.
 *
 * The installer build never runs `tsc` (W-21 D3 lesson: a stale compiled module
 * shipped in an otherwise green build), so a stale `dist/` passes every
 * "file exists" check while the artifact carries the old product.
 *
 * @param options.entry - compiled entry file the artifact ships.
 * @param options.newestSourceMtimeMs - newest mtime under the source tree.
 * @param options.entryMtimeMs - mtime of the compiled entry.
 */
export function assertFreshBuild({ entry, newestSourceMtimeMs, entryMtimeMs }) {
  if (entryMtimeMs < newestSourceMtimeMs) {
    throw new Error(`stale build: ${entry} is older than its sources — rebuild before packing`)
  }
}

/**
 * The desktop shell the artifact runs must itself be fresh (W-21 D10 / #836).
 *
 * `files: ['dist/**\/*']` copies the compiled shell verbatim, so a build run without
 * `npm run build` ships the previous shell while every other check stays green
 * (observed: an artifact whose `resources/app/dist/src/service-host.js` predated the
 * D8 fix). Same assertion the TUI entry already gets, one entry higher.
 *
 * @param options.entry - compiled shell entry (`dist/src/main.js`).
 * @param options.sourceDir - `desktop/src`.
 * @param options.newestSourceMtimeMs - injectable, defaults to the real tree.
 * @param options.entryMtimeMs - injectable, defaults to the real file.
 */
export function assertDesktopBuildFresh({
  entry,
  sourceDir,
  newestSourceMtimeMs = newestMtimeMs(sourceDir),
  entryMtimeMs = statSync(entry).mtimeMs,
}) {
  assertFreshBuild({ entry, newestSourceMtimeMs, entryMtimeMs })
}

/** Newest mtime (ms) in a directory tree; 0 when the tree does not exist. */
function newestMtimeMs(dir) {
  let newest = 0
  let entries
  try {
    entries = readdirSync(dir, { withFileTypes: true })
  } catch {
    return 0
  }
  for (const entry of entries) {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) newest = Math.max(newest, newestMtimeMs(full))
    else newest = Math.max(newest, statSync(full).mtimeMs)
  }
  return newest
}

/**
 * Content hashes of every Python module in a package tree, keyed by relative path.
 *
 * Content, not mtime and not version: the runtime carries a `pip install` of a wheel
 * whose version string never changes between commits (W-21 D9 / #835 — the installer
 * shipped a pre-D5 `web/app.py` because nothing compared the code), while pip writes
 * install-time mtimes.
 *
 * @param root - package directory to walk.
 * @param deps - injectable filesystem, for tests.
 * @returns Map of relative POSIX path → sha256 hex.
 */
export function pythonFileHashes(root, { readdir = readdirSync, readFile = readFileSync } = {}) {
  const hashes = new Map()
  const walk = (dir, prefix) => {
    let entries
    try { entries = readdir(dir, { withFileTypes: true }) } catch { return }
    for (const entry of entries) {
      if (entry.name === '__pycache__') continue
      const relative = prefix === '' ? entry.name : `${prefix}/${entry.name}`
      if (entry.isDirectory()) walk(join(dir, entry.name), relative)
      else if (entry.name.endsWith('.py')) {
        hashes.set(relative, createHash('sha256').update(readFile(join(dir, entry.name))).digest('hex'))
      }
    }
  }
  walk(root, '')
  return hashes
}

/**
 * One digest over a package tree's module hashes (recorded in the build metadata so
 * an artifact names the product code it was built from).
 *
 * @param root - package directory.
 * @param deps - injectable filesystem, for tests.
 * @returns sha256 hex over the sorted `path:hash` lines.
 */
export function productCodeDigest(root, deps) {
  const lines = [...pythonFileHashes(root, deps).entries()]
    .map(([path, hash]) => `${path}:${hash}`)
    .sort()
  if (lines.length === 0) throw new Error(`no Python modules under ${root}`)
  return createHash('sha256').update(lines.join('\n')).digest('hex')
}

/**
 * Directory the staged interpreter imports the product module from.
 *
 * Asking the staged runtime is the only layout-free answer: it is the same
 * interpreter the installer ships, whatever the runtime's directory shape.
 *
 * @param options.pythonExe - staged interpreter.
 * @param options.module - top-level product module (lockfile `product.module`).
 * @param options.run - injectable spawn, for tests.
 * @returns absolute path of the imported package.
 */
export function stagedProductDir({ pythonExe, module, run = spawnSync }) {
  const probe = `import importlib, os; print(os.path.dirname(importlib.import_module(${JSON.stringify(module)}).__file__))`
  const result = run(pythonExe, ['-c', probe], { encoding: 'utf8' })
  if (result.status !== 0) {
    const detail = result.error !== undefined
      ? result.error.message
      : String(result.stderr ?? '').trim() || `exit ${String(result.status)}`
    throw new Error(`staged runtime does not import ${module} (${pythonExe}): ${detail}`)
  }
  const path = String(result.stdout ?? '').trim().split(/\r?\n/).at(-1)?.trim() ?? ''
  if (path === '') throw new Error(`staged runtime printed no path for ${module}`)
  return path
}

/**
 * The packaged runtime must carry THIS checkout's product code (W-21 D9 / #835).
 *
 * `assertRuntimeProduct` only checks the distribution *version*, and the version does
 * not move between commits — so a staged copy that lags the checkout passes every
 * existing check and ships silently. This compares module content instead, and fails
 * with the command that refreshes staging. Same spirit as `assertFreshBuild` (stale
 * compiled entry) one layer down.
 *
 * @param options.sourceDir - `src/<module>` in this checkout.
 * @param options.stagedDir - the package the staged runtime imports.
 * @param deps - injectable filesystem, for tests (`readdir`, `readFile`).
 */
export function assertStagedProductMatchesSource({ sourceDir, stagedDir, ...deps }) {
  const source = pythonFileHashes(sourceDir, deps)
  if (source.size === 0) throw new Error(`no product modules under ${sourceDir}`)
  const staged = pythonFileHashes(stagedDir, deps)
  const missing = [...source.keys()].filter((path) => !staged.has(path))
  const drifted = [...source.keys()].filter((path) => staged.has(path) && staged.get(path) !== source.get(path))
  if (missing.length > 0 || drifted.length > 0) {
    const report = [
      ...drifted.slice(0, 5),
      ...missing.slice(0, 5).map((path) => `${path} (missing)`),
      `${String(missing.length + drifted.length)} file(s)`,
    ].join(', ')
    throw new Error(
      `staged product differs from the checkout: ${report} — run `
      + '"python desktop/scripts/prepare_python_runtime.py" before packing',
    )
  }
}

/**
 * Pure electron-builder configuration for the Windows x64 installer.
 * Kept pure (no electron-builder import) so it is unit-testable on any OS.
 */
/** The `${LANG_<NAME>}` symbol a LangString line references, or null. */
function langStringSymbol(line) {
  const match = /^\s*LangString\s+\S+\s+\$\{LANG_([A-Z0-9_]+)\}/.exec(line)
  return match ? match[1] : null
}

/**
 * Open a conditional-compilation frame. Every opener is pushed so a later
 * `!endif` pops the right frame. Only `!ifdef` carries the symbols the guard
 * check tests: an `!ifndef LANG_X` frame must never count as a guard, so its
 * symbols are not collected (`kind` already distinguishes it).
 */
function conditionalFrame(line) {
  const match = /^\s*!(if|ifdef|ifndef|ifmacrodef|ifmacrondef)\b(.*)$/.exec(line)
  if (!match) return null
  const kind = match[1]
  const symbols =
    kind === 'ifdef' ? new Set(match[2].match(/[A-Za-z_][A-Za-z0-9_]*/g) ?? []) : new Set()
  return { kind, symbols }
}

/**
 * Every `LangString <name> ${LANG_<X>}` line that is NOT enclosed in a matching
 * `!ifdef LANG_<X>` block, as `{ line, symbol }` (line is 1-based). See
 * `validateLangStringGuards` for why this matters (#831).
 */
export function unguardedLangStrings(source) {
  const bad = []
  const stack = []
  const lines = source.split(/\r?\n/)
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i]
    const frame = conditionalFrame(line)
    if (frame) {
      stack.push(frame)
      continue
    }
    if (/^\s*!else\b/.test(line)) {
      const top = stack[stack.length - 1]
      if (top) top.symbols = new Set()
      continue
    }
    if (/^\s*!endif\b/.test(line)) {
      stack.pop()
      continue
    }
    const symbol = langStringSymbol(line)
    if (symbol === null) continue
    const guarded = stack.some((f) => f.kind === 'ifdef' && f.symbols.has(`LANG_${symbol}`))
    if (!guarded) bad.push({ line: i + 1, symbol })
  }
  return bad
}

/**
 * Build-time assertion (#831): throw when `source` contains a LangString that
 * is not inside a matching `!ifdef LANG_<NAME>` guard.
 *
 * The stock NSIS template inserts `customHeader` right after
 * `!insertmacro addLangs`, so the `${LANG_*}` symbols defined by
 * `LoadLanguageFile` are visible there. A single-language installer
 * (`installerLanguages: [language]`, as the W-16 smoke test builds) loads only
 * that language, and NSIS leaves an undefined `${SYMBOL}` as literal text, so
 * an unguarded `LangString ... ${LANG_SIMPCHINESE}` makes makensis emit warning
 * 7025 — fatal, because electron-builder runs makensis with warnings-as-errors.
 * This turns that into an early, explicit build failure.
 */
export function validateLangStringGuards(source, filename) {
  const bad = unguardedLangStrings(source)
  if (bad.length > 0) {
    const where = bad.map((b) => `line ${b.line}: ${b.symbol}`).join(', ')
    throw new Error(`${filename}: LangString not guarded by !ifdef LANG_<NAME> (${where})`)
  }
}

/**
 * Blank out NSIS `;` line comments (NSIS has no block comments). A `;` inside a
 * double-quoted string is not a comment, so the scan has to follow the string
 * state, including the one escape that can carry a quote (#901).
 *
 * Measured on makensis 3.0.4.1: `"a$\"b"` stores `a"b`, so `$\"` is an escaped
 * quote and does not end the string; `"a$"b"` is a parse error ("WriteRegStr
 * expects 4 parameters, got 5"), so a bare `$"` is no escape at all — that `"`
 * closes the string. Treating `$"` as an escape and `$\"` as ordinary
 * characters made the scan disagree with the compiler on both forms.
 */
export function stripNsisComments(source) {
  return source
    .split(/\r?\n/)
    .map((line) => {
      let inQuote = false
      let out = ''
      for (let i = 0; i < line.length; i += 1) {
        const ch = line[i]
        if (inQuote && ch === '$' && line[i + 1] === '\\' && line[i + 2] === '"') {
          out += '$\\"'
          i += 2
        } else if (ch === '"') {
          inQuote = !inQuote
          out += ch
        } else if (!inQuote && ch === ';') {
          break
        } else if (!inQuote && ch === '#' && (out.trim() === '' || /\s$/.test(out))) {
          // NSIS also accepts `#` comments — at the start of a line or after a
          // complete statement: measured on makensis 3.0.4.1, `DetailPrint "x"
          // # trailing` compiles while `DetailPrint "x" stray` fails with
          // "Error in script", so the `#` really starts a comment and the rest
          // of the line is not a parameter. A `#` inside a string is text; one
          // right after a non-space token is left alone, because guessing there
          // could eat code — leaving it can only over-report (fail-closed).
          break
        } else {
          out += ch
        }
      }
      return out
    })
    .join('\n')
}

/**
 * Every `\\?` in code (comments ignored) that does not carry the separator
 * backslash of a complete long-path prefix, as `{ line, text }` (#901 / R1).
 *
 * NSIS strings have no backslash escapes, so `"\\?$dir"` builds `\\?C:\...`:
 * Win32 requires the separator right after the question mark, so that path is
 * unresolvable and every delete/rename naming it fails. That typo came with the
 * atomic-swap protocol in #361 and made the delete of the previous version fail
 * on every update; #901 / R1 corrected it and added the guards below.
 */
export function malformedLongPathPrefixes(source) {
  const bad = []
  const lines = stripNsisComments(source).split(/\r?\n/)
  for (let i = 0; i < lines.length; i += 1) {
    const pattern = /\\\\\?/g
    let match = pattern.exec(lines[i])
    while (match !== null) {
      if (lines[i][match.index + match[0].length] !== '\\') {
        bad.push({ line: i + 1, text: lines[i].trim() })
      }
      match = pattern.exec(lines[i])
    }
  }
  return bad
}

/** Build-time assertion for `malformedLongPathPrefixes` (#901 / R1). */
export function validateLongPathPrefixes(source, filename) {
  const bad = malformedLongPathPrefixes(source)
  if (bad.length > 0) {
    const where = bad.map((b) => `line ${b.line}: ${b.text}`).join('; ')
    throw new Error(
      `${filename}: "\\\\?" is not a complete long-path prefix (expected "\\\\?\\") — ${where}`,
    )
  }
}

/**
 * Problems with the `$iaBackupDirectory` delete site of the atomic-swap
 * protocol, as `{ line, what }` (#901 / R1):
 *
 *   - `ClearErrors` has to precede the delete, in the same block: the NSIS error
 *     flag is sticky, so a test without a clear can read an unrelated earlier
 *     failure, and a clear call in the previous function or in a macro
 *     definition does not clear this path;
 *   - exactly one `${Errors}` read may sit between the delete and
 *     `iaClearBackupDir`. LogicLib's `${Errors}` is `IfErrors`, which consumes
 *     the flag: measured on NSIS 3.0.4.1, the first read after a delete that
 *     fails because a child directory denies DELETE reports the error and an
 *     immediate second read reports none, while an intervening `StrCpy` does
 *     not clear it. A second read therefore turns the failure branch into dead
 *     code;
 *   - the failure has to be recorded as `IaLeftoverDir`, in `HKCU` under
 *     `${INSTALL_REGISTRY_KEY}` — the key the reader reads back — and carrying
 *     `$iaBackupDirectory`: a record naming anything else, or written where the
 *     next promote will not look, hides the leftover it was written for. Without
 *     a record at all a failed delete cleared the pointer that named the
 *     previous version and the directory stayed on disk with nothing referencing
 *     it (R1: ~0.7 GB per update, 3.50 GiB measured on one machine), and a silent
 *     install has no UI and writes no NSIS detail log to read it back from.
 *
 * The check window ends at `iaClearBackupDir`, at the enclosing `FunctionEnd`,
 * or at the enclosing `!macroend`, whichever comes first, and a delete without a
 * following `iaClearBackupDir` is itself a problem: bounding the window by the
 * clear call alone made the guard fail open — with the call removed, an
 * unrelated `${Errors}` read further down the file satisfied the check for this
 * site, and a delete inside a macro had no enclosing `FunctionEnd` to stop at.
 *
 * Two shapes are problems on their own, because text that satisfies the checks
 * is not the same as code that runs them (both verified against this guard):
 *
 *   - **conditional compilation** — either a directive anywhere in the delete's
 *     enclosing block (`Function` … `FunctionEnd` or `!macro` … `!macroend`), or
 *     a net-open `!if…` above the delete with its `!endif` further down. The
 *     block scan exists because the common wrapping form puts the `!ifdef` above
 *     the delete and the `!endif` after the clear; the open-above scan exists
 *     because a wrapper around the whole Function is indistinguishable from a
 *     branch that drops it (the shipped file wraps `.onGUIEnd` in
 *     `!ifndef BUILD_UNINSTALLER`). Directives are case-insensitive, so this
 *     scan is; the open-above case is deliberately fail-closed — an intentional
 *     wrapper around the guarded block has to be noted here;
 *   - **LogicLib nesting**, measured from the start of the enclosing block: the
 *     delete may sit at most one branch deep (the "new files are in place"
 *     guard), because a branch above it skips the delete and every check with
 *     it — and relative depths alone cannot see a wrapper around the whole
 *     guarded body, which moves delete, checks and clear down together — and
 *     `iaClearBackupDir` has to be at the delete's own depth, or a path skips
 *     the clear and a finished install still reads as incomplete.
 *
 * Fail-closed: a file with no long-path delete of `$iaBackupDirectory` at all
 * is reported as a problem, so removing the delete site cannot pass silently.
 * The site match is case-insensitive — NSIS directives are — so a re-cased
 * `rmdir /r` is recognized rather than reported as a missing delete.
 *
 * Limits of this scan (it reads one file's text, not compiled code): `${Errors}`
 * is counted wherever it appears, including inside a string literal; the delete
 * argument, the record key and the record value are matched in the shipped
 * spelling (double quotes, `HKCU "${INSTALL_REGISTRY_KEY}"`, whole
 * `$iaBackupDirectory`), so a single-quote or backtick argument is reported as a
 * missing delete and a re-cased or concatenated record as not recorded — both
 * fail closed (reported, not silently accepted). A delete sitting in a macro
 * that nothing inserts, or in an `!include`d file, is invisible here; the
 * follow-up issue for #901 tracks that along with the other text-scan limits.
 */
// LogicLib 2.6's block macros. The closers matter as much as the openers: the
// do-loop family closes with `${Loop}`, `${LoopWhile}` or `${LoopUntil}` and
// `${Unless}` with `${EndUnless}` — leaving those out read a correctly closed
// loop as nesting that is not there (measured: `${Do} … ${LoopUntil}` around a
// top-level statement reported "1 LogicLib level(s) deeper").
const OPENS_BLOCK = /\$\{(If|IfNot|Unless|While|Do|DoWhile|DoUntil|For|ForEach|Select|Switch)\}/g
const CLOSES_BLOCK =
  /\$\{(EndIf|EndWhile|EndUnless|Loop|LoopWhile|LoopUntil|Next|EndSelect|EndSwitch)\}/g
// Directives are case-insensitive (`!IFDEF` compiles — measured on 3.0.4.1), so
// the scan is too.
const CONDITIONAL = /^\s*!(if\b|ifdef\b|ifndef\b|ifmacrodef\b|ifmacrondef\b|else\b|elseif\b|endif\b)/i
const OPENS_FUNCTION = /^\s*(Function|!macro)\b/i
const CLOSES_FUNCTION = /^\s*(FunctionEnd|!macroend)\b/i

/** Net LogicLib block nesting over `lines` (openers minus closers). */
function logicLibDepth(lines) {
  let depth = 0
  for (const line of lines) {
    depth += (line.match(OPENS_BLOCK) ?? []).length
    depth -= (line.match(CLOSES_BLOCK) ?? []).length
  }
  return depth
}

/**
 * Conditionals still open at `index` — `!if…` above it with no `!endif` yet.
 *
 * A `!ifdef` above the delete that closes after it (or above the enclosing
 * Function and closes after the FunctionEnd) makes the whole guarded block
 * conditional, and a block-scoped scan cannot see the opener. `${!else}` keeps
 * the enclosing level open, so it is not counted.
 */
function openConditionals(lines, index) {
  let open = 0
  for (let i = 0; i < index; i += 1) {
    if (/^\s*!(if|ifdef|ifndef|ifmacrodef|ifmacrondef)\b/i.test(lines[i])) open += 1
    else if (/^\s*!endif\b/i.test(lines[i])) open -= 1
  }
  return Math.max(open, 0)
}

export function unguardedBackupDelete(source) {
  const problems = []
  const lines = stripNsisComments(source).split(/\r?\n/)
  const deleteLine = /RMDir\s+\/r\s+"\\\\\?\\\$iaBackupDirectory"/i
  const errorsMacro = '${Errors}'
  let sites = 0
  for (let i = 0; i < lines.length; i += 1) {
    if (!deleteLine.test(lines[i])) continue
    sites += 1
    const blockStart = (() => {
      for (let j = i - 1; j >= 0; j -= 1) {
        if (OPENS_FUNCTION.test(lines[j])) return j
      }
      return 0
    })()
    const blockEnd = (() => {
      for (let j = i + 1; j < lines.length; j += 1) {
        if (CLOSES_FUNCTION.test(lines[j])) return j
      }
      return lines.length - 1
    })()
    // `ClearErrors` has to be on the delete's own path: a clear call in the
    // previous function or in a macro definition does not clear this one.
    const before = lines
      .slice(blockStart, i)
      .filter((line) => line.trim() !== '')
      .slice(-3)
    if (!before.some((line) => /^\s*ClearErrors\b/.test(line))) {
      problems.push({ line: i + 1, what: 'missing ClearErrors before the delete' })
    }
    const rest = lines.slice(i + 1)
    const enclosing = [
      rest.findIndex((line) => /^\s*FunctionEnd\b/i.test(line)),
      rest.findIndex((line) => /^\s*!macroend\b/i.test(line)),
    ].filter((index) => index !== -1)
    const limit = enclosing.length === 0 ? -1 : Math.min(...enclosing)
    const clearIndex = rest.findIndex((line) => /!insertmacro\s+iaClearBackupDir\b/i.test(line))
    if (clearIndex === -1 || (limit !== -1 && limit < clearIndex)) {
      problems.push({ line: i + 1, what: 'no !insertmacro iaClearBackupDir after the delete' })
    }
    const bounds = [...enclosing, clearIndex].filter((index) => index !== -1)
    const window = bounds.length === 0 ? rest : rest.slice(0, Math.min(...bounds))
    const reads = window.filter((line) => line.includes(errorsMacro)).length
    const firstRead = window.findIndex((line) => line.includes(errorsMacro))
    if (reads === 0) {
      problems.push({ line: i + 1, what: `missing ${errorsMacro} check before iaClearBackupDir` })
    } else if (reads > 1) {
      problems.push({
        line: i + 1,
        what: `${reads} ${errorsMacro} reads between the delete and iaClearBackupDir — only the first can see the failed delete`,
      })
    }
    if (firstRead !== -1 && window.slice(0, firstRead).some((line) => /^\s*ClearErrors\b/.test(line))) {
      problems.push({
        line: i + 1,
        what: `ClearErrors between the delete and the ${errorsMacro} read discards the failed delete`,
      })
    }
    // The record has to land where the reader looks (HKCU, this app's own key)
    // and carry the whole directory, not a name built from it: a record of
    // "$iaBackupDirectory-tmp" or one written to another hive names something
    // the next promote will never find — the R1 orphan again.
    if (
      !window.some((line) =>
        /WriteRegStr\s+HKCU\s+"\$\{INSTALL_REGISTRY_KEY\}"\s+"IaLeftoverDir"\s+"?\$iaBackupDirectory"?\s*$/.test(
          line,
        ),
      )
    ) {
      problems.push({
        line: i + 1,
        what: 'failed delete is not recorded (no IaLeftoverDir write of $iaBackupDirectory)',
      })
    }
    // Conditional compilation above the delete (net-open) or anywhere in its
    // block: text that satisfies every check above is worth nothing if it never
    // compiles. The above-the-delete form is the one a block-scoped scan cannot
    // see, and the shipped file's own idiom for `.onGUIEnd` — a `!ifndef` above
    // a Function — is indistinguishable from a branch that drops it, so this is
    // deliberately fail-closed: an intentional wrapper has to be recorded here.
    const openAbove = openConditionals(lines, i)
    if (openAbove > 0) {
      problems.push({
        line: i + 1,
        what: `the delete sits inside ${openAbove} open conditional compilation directive(s) — the delete and its checks can be compiled out`,
      })
    } else if (lines.slice(blockStart, blockEnd + 1).some((line) => CONDITIONAL.test(line))) {
      problems.push({
        line: i + 1,
        what:
          "conditional compilation in the delete's enclosing block — the delete and its checks can be compiled out",
      })
    }
    // LogicLib nesting, measured from the start of the enclosing block: the
    // delete has to sit on the promote path itself (one branch deep at most —
    // the "new files are in place" guard), because a branch above it can skip
    // the delete and every check with it, and relative depths alone cannot see
    // that (a wrapper around the whole guarded body moves delete, checks and
    // clear down together). The clear call then has to be at the delete's own
    // depth, or a path skips it.
    const deleteDepth = logicLibDepth(lines.slice(blockStart, i))
    if (deleteDepth > 1) {
      problems.push({
        line: i + 1,
        what: `the delete sits ${deleteDepth} LogicLib level(s) inside the block — a branch above it can skip the delete and its checks`,
      })
    }
    if (clearIndex !== -1) {
      const clearDepth = logicLibDepth(lines.slice(blockStart, i + 1 + clearIndex))
      if (clearDepth > deleteDepth) {
        problems.push({
          line: i + 1,
          what: `iaClearBackupDir sits ${clearDepth - deleteDepth} LogicLib level(s) deeper than the delete — a path skips the clear`,
        })
      }
    }
  }
  if (sites === 0) {
    problems.push({ line: 0, what: 'no long-path RMDir of $iaBackupDirectory found' })
  }
  return problems
}

/** Build-time assertion for `unguardedBackupDelete` (#901 / R1). */
export function validateBackupDeleteGuards(source, filename) {
  const problems = unguardedBackupDelete(source)
  if (problems.length > 0) {
    const where = problems
      .map((p) => (p.line === 0 ? p.what : `line ${p.line}: ${p.what}`))
      .join('; ')
    throw new Error(`${filename}: unguarded backup delete — ${where}`)
  }
}

/**
 * Validate every NSIS script that runs inside `customHeader`: installer.nsh and
 * the installer-directories.nsh it `!include`s. Both are compiled after
 * addLangs, so both share the #831 hazard; the #901 long-path and backup-delete
 * guards apply to both files, while the delete site itself only exists in
 * installer-directories.nsh.
 */
export function validateInstallerScripts(installerDir) {
  for (const file of ['installer.nsh', 'installer-directories.nsh']) {
    const source = readFileSync(join(installerDir, file), 'utf8')
    validateLangStringGuards(source, file)
    validateLongPathPrefixes(source, file)
  }
  validateBackupDeleteGuards(
    readFileSync(join(installerDir, 'installer-directories.nsh'), 'utf8'),
    'installer-directories.nsh',
  )
}


export function createWindowsInstallerConfig({ version, appId, productName, installerDir, runtimeProduct, nodeVersion }) {
  const productFilename = productName.replace(/ /g, '-')
  return {
    appId,
    productName,
    copyright: 'Copyright © 2026 intelligence-agent contributors',
    extraMetadata: { version },
    directories: {
      app: '.',
      output: 'dist-installer',
      buildResources: 'installer',
    },
    // Spawned files (python child, scripts) must be real files on disk.
    asar: false,
    npmRebuild: false,
    files: ['dist/**/*', 'package.json'],
    // Bundled offline Python runtime (pinned by python-runtime.lock.json).
    // `from` is relative to the project dir (desktop/).
    extraResources: [
      { from: `${installerDir}/staging/python/`, to: 'python/', filter: ['**/*'] },
      // W-21 D5 (#817): the terminal client's runtime. The app's own Electron
      // binary cannot host a raw-mode TUI (measured: no TTY in node mode), so
      // the artifact carries its own node.exe (node-runtime.lock.json).
      { from: `${installerDir}/staging/node/`, to: 'node/', filter: ['**/*'] },
      // W-21 D3 (#815): the built renderer UI; the shell tells the service where
      // it is (WEB_DIST_DIR) and loads its window from the service origin.
      { from: '../web/dist/', to: 'web/', filter: ['**/*'] },
      // W-21 D5 (#817): the terminal client (compiled dist + runtime deps).
      // Only the runtime closure is shipped — the TUI's devDependencies
      // (typescript, @types) stay out; afterPack asserts the closure anyway.
      { from: '../tui/dist/', to: 'tui/dist/', filter: ['**/*'] },
      {
        from: '../tui/node_modules/',
        to: 'tui/node_modules/',
        filter: ['@earendil-works/**', 'get-east-asian-width/**', 'marked/**'],
      },
    ],
    // W-21 D5 (#817): the TUI entry, at the install root next to the app exe.
    extraFiles: [{ from: `${installerDir}/${TUI_LAUNCHER}`, to: TUI_LAUNCHER }],
    win: {
      target: [{ target: 'nsis', arch: ['x64'] }],
    },
    nsis: {
      oneClick: false,
      perMachine: false,
      allowToChangeInstallationDirectory: true,
      createDesktopShortcut: true,
      createStartMenuShortcut: true,
      shortcutName: productName,
      // Atomic directory swap + rollback, user-data preservation.
      include: `${installerDir}/installer.nsh`,
      // Never silently delete user data on uninstall; explicit cleanup is
      // scripts/clean-user-data.mjs (#361).
      deleteAppDataOnUninstall: false,
      artifactName: `${productFilename}-Setup-\${version}.\${ext}`,
      installerLanguages: ['en_US', 'zh_CN'],
    },
    afterPack: async (context) => {
      const resourcesDir =
        context.electronPlatformName === 'win32'
          ? join(context.appOutDir, 'resources')
          : join(context.appOutDir, `${context.packager.appInfo.productFilename}.app`, 'Contents', 'Resources')
      const pythonExe = join(resourcesDir, 'python', 'python.exe')
      if (!existsSync(pythonExe)) {
        throw new Error(
          `bundled python runtime missing: ${pythonExe} — prepare it per installer/README.md`,
        )
      }
      // W-21 D2: a staged runtime holding only the dependency closure imports
      // nothing; fail the build instead of shipping an installer that cannot start.
      assertRuntimeProduct({ pythonExe, product: runtimeProduct })
      // W-21 D5: the terminal client runs on the bundled node runtime.
      assertNodeRuntime({ nodeExe: join(resourcesDir, 'node', 'node.exe'), version: nodeVersion })
      // W-21 D3: the window loads the packaged renderer build from the service.
      assertBundledWebAssets({ resourcesDir })
      // W-21 D5: the terminal client must run from the artifact (entry +
      // launcher + the whole dependency closure of its one runtime dependency).
      assertTuiRuntimeClosure({ resourcesDir, appOutDir: context.appOutDir })
    },
  }
}

function loadLockfile(name = 'python-runtime.lock.json') {
  const path = join(installerDir, name)
  if (!existsSync(path)) fail(`not found: ${path}`)
  return JSON.parse(readFileSync(path, 'utf8'))
}

async function main() {
  const args = process.argv.slice(2)
  const compileOnly = args.includes('--compile-only')
  const outIndex = args.indexOf('--out')
  const outDir = outIndex >= 0 ? resolve(args[outIndex + 1]) : join(desktopDir, 'dist-installer')

  const lock = loadLockfile()
  validateRuntimeLockfile(lock)
  console.log(
    'lockfile OK:',
    lock.python.implementation,
    lock.python.version,
    `(${lock.wheels.length} pinned wheels)`,
    `+ product ${lock.product.name}==${lock.product.version}`,
  )
  const nodeLock = loadLockfile('node-runtime.lock.json')
  validateNodeRuntimeLockfile(nodeLock)
  console.log('node lockfile OK:', nodeLock.node.version, `(${nodeLock.layout.executable})`)

  const pkg = JSON.parse(readFileSync(join(desktopDir, 'package.json'), 'utf8'))
  const config = createWindowsInstallerConfig({
    version: pkg.version,
    appId: 'com.intelligence-agent.desktop',
    productName: 'Intelligence Agent',
    installerDir,
    runtimeProduct: lock.product,
    nodeVersion: nodeLock.node.version,
  })
  config.directories.output = outDir

  for (const required of [
    'installer.nsh',
    'installer-directories.nsh',
    'python-runtime.lock.json',
    'node-runtime.lock.json',
    TUI_LAUNCHER,
  ]) {
    if (!existsSync(join(installerDir, required))) {
      throw new Error(`missing installer input: ${required}`)
    }
  }
  const installerNshSource = readFileSync(join(installerDir, 'installer.nsh'), 'utf8')
  assertNsisIncludePlacement(installerNshSource, 'installer-directories.nsh')
  assertNsisLangStringGuards(installerNshSource)
  // #831: fail early when a LangString is not guarded by its language.
  validateInstallerScripts(installerDir)
  console.log('installer LangString guards OK')
  console.log('installer inputs OK')

  if (compileOnly) {
    console.log('compile-only: config valid, NSIS includes present')
    return
  }
  if (process.platform !== 'win32' || process.arch !== 'x64') {
    throw new Error('full installer build requires Windows x64 (use --compile-only elsewhere)')
  }
  // Fail before electron-builder spends minutes on a package it cannot complete.
  const webDistIndex = join(desktopDir, '..', 'web', 'dist', 'index.html')
  if (!existsSync(webDistIndex)) {
    throw new Error(`renderer build missing: ${webDistIndex} — run \`npm run build\` in web/`)
  }
  // W-21 D5: same for the terminal client (compiled from tui/src by tsc), plus
  // the staleness guard — a stale dist ships an old TUI in a green build.
  const tuiEntry = join(desktopDir, '..', 'tui', 'dist', 'src', 'index.js')
  if (!existsSync(tuiEntry)) {
    throw new Error(`TUI build missing: ${tuiEntry} — run \`npm run build\` in tui/`)
  }
  assertFreshBuild({
    entry: tuiEntry,
    newestSourceMtimeMs: newestMtimeMs(join(desktopDir, '..', 'tui', 'src')),
    entryMtimeMs: statSync(tuiEntry).mtimeMs,
  })
  // W-21 D10 (#836): …and for the desktop shell itself, which this build packages
  // verbatim from dist/. Without this the artifact can carry the previous shell
  // while every other check passes (observed in the D8 regression run).
  const shellEntry = join(desktopDir, 'dist', 'src', 'main.js')
  if (!existsSync(shellEntry)) {
    throw new Error(`desktop shell build missing: ${shellEntry} — run \`npm run build\` in desktop/`)
  }
  assertDesktopBuildFresh({ entry: shellEntry, sourceDir: join(desktopDir, 'src') })
  // …and for the staged runtime the TUI runs on.
  const stagedNode = join(installerDir, 'staging', nodeLock.layout.executable)
  if (!existsSync(stagedNode)) {
    throw new Error(
      `bundled node runtime not staged: ${stagedNode} — ` +
        'run `python scripts/prepare_node_runtime.py` (installer/README.md)',
    )
  }
  // W-21 D9 (#835): the staged Python runtime must carry THIS checkout's product
  // code. The version pin is blind to a lagging copy (observed: the installer
  // shipped a pre-D5 web/app.py, so the packaged service served no renderer).
  const stagedPython = join(installerDir, 'staging', lock.layout.executable)
  if (!existsSync(stagedPython)) {
    throw new Error(
      `bundled python runtime not staged: ${stagedPython} — ` +
        'run `python scripts/prepare_python_runtime.py` (installer/README.md)',
    )
  }
  assertStagedProductMatchesSource({
    sourceDir: join(repoDir, 'src', lock.product.module),
    stagedDir: stagedProductDir({ pythonExe: stagedPython, module: lock.product.module }),
  })
  console.log('staged product matches the checkout')
  let builder
  try {
    builder = await import('electron-builder')
  } catch {
    throw new Error('electron-builder is not installed: run `npm i -D electron-builder` in desktop/')
  }
  const { Platform, Arch } = builder
  await builder.build({
    projectDir: desktopDir,
    targets: Platform.WINDOWS.createTarget(['nsis'], Arch.x64),
    publish: 'never',
    config,
  })

  // SHA-256 manifest over the produced artifacts (#361: unsigned but hashed).
  const manifest = []
  for (const file of readdirSync(outDir)) {
    if (!file.endsWith('.exe') && !file.endsWith('.nsh')) continue
    manifest.push(`${sha256File(join(outDir, file))}  ${file}`)
  }
  writeFileSync(join(outDir, 'SHA256SUMS.txt'), `${manifest.join('\n')}\n`)
  const buildInfo = {
    version: pkg.version,
    appId: config.appId,
    platform: 'win32',
    arch: 'x64',
    lockfileSha256: sha256File(join(installerDir, 'python-runtime.lock.json')),
    nodeLockfileSha256: sha256File(join(installerDir, 'node-runtime.lock.json')),
    // W-21 D9 (#835): the product code this artifact was built from — the gate
    // evidence names it instead of trusting a version string.
    productCodeSha256: productCodeDigest(join(repoDir, 'src', lock.product.module)),
    builtAt: new Date().toISOString(),
  }
  writeFileSync(join(outDir, 'installer-build.json'), `${JSON.stringify(buildInfo, null, 2)}\n`)
  console.log(`artifacts in ${outDir}`)
}

const isMainModule =
  process.argv[1] !== undefined && import.meta.url === pathToFileURL(process.argv[1]).href
if (isMainModule) {
  main().catch((error) => {
    console.error(error instanceof Error ? error.message : error)
    process.exit(1)
  })
}
