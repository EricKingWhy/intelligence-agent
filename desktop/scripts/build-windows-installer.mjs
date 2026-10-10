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
  requirements: { engines: '>=22.1', minimumMajor: 22 },
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
  const [majorText, minorText] = String(node.version ?? '').split('.')
  const major = Number(majorText)
  if (!Number.isInteger(major)) failNode(`bad node.version ${String(node.version)}`)
  const minimum = lock.requirements?.minimumMajor
  if (!Number.isInteger(minimum)) failNode('requirements.minimumMajor must be an integer')
  if (major < minimum) {
    failNode(`node ${node.version} is below requirements.minimumMajor ${String(minimum)} (tui engines)`)
  }
  // The integer minimum is only the mirror of the TUI floor; the floor itself
  // carries a minor (`>=22.1`), and a pin like 22.0.x satisfies the major check
  // while still missing the runtime the client needs (#919 Q11). Parse the
  // floor and compare the tuple: below the floor's major fails outright, at it
  // the minor decides. #919 review (S4): the pattern is anchored so a trailing
  // patch or junk cannot ride along — `>=22.1.5` against a `22.1.0` pin was
  // accepted, and `>=22x` parsed as `>=22` though semver rejects it.
  const engines = lock.requirements?.engines
  const floor = /^\s*>=\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?\s*$/.exec(String(engines ?? ''))
  if (floor === null) {
    failNode(`requirements.engines must be a ">=<major>[.<minor>[.<patch>]]" floor, got ${JSON.stringify(engines)}`)
  }
  const floorMajor = Number(floor[1])
  const floorMinor = floor[2] === undefined ? 0 : Number(floor[2])
  const floorPatch = floor[3] === undefined ? 0 : Number(floor[3])
  if (major < floorMajor) {
    failNode(`node ${node.version} is below requirements.engines ${JSON.stringify(engines)} (tui engines)`)
  }
  if (major === floorMajor) {
    const minor = Number(minorText)
    if (!Number.isInteger(minor)) {
      failNode(
        `node.version ${String(node.version)} has no minor to hold the requirements.engines floor ${JSON.stringify(engines)}`,
      )
    }
    const [patchText] = String(node.version ?? '').split('.').slice(2)
    const patch = patchText === undefined ? 0 : Number(patchText)
    if (!Number.isInteger(patch)) {
      failNode(`node.version ${String(node.version)} has a non-numeric patch`)
    }
    if (minor < floorMinor || (minor === floorMinor && patch < floorPatch)) {
      failNode(`node ${node.version} is below requirements.engines ${JSON.stringify(engines)} (tui engines)`)
    }
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
      // NSIS's three string forms and its comment markers, as measured on
      // makensis 3.0.4.1 (#905 / R7): `"`, `'` and `` ` `` all delimit strings
      // (`RMDir /r '<path>;x'` compiles and deletes a name containing the `;`),
      // `$\<quote>` inside a string is an escape that does not close it
      // (`$\'` stores a literal `'`), and `;`/`#` start a comment only outside
      // one. A `#` glued to a closing quote starts a comment too (measured:
      // `DetailPrint "a"# trailing` compiles, and `StrCpy $0 "a"#$1` stores
      // `a`), while one glued to a non-quote token is text (`StrCpy $0 foo#bar`
      // stores `foo#bar`) — leaving that case alone can only over-report, which
      // is the fail-closed direction.
      let quote = ''
      let out = ''
      for (let i = 0; i < line.length; i += 1) {
        const ch = line[i]
        if (quote !== '' && ch === '$' && line[i + 1] === '\\' && line[i + 2] === quote) {
          out += `$\\${quote}`
          i += 2
        } else if (ch === '"' || ch === "'" || ch === '`') {
          if (quote === '') quote = ch
          else if (quote === ch) quote = ''
          out += ch
        } else if (quote === '' && ch === ';') {
          break
        } else if (
          quote === '' &&
          ch === '#' &&
          (out.trim() === '' || /\s$/.test(out) || /["'`]$/.test(out))
        ) {
          // NSIS also accepts `#` comments — at the start of a line, after a
          // complete statement, or glued to the closing quote of one (measured:
          // `DetailPrint "x" # trailing` compiles while `DetailPrint "x" stray`
          // fails with "Error in script", so the `#` really starts a comment and
          // the rest of the line is not a parameter).
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
 * `line` with the contents of every string replaced by spaces, so a scan that
 * should read code only does not match text inside a message.
 *
 * The string rules are the measured ones `stripNsisComments` uses — all three
 * quote forms, and `$\<quote>` inside a string is an escape that does not close
 * it — and comments are already gone where this is used, so only the string
 * bodies are blanked (#919 Q4: `DetailPrint "run: RMDir /r …"` was read as a
 * recursive delete statement).
 */
function outsideStrings(line) {
  let quote = ''
  let out = ''
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i]
    if (quote !== '' && ch === '$' && line[i + 1] === '\\' && line[i + 2] === quote) {
      out += '   '
      i += 2
    } else if (ch === '"' || ch === "'" || ch === '`') {
      if (quote === '') quote = ch
      else if (quote === ch) quote = ''
      out += ch
    } else {
      out += quote === '' ? ch : ' '
    }
  }
  return out
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
 * protocol, as `{ line, what }` (#901 / R1, extended for #905):
 *
 *   - `ClearErrors` has to precede the delete on the delete's own path: the NSIS
 *     error flag is sticky, so a test without a clear can read an unrelated
 *     earlier failure, and a clear call in the previous function, in a macro
 *     definition (round-5 R4), in a branch the delete is not on (R5), or on the
 *     other side of an `${Else}` does not clear this path either;
 *   - exactly one `${Errors}` read may sit between the delete and
 *     `iaClearBackupDir`, and it has to be a form the guard can name: a
 *     line-anchored `${If}`/`${IfNot}`/`${Unless}` condition carrying
 *     `${Errors}` — LogicLib expands the macro to `"" Errors ""`, its condition
 *     form — or a line-anchored raw `IfErrors`. Any other `${Errors}` inside the
 *     window is reported rather than counted: `${Errors}` in a string literal
 *     does not even compile (measured: "DetailPrint expects 1 parameters, got
 *     3"), and a usage the guard cannot recognize must not satisfy a rule about
 *     reads (round-5 item 1). LogicLib's `${Errors}` is `IfErrors`, which
 *     consumes the flag: measured on NSIS 3.0.4.1, the first read after a delete
 *     that fails because a child directory denies DELETE reports the error and
 *     an immediate second read reports none, while an intervening `StrCpy` does
 *     not clear it. A second read therefore turns the failure branch into dead
 *     code;
 *   - the failure has to be recorded as `IaLeftoverDir`, in `HKCU` under
 *     `${INSTALL_REGISTRY_KEY}` — the key the reader reads back — and carrying
 *     `$iaBackupDirectory`: a record naming anything else, or written where the
 *     next promote will not look, hides the leftover it was written for. Without
 *     a record at all a failed delete cleared the pointer that named the
 *     previous version and the directory stayed on disk with nothing referencing
 *     it (R1: ~0.7 GB per update, 3.50 GiB measured on one machine), and a silent
 *     install has no UI and writes no NSIS detail log to read it back from;
 *   - a delete inside a `!macro` body is only reported clean when the file also
 *     `!insertmacro`s that macro (round-5 item 4, fail-closed): a macro body runs
 *     at its call sites, not at its definition, and the window's own `!macroend`
 *     bound made such a delete look complete while nothing ran it.
 *
 * The check window ends at `iaClearBackupDir`, at the enclosing `FunctionEnd`,
 * or at the enclosing `!macroend`, whichever comes first, and a delete without a
 * following `iaClearBackupDir` is itself a problem: bounding the window by the
 * clear call alone made the guard fail open — with the call removed, an
 * unrelated `${Errors}` read further down the file satisfied the check for this
 * site, and a delete inside a macro had no enclosing `FunctionEnd` to stop at.
 * Statements inside a `!macro` body that does not contain the delete are
 * ignored — their definition site is not a call site (round-5 R4).
 *
 * Two shapes are problems on their own, because text that satisfies the checks
 * is not the same as code that runs them (all verified against this guard):
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
 *     `iaClearBackupDir` has to be at the delete's own depth and on the delete's
 *     side of every `${Else}`/`${ElseIf}`/`${Case}`/`${CaseElse}` divider, or a
 *     path skips the clear and a finished install still reads as incomplete
 *     (round-5 R2). Nesting counts every block macro of LogicLib 2.6 — the
 *     `${DoWhile}`/`${DoUntil}`/`${LoopWhile}` family included — and is
 *     case-insensitive, because define and macro names are: `${iF}` … `${eNdIf}`
 *     nests exactly as `${If}` … `${EndIf}` does (round-5 R1);
 *   - **constant-false branches**: an opener whose condition is a comparison of
 *     two literals that cannot hold (`${If} 1 == 0`, `${IfNot} 1 == 1`,
 *     `${Unless} 1 == 1`, integer or quoted operands) is reported when it covers
 *     the delete, its `${Errors}` read, its record or the clear call. This is the
 *     same bypass as `!ifdef NOPE`, in LogicLib's syntax — round-5 R3/R5 asked
 *     for this narrow reading, not for reachability analysis (see the limits
 *     below).
 *
 * Fail-closed: a file with no long-path delete of `$iaBackupDirectory` at all
 * is reported as a problem, so removing the delete site cannot pass silently.
 * The site match is case-insensitive — NSIS directives are — so a re-cased
 * `rmdir /r` is recognized rather than reported as a missing delete, and the
 * delete argument may use any of NSIS's three quote styles (all three compile
 * and delete — measured), because a single-quoted call is the same delete.
 *
 * Limits of this scan (it reads one file's text, not compiled code), each with
 * the construction it is known to let through — reported as a gap, not accepted
 * as safe:
 *
 *   - comments and strings follow the measured rules (makensis 3.0.4.1, see
 *     `stripNsisComments`): `"`/`'`/`` ` `` all delimit strings, `$\<quote>`
 *     inside a string is an escape, and `;`/`#` start a comment outside a
 *     string — including a `#` glued to a closing quote — while a `#` glued to
 *     a non-quote token is text (`StrCpy $0 foo#bar` stores `foo#bar`). A stray
 *     `#` can therefore only over-report;
 *   - the record and delete argument must be written as one statement on one
 *     line, with the `${INSTALL_REGISTRY_KEY}` define (any case) and the whole
 *     `$iaBackupDirectory` (any case) as the value. A record built by
 *     concatenation, or one written by a helper macro that takes the path as an
 *     argument, reads as "not recorded";
 *   - the constant-false rule decides only literal comparisons. A skip built
 *     from variables, `${FileExists}`, arithmetic or an unlisted condition is
 *     not analyzed at all — e.g. `${If} $0 != ""` … with `$0` known to the
 *     installer as empty. Catching that needs a evaluator, not a text scan;
 *   - the arming window (#919 Q2/Q3) is read as statements: every `StrCpy`
 *     whose output is `$iaDeleteCandidate` — bare or quoted: measured, `StrCpy
 *     "$vFlag" "0"` compiles and stores the 0 (probe r9), so the output's quote
 *     form is not the statement's meaning — above the prepare call is
 *     collected, and the flag is the last `StrCpy` to `$iaDeleteShapeCheck`
 *     carrying a `[01]` value, in any quote form. The optional `[maxlen]
 *     [startoffset]` operands are read because they decide what is stored
 *     (measured, probe r6: `"1" 1`, `"1" 2` and `"1" 1 0` store "1", while
 *     `"1" 0` and `"1" 2 1` store ""), so a maxlen of at least 1 with offset 0
 *     counts as armed, and maxlen 0, an offset past the value, or an operand
 *     this scan cannot read as a number counts as not armed (fail-closed: the
 *     truncating forms are reported, and the disposition review's R5 maxlen
 *     over-report is closed). More than one distinct readable source, or a copy
 *     whose value cannot be read, arms the requirement where the exemption for
 *     the rollback's own `$iaFinalDirectory` install would otherwise skip it —
 *     the name the delete runs with is not one value the window shows
 *     (fail-closed; the whole-window reading replaced "the textually last copy"
 *     in the fix round, which a branch-divergent layout had laundered). A value
 *     written in another instruction (`Pop`, `IntOp`, `ReadRegStr`) is not
 *     readable as the value it sets, so the guard counts it as "not armed". Any
 *     other statement in the window that uses either variable is reported, and
 *     a quoted span that is nothing but the variable counts as a use — a quoted
 *     output or `$var`-typed parameter is the same write one quote form away
 *     (N2) — while a mention inside a longer message string is not a write and
 *     stays unreported (the disposition review's N3/R4 negative). A `${define}`
 *     use whose body touches one of the variables, an `!insertmacro` of a macro
 *     that does (or that this text does not define), and an `!include` anywhere
 *     on the path are reported (the included text is not read here — round-5
 *     item 4; N4: this list used to omit the include report it had). The substitution set is this text's
 *     `!macro` bodies and its `!define` bodies with `\`-continued values joined
 *     — measured: a value continued with `\` compiles and the `${NAME}`
 *     insertion executes the statement (probe u4), so reading only the first
 *     fragment left the continuation's text unread (the review's N3). A
 *     `${NAME}` whose define lives in an `!include`d file resolves to nothing
 *     here and is invisible in the window. Nested tokens are resolved the way
 *     the preprocessor resolves them: `${${IA.SEL}}` uses the *value* of
 *     `IA.SEL` as the name (measured, probe r5: with `!define IA.DISARM "StrCpy
 *     $iaDeleteShapeCheck 0"` the token executed the disarm), and the braces are
 *     paired — the earlier token reading stopped at the first `}` and saw
 *     `${${IA.SEL`, a name no map holds, so the disarm counted as clean (the
 *     review's F-3). The outer quote pair of a define's value is stripped before
 *     the scan, so a `${define}` whose quoted value only *names* a variable in
 *     running text is reported too (over-report, fail-closed: keeping the quotes
 *     would hide a carried `!insertmacro`, which does expand). Dot- and
 *     dash-spelled define names are read like any other;
 *   - the declined-sweep branch must end in `Goto`/`Return`/`Abort`/`Quit`: the
 *     test runs LogicLib's own block trace over the branch's statements, so a
 *     trailing `${If}`/`${Else}`/`${EndIf}` whose every arm jumps out counts as
 *     ended (measured legal, and measured as a false report before — disposition
 *     review R6), while an arm that can fall through still counts as
 *     unterminated. A label declared inline (`iaSweepDeclined: SetErrorLevel 2`
 *     — measured legal in a Section and in a `!macro` body) is read with the
 *     statement on its line, and a label reference may be quoted (`Goto "L"`
 *     compiles and jumps — measured), so a quoted target is followed like a bare
 *     one. A label's name is read as the measured alphabet: a name starts with
 *     a letter, `_`, `.`, `%` or `@` and carries letters, digits, `_`, `.`,
 *     `+`, `!`, `%`, `#`, `@`, `$`, `-` or `:` (measured on 3.0.4.1 with
 *     declared variables: `.a:`, `a-b:`, `a+b:`, `a!b:`, `a%b:`, `a#b:`,
 *     `a@b:`, `a:b:`, `a$$b:`, `%ab:`, `@ab:` and `_ab:` all compile and every
 *     jump lands — probes n8/n9, the `%`/`@` starts and the colon form
 *     re-measured in this disposal round), while a *leading* digit, `$`, `!`
 *     or `+`/`-` is refused (`Goto targets cannot begin with 0-9, $, !`;
 *     `+`/`-` start a relative jump) and a `#` at line start is a comment. The
 *     old `[A-Za-z0-9_.]` class read `ia-hop-into-delete:`, `.iaHop:` and the
 *     other spellings as no declaration at all, so a declined branch jumping
 *     through such a hop into the delete pass went unreported where the
 *     plain-name twin reported (the fix round's own review, gap A-1, and the
 *     two-axis review's P2: the dot-start form was a regression against the
 *     base guard);
 *   - the declined branch's reach back into the delete pass is a text scan of
 *     label references: a label is "in the pass" when it sits above the delete
 *     or between the label a statement below the delete names and that
 *     statement, and a label whose own span (to the next label) names a label
 *     that reaches the pass counts as reaching it too. A reference built from a
 *     variable (`Goto $1`) is not read, and a jump to a label declared in an
 *     `!include`d file is not read either. A jump to a label this block does
 *     not declare is reported when the file declares that name elsewhere:
 *     sibling `!macro` bodies share the label namespace (measured: a cross-body
 *     `Goto` compiles and jumps — probe r4), so the target's own body can enter
 *     the delete pass unseen beyond this block's text (fail-closed);
 *   - the failed delete's path — the statements after the sweep's `${Errors}`
 *     read — must reach the kept accounting: a text walk follows bare `Goto`s
 *     through this block's label table and accepts the label the status gate
 *     jumps to (the refusal landing, where a kept candidate is counted) or a
 *     counter bump on the path. A target built from a variable (`Goto $1`), a
 *     quoted target, or a jump to a label an `!include`d file declares is not
 *     followed and reads as not reaching it (fail-closed). The gap this closes
 *     was measured: a `Goto iaSweepNext` between the failure-branch status write
 *     and the kept label kept the whole suite green while every failed delete
 *     went uncounted — no report, exit code left at 0. Which leg of the read
 *     is the failure path comes from the read's own form: `IfErrors 0 label`
 *     jumps when there is *no* error, so the fall-through is the failure path,
 *     while the one-argument `IfErrors label` jumps *on* error — there the
 *     walk follows the jump, and a target the block does not declare reports
 *     (fail-closed);
 *   - the rollback's restore-failure window is the `${Errors}` read after the
 *     last `Rename` and the branch it closes. A raw `IfErrors 0 label` jump has
 *     neither a name nor a closer the scan reads, so a rollback using that form
 *     is reported as "not measurable" even where its branch does record
 *     (fail-closed, disposition review R7); LogicLib's `${If} ${Errors}` is the
 *     measurable form;
 *   - a delete reached through `!include` is invisible here, and so is a macro
 *     `!insertmacro`d from an `!include`d file (the include boundary, round-5
 *     item 4): only the named installer scripts are scanned.
 */
// LogicLib 2.6's block macros. The closers matter as much as the openers: the
// do-loop family closes with `${Loop}`, `${LoopWhile}` or `${LoopUntil}` and
// `${Unless}` with `${EndUnless}` — leaving those out read a correctly closed
// loop as nesting that is not there (measured: `${Do} … ${LoopUntil}` around a
// top-level statement reported "1 LogicLib level(s) deeper"). The lists are
// exported as pattern sources so the test suite counts nesting with the same
// lists: its private copy had drifted — three closers and the case-insensitivity
// were missing, so test and guard disagreed about the same layout (#905 / R8).
export const LOGICLIB_OPENS =
  '\\$\\{(If|IfNot|Unless|While|Do|DoWhile|DoUntil|For|ForEach|Select|Switch)\\}'
export const LOGICLIB_CLOSES =
  '\\$\\{(EndIf|EndWhile|EndUnless|Loop|LoopWhile|LoopUntil|Next|EndSelect|EndSwitch)\\}'
// LogicLib 2.6.0 (`Include/LogicLib.nsh`): `${Else}`/`${ElseIf}` divide one
// `${If}`, `${Case}`/`${CaseElse}` divide one `${Switch}`. The two sides are
// different paths, so a delete on one side and its clear call on the other do
// not run together (round-5 R2).
const DIVIDES_BLOCK = /\$\{(Else|ElseIf|Case|CaseElse)\}/gi
// Directives, define names, variable names and macro names are all
// case-insensitive (measured on 3.0.4.1: `!IFDEF` compiles, `!define Foo`
// answers `!ifdef foo`, `$IABackupDirectory` resolves `Var iaBackupDirectory`),
// so the scans are too: `${iF}` opens a block exactly like `${If}`, and a
// case-sensitive scan goes blind to it (round-5 R1).
const OPENS_BLOCK = new RegExp(LOGICLIB_OPENS, 'gi')
const CLOSES_BLOCK = new RegExp(LOGICLIB_CLOSES, 'gi')
const CONDITIONAL = /^\s*!(if\b|ifdef\b|ifndef\b|ifmacrodef\b|ifmacrondef\b|else\b|elseif\b|endif\b)/i
const OPENS_FUNCTION = /^\s*(Function|!macro)\b/i
const CLOSES_FUNCTION = /^\s*(FunctionEnd|!macroend)\b/i
const INSERT_MACRO = /^\s*!insertmacro\s+(\S+)/i
// Comparisons of two literal integers, or of two quoted literals: the only
// conditions `constantConditionTruth` decides. LogicLib compares `==`/`=` by
// string (case-insensitively) and `<`/`>` numerically.
const INTEGER_COMPARISON = /^(-?\d+)\s*(==|!=|<>|<=|>=|<|>|=)\s*(-?\d+)$/
const QUOTED_COMPARISON = /^(['"`])(.*)\1\s*(==|!=|<>|=)\s*(['"`])(.*)\4$/

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
 * LogicLib block structure over `lines`: the openers in effect when each line
 * starts (`stackBefore`), every divider with the block it divides (`dividers`),
 * the line each opener closes on (`closeOf`), and the opener each closer
 * belongs to (`openOf`). Within a line the events are ordered by position, so
 * an opener and its closer on one line still cancel.
 */
function logicLibTrace(lines) {
  const stack = []
  const stackBefore = []
  const dividers = []
  const closeOf = new Map()
  const openOf = new Map()
  for (let k = 0; k < lines.length; k += 1) {
    stackBefore.push(stack.slice())
    const events = []
    for (const match of lines[k].matchAll(OPENS_BLOCK)) events.push({ at: match.index, kind: 'open' })
    for (const match of lines[k].matchAll(CLOSES_BLOCK)) events.push({ at: match.index, kind: 'close' })
    for (const match of lines[k].matchAll(DIVIDES_BLOCK)) events.push({ at: match.index, kind: 'divide' })
    events.sort((a, b) => a.at - b.at)
    for (const event of events) {
      if (event.kind === 'open') {
        stack.push(k)
      } else if (event.kind === 'close') {
        const opener = stack.pop()
        if (opener !== undefined) {
          closeOf.set(opener, k)
          openOf.set(k, opener)
        }
      } else {
        dividers.push({ line: k, opener: stack.length > 0 ? stack[stack.length - 1] : -1 })
      }
    }
  }
  return { stackBefore, dividers, closeOf, openOf }
}

/**
 * True when `inner` sits in the same branch chain as `outer`: every block open
 * at `inner` is also open at `outer`. A candidate that fails this is deeper than
 * the delete or outside a block the delete sits in, so it does not run on every
 * path that reaches the delete.
 */
function onSameBranch(trace, inner, outer) {
  return trace.stackBefore[inner].every((opener) => trace.stackBefore[outer].includes(opener))
}

/** `${Else}`-family dividers in `(from, to)` that split a block open at `from`. */
function branchDividersBetween(trace, from, to) {
  return trace.dividers.filter(
    (divider) =>
      divider.line > from && divider.line < to && trace.stackBefore[from].includes(divider.opener),
  )
}

/**
 * Line of the innermost `Function`/`!macro` still open at `index`, else 0.
 *
 * Scanning backwards for the nearest opener read a `!macro` whose `!macroend`
 * already closed above the delete as the enclosing block, and then measured the
 * delete against a macro body it is not in (round-5 item 4) — a closed block has
 * to be counted out.
 */
function enclosingBlockStart(lines, index) {
  const stack = []
  for (let j = 0; j < index; j += 1) {
    if (OPENS_FUNCTION.test(lines[j])) stack.push(j)
    else if (CLOSES_FUNCTION.test(lines[j])) stack.pop()
  }
  return stack.length === 0 ? 0 : stack[stack.length - 1]
}

/** `!macro` … `!macroend` body ranges, `{ start, end }`, both ends inclusive. */
function macroBodyRanges(lines) {
  const ranges = []
  let start = -1
  for (let k = 0; k < lines.length; k += 1) {
    if (start === -1 && /^\s*!macro\b/i.test(lines[k])) start = k
    else if (start !== -1 && /^\s*!macroend\b/i.test(lines[k])) {
      ranges.push({ start, end: k })
      start = -1
    }
  }
  if (start !== -1) ranges.push({ start, end: lines.length - 1 })
  return ranges
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

/**
 * The truth of a literal comparison, or `undefined` when the condition is not
 * decidable. Deliberately narrow (round-5 R3/R5): the guard reads text, so only
 * comparisons of two literals are decided — `${If} 1 == 0` and friends.
 */
function constantConditionTruth(condition) {
  const numbers = INTEGER_COMPARISON.exec(condition)
  if (numbers !== null) {
    const left = Number(numbers[1])
    const right = Number(numbers[3])
    if (numbers[2] === '==' || numbers[2] === '=') return left === right
    if (numbers[2] === '!=' || numbers[2] === '<>') return left !== right
    if (numbers[2] === '<') return left < right
    if (numbers[2] === '>') return left > right
    if (numbers[2] === '<=') return left <= right
    return left >= right
  }
  const quoted = QUOTED_COMPARISON.exec(condition)
  if (quoted === null) return undefined
  const left = quoted[2]
  const right = quoted[5]
  // A `$` means the text is a variable, not a literal: `"$a" == "$b"` is not
  // decidable here.
  if (left.includes('$') || right.includes('$')) return undefined
  if (quoted[3] === '==' || quoted[3] === '=') return left.toLowerCase() === right.toLowerCase()
  return left.toLowerCase() !== right.toLowerCase()
}

/**
 * The text of an opener on `line` whose condition can never hold, else
 * `undefined`. Only openers written the usual way — one per line, at the start,
 * with nothing but the condition after them — are decided; `${DoWhile}` and
 * `${DoUntil}` are left alone because their bodies run once regardless, and a
 * negated opener (`${IfNot}`, `${Unless}`) is dead when its condition HOLDS.
 */
function deadBranchText(line) {
  const opener = /^\s*\$\{(If|IfNot|Unless|While)\}\s+(.*)$/i.exec(line)
  if (opener === null) return undefined
  const truth = constantConditionTruth(opener[2].trim())
  if (truth === undefined) return undefined
  const negated = /^(IfNot|Unless)$/i.test(opener[1])
  return (negated ? !truth : truth) ? undefined : line.trim()
}

/**
 * The window of a rollback's restore-failure record: from the `${Errors}` read
 * that follows the last `Rename` to the end of that read's branch — its
 * `${Else}`-family divider or closing `${EndIf}` — as `{ from, to }`, or `null`
 * when the block has no rename, no such read, or the read never closes.
 *
 * The rollback policy (#904) wants the record for the restore failure: the
 * delete's own error is deliberately not read, and the rename is the operation
 * whose failure has to be visible. #919 Q7: the check was "some record in the
 * block", which the refused branch's own record satisfied — a rollback that
 * stopped recording the restore failure passed every check (measured). The
 * last `Rename` of the site is the restore (the rollback renames the backup
 * back, and nothing after it renames anything).
 */
function restoreFailureWindow(lines, trace, { start, end }, onPath, isRead) {
  let rename = -1
  for (let j = start; j <= end; j += 1) {
    if (onPath(j) && /^\s*Rename\b/i.test(lines[j])) rename = j
  }
  if (rename === -1) return null
  let read = -1
  for (let j = rename + 1; j <= end; j += 1) {
    if (onPath(j) && isRead(lines[j])) {
      read = j
      break
    }
  }
  if (read === -1) return null
  // #919 review (F2): the read must name its own end. The raw `IfErrors` form
  // has no `${EndIf}` to close it, and falling back to the site's end made the
  // window unbounded — the refused branch's record then satisfied the rule the
  // window exists for (measured). A window this function cannot read comes back
  // as `null`, and the caller gives that its own reason — "the read is not in a
  // form this scan can measure" — instead of reporting a missing record: a raw
  // jump whose branch does record would otherwise be called "does not record"
  // (the disposition review's N2/R7 measured exactly that; the direction is
  // fail-closed, the reason was not true).
  const close = trace.closeOf.get(read)
  if (close === undefined) return null
  const divide = trace.dividers.find((divider) => divider.opener === read)?.line ?? -1
  return { from: read + 1, to: divide === -1 ? close : Math.min(close, divide) }
}

/**
 * First `SetErrorLevel 2` on `onPath` in `(after, end]`, else -1 — the sweep's
 * "leftovers were kept" reading (#904 item 4), as a line index.
 *
 * #919 Q3: the scan used to take any match in the span, so a `SetErrorLevel 2`
 * inside another macro body — which only runs where that macro is inserted —
 * satisfied the rule. Returning the index lets the caller hand the hit to the
 * dead-branch scan, which asks whether the line can run at all.
 */
function keptExitLevel(lines, after, end, onPath, inline = '') {
  // A label may carry the exit code on its own line (`iaSweepDeclined:
  // SetErrorLevel 2`) — measured on 3.0.4.1, a label followed by its statement
  // is legal in a Section and in a `!macro` body (probe r1/r2, fix round F-1) —
  // so the label's own line is read before the lines below it.
  if (/^\s*SetErrorLevel\s+2\s*$/i.test(inline)) return after
  // A statement may sit on a label's own line anywhere in the span, not only on
  // the first label's: `iaSweepKept: SetErrorLevel 2` is the same kept reading
  // (measured, probes r1/r2), and the *last* label of a chain carries the
  // statement that follows the chain. Reading whole lines only reported those
  // spellings as a missing exit code (the fix round's review, F-4).
  const declaresInlineExit = (line) =>
    labelDeclarations(line).some((declaration) => /^\s*SetErrorLevel\s+2\s*$/i.test(declaration.rest))
  for (let j = after + 1; j <= end; j += 1) {
    if (!onPath(j)) continue
    if (/^\s*SetErrorLevel\s+2\s*$/i.test(lines[j]) || declaresInlineExit(lines[j])) return j
  }
  return -1
}

/**
 * The labels a line declares, in order, each with the statement text that
 * follows it on the same line (`{ name, rest }`, `rest` possibly empty), or an
 * empty list for a line that declares none.
 *
 * NSIS labels are positions: `Hop:` on its own line and `Hop: Goto Target` name
 * the same place, and a chain `L1: L2: Goto X` declares two names at one
 * position (all measured on 3.0.4.1 — Section, `!macro` body and loop head:
 * probes r1/r2/r8, all compiled and ran). The fix round's review (F-1) measured
 * what reading only the stand-alone spelling cost: an inline-declared hop was
 * invisible to the label table, so a declined `Goto` through it reached the
 * delete pass silently, and an inline `iaSweepDeclined: SetErrorLevel 2` read
 * as a declined branch that sets no exit code.
 */
function labelDeclarations(line) {
  const out = []
  let text = stripNsisComments(line)
  for (;;) {
    const match = /^\s*([A-Za-z_.%@][A-Za-z0-9_.+!%#@$:-]*)\s*:/.exec(text)
    if (match === null) return out
    out.push({ name: match[1].toLowerCase(), rest: text.slice(match[0].length) })
    text = text.slice(match[0].length)
  }
}

/** The first label a line declares, or `null`. */
function labelDeclaration(line) {
  return labelDeclarations(line)[0] ?? null
}

/**
 * Every token of a line that can name a label: the unquoted words and the
 * contents of every quoted span. `outsideStrings` blanks quoted contents, and a
 * jump target may be quoted — `Goto "L"` and `IfErrors 0 "L"` both compile and
 * jump (measured, probes r3 and the review's q2/q3) — so the quoted spans are
 * read as their own candidates (the fix round's N1).
 */
/**
 * Statements whose operands can be a label. A quoted span is read as a label
 * candidate only on one of these: the fix round's review (P3) measured
 * `DetailPrint "iaSweepNext"` — message text equal to a label this text
 * declares — read as a jump to that label and reported as a declined branch
 * that reaches the delete pass (one problem for the message, two for a string
 * naming two labels; both are correct code). Quoted operands stay read where
 * they are real: `Goto "iaSweepHop"` is a jump (measured), and the
 * `MessageBox … IDOK <label> IDCANCEL <label>` / `StrCmp` / `IfErrors` / `If*`
 * family all take label operands. `Call` is deliberately not here: its operand
 * names a function, and a function is not a label.
 */
const LABEL_OPERAND_STATEMENT =
  /^\s*(?:Goto|GotoIf|IfErrors|IfSilent|IfAbort|IfFileExists|StrCmp|StrCmpS|IntCmp|IntCmpU|MessageBox|MessageBoxEx)\b/i

function lineTokens(line) {
  const code = outsideStrings(line)
  // The statement without its comments: a `#`-comment naming a label is not a
  // jump either, and the earlier read took the quoted spans of the raw line.
  const statement = stripNsisComments(line)
  const quoted = LABEL_OPERAND_STATEMENT.test(statement)
    ? [...statement.matchAll(/(['"`])([^'"`]*)\1/g)].map((match) => match[2].trim())
    : []
  return [...code.trim().split(/\s+/), ...quoted]
    .map((token) => token.toLowerCase())
    .filter((token) => token !== '')
}

/**
 * The `${…}` tokens of `code`, with braces paired: a token ends at the `}` that
 * closes its own `${`, so `${${X}}` is one token. The earlier `\$\{[^}\s]+\}`
 * reading stopped at the first `}` and saw `${${X` for it — a name no
 * substitution map holds, so the token was invisible (the fix round's F-3).
 *
 * The nested form is not hypothetical: measured on 3.0.4.1, `!define IA.SEL
 * "IA.DISARM"` + `${${IA.SEL}}` with `!define IA.DISARM "StrCpy
 * $iaDeleteShapeCheck 0"` expands to the disarm and executes it (probe r5 — the
 * flag read back empty after the arming).
 */
function defineTokens(code) {
  const tokens = []
  for (let i = 0; i + 1 < code.length; i += 1) {
    if (code[i] !== '$' || code[i + 1] !== '{') continue
    let depth = 0
    let end = i + 1
    for (; end < code.length; end += 1) {
      if (code[end] === '{') depth += 1
      else if (code[end] === '}') {
        depth -= 1
        if (depth === 0) break
      }
    }
    if (depth !== 0) break
    tokens.push(code.slice(i, end + 1))
    i = end
  }
  return tokens
}

/**
 * The define name a `${…}` token (or a bare name) names, resolved the way the
 * preprocessor resolves it. `!define`s hold static text and the innermost token
 * is expanded first, so `${${IA.SEL}}` with `!define IA.SEL "IA.DISARM"` looks
 * up `IA.DISARM` — measured on 3.0.4.1: with `!define IA.DISARM "StrCpy
 * $iaDeleteShapeCheck 0"` the token executes the disarm (probe r5), and the
 * dashed value resolves because "plain name" is read as one non-whitespace
 * token (probe n6; the old `[A-Za-z0-9_.]` class resolved the dashed twin back
 * to the inner name and read that window clean — gap A-2). The walk repeats:
 * the braces are paired by `defineTokens`, and any name whose body is one more
 * token or bare name is replaced by it while that holds — so `${${${A}}}` with
 * A -> B -> C -> disarm reaches `C` and the disarm is read with it. One level of
 * nesting was all the earlier read did, so the depth-2 spelling and a define
 * body carrying one read as names no map holds and the window stayed green (the
 * fix round's review, P2). A body with whitespace is a statement (or several),
 * not a name, and stops the walk; a name no define holds stops it too — that is
 * the include boundary this guard does not follow (round-5 item 4). The walk is
 * bounded by `seen`, so `!define A "${A}"` (and `!define A "A"`) returns `a`
 * instead of looping. Every name is compared case-insensitively like every
 * other directive name.
 */
function defineName(text, substitutions) {
  const seen = new Set()
  let name = text.trim().toLowerCase()
  for (;;) {
    const wrapped = /^\$\{([\s\S]*)\}$/.exec(name)
    if (wrapped !== null) {
      name = wrapped[1].trim().toLowerCase()
      if (seen.has(name)) return name
      seen.add(name)
      continue
    }
    const body = substitutions.get(name)
    const value = body === undefined ? undefined : body.join(' ').trim()
    if (value === undefined || /\s/.test(value)) return name
    name = value.toLowerCase()
    if (seen.has(name)) return name
    seen.add(name)
  }
}

/**
 * True when every path through `statements` ends in a jump out — a bare
 * `Goto`/`Return`/`Abort`/`Quit`, or a trailing LogicLib block (`${If}` …
 * `${Else}` … `${EndIf}`) whose every arm does. `statements` are statement
 * texts (already comment-stripped); a trailing block's arms come from a local
 * `logicLibTrace` over them.
 *
 * The disposition review's R6: the branch test was "is the last text line a
 * jump", and a declined branch that closed with an `${EndIf}` over arms that
 * each jump out was reported as unterminated — a false report on a legal
 * shape. An arm that can fall through still fails the test, so the direction
 * stays fail-closed.
 */
function endsInJump(statements) {
  const trace = logicLibTrace(statements)
  const visit = (from, to) => {
    let last = to
    while (last >= from && statements[last].trim() === '') last -= 1
    if (last < from) return false
    if (/^\s*(Goto|Return|Abort|Quit)\b/i.test(statements[last])) return true
    const opener = trace.openOf.get(last)
    if (opener === undefined || opener < from) return false
    const cuts = [
      opener,
      ...trace.dividers.filter((d) => d.opener === opener && d.line > opener && d.line < last).map((d) => d.line),
      last,
    ]
    // In a `${Switch}`/`${Select}`, the text before the first `${Case}` is the
    // discriminant the macro compares, not a branch a path runs through, so it
    // is not an arm; every region after a `${Case}`/`${CaseElse}` is one. (For
    // `${If}`/`${ElseIf}`/`${Else}` the region before the first divider is the
    // Then arm and counts.) A switch with no divider at all keeps the plain
    // reading, so it is still reported (fail-closed).
    const firstDivider = cuts.length > 2 ? cuts[1] : undefined
    const isSwitch = /^\s*\$\{(Switch|Select)\b/i.test(statements[opener])
    // A `${Switch}`/`${Select}` with no `${Case}`/`${CaseElse}` at all has no
    // arm that can run: LogicLib jumps straight to the `${EndSwitch}` label, so
    // the body — a `Goto` in it included — is dead text and control leaves the
    // block after the closer (measured on 3.0.4.1: the body's marker file was
    // never written and the one after `${EndSwitch}` was — probes t7/t8). It
    // cannot be the branch's ending jump, so it is reported (fail-closed).
    // Reading the dead body's tail as an arm was this rule's own regression
    // (the fix round's review, F-1: the base guard reported the shape and the
    // fix round accepted it).
    if (isSwitch && firstDivider === undefined) return false
    const boundaries = firstDivider !== undefined && isSwitch ? cuts.slice(1) : cuts
    for (let a = 0; a + 1 < boundaries.length; a += 1) {
      if (!visit(boundaries[a] + 1, boundaries[a + 1] - 1)) return false
    }
    return true
  }
  return visit(0, statements.length - 1)
}

export function unguardedBackupDelete(source, options = {}) {
  const { sitePolicies = {}, requireSite = true } = options
  const problems = []
  const lines = stripNsisComments(source).split(/\r?\n/)
  const trace = logicLibTrace(lines)
  const macroRanges = macroBodyRanges(lines)
  // Either quote style is the same delete: measured on 3.0.4.1, `RMDir /r '…'`
  // and `RMDir /r `…`` both compile and remove the directory. The target is the
  // variable iaPrepareDelete writes and clears again on a refusal (#904), so a
  // delete of anything else is a site this guard cannot say anything about.
  // #919 Q4: NSIS takes options between `/r` and the target — `/REBOOTOK` is
  // the one that applies to RMDir — and that spelling is the same delete
  // (measured: it compiled and removed), so the options are part of the match.
  const deleteLine = /RMDir\s+\/r(?:\s+\/[A-Za-z]+)*\s+(['"`])\$iaDeleteTarget\1/i
  // The two statements the arming window reads, in every spelling that writes
  // the same variable. `StrCpy`'s output may be bare or quoted and stores the
  // same value either way (measured: `StrCpy "$vFlag" "0"` compiled and wrote
  // the 0 — probe r9), so the `_OUT` patterns (bare or quoted output) gate what
  // the scans collect while the full patterns read the operands. The candidate
  // copy takes one operand — a source, quoted or bare, or the register/spelling
  // forms the failing-to-read path reports (the review's F1/N2). The arming
  // takes the `[01]` value plus `StrCpy`'s optional `[maxlen] [startoffset]`,
  // which decide what is stored (measured, probe r6: `"1" 1`, `"1" 2` and
  // `"1" 1 0` store "1"; `"1" 0` and `"1" 2 1` store "") — the review's R5.
  const CANDIDATE_COPY_OUT = /^\s*StrCpy\s+(?:(['"`])\$iaDeleteCandidate\1|\$iaDeleteCandidate\b)/i
  const CANDIDATE_COPY = /^\s*StrCpy\s+(?:(['"`])\$iaDeleteCandidate\1|\$iaDeleteCandidate\b)\s+(?:(['"`])(\S+)\2|(\S+))\s*$/i
  const SHAPE_ARMING_OUT = /^\s*StrCpy\s+(?:(['"`])\$iaDeleteShapeCheck\1|\$iaDeleteShapeCheck\b)/i
  const SHAPE_ARMING = /^\s*StrCpy\s+(?:(['"`])\$iaDeleteShapeCheck\1|\$iaDeleteShapeCheck\b)\s+(?:(['"`])([01])\2|([01]))\s*(?:(\d+)\s*(?:(\d+)\s*)?)?$/i
  const ERRORS = '${Errors}'
  const errorsMacro = /\$\{Errors\}/i
  // A read is a line-anchored `${If}`/`${IfNot}`/`${Unless}` condition carrying
  // `${Errors}` — LogicLib expands the macro to `"" Errors ""`, which only
  // compiles as a condition — or a line-anchored raw `IfErrors`. Anything else
  // is reported instead of counted (round-5 item 1): a read the guard cannot
  // name must not satisfy a rule about reads.
  const errorsRead = /^\s*(?:\$\{(If|IfNot|Unless)\}[^\n]*\$\{Errors\}|IfErrors\b)/i
  const clearErrors = /^\s*ClearErrors\b/i
  const clearCall = /!insertmacro\s+iaClearBackupDir\b/i
  // #904: the delete may only run on the target iaPrepareDelete built — that
  // call is where the shape check and the reparse-point scan live — and only
  // behind a status gate, so a refused target is never deleted. iaPrepareDelete
  // clears the target on a refusal, and a caller that ignores the status cannot
  // delete anything (measured: `RMDir /r ""` removes nothing and sets the error
  // flag), but a site that never prepares at all would delete a path built
  // somewhere else. The uninstaller names the helper's un. twin (`Call
  // un.iaPrepareDelete`): NSIS only lets un. code call un.-prefixed functions
  // (measured), and installer-cleanup.nsh instantiates both name spaces from one
  // body — it is the same prepare step either way.
  const prepareCall = /^\s*Call\s+(?:un\.)?iaPrepareDelete\b/i
  const statusGate = /^\s*StrCmp\s+\$iaDeleteStatus\s+"ok"\s+0\s+\S+/i
  // Where the record has to land: HKCU, this application's own key, carrying the
  // whole backup path. Variable and define spellings are case-insensitive
  // (measured), so the match is too, and either the key or the value may be
  // quoted in any of the three forms.
  const recordWrite =
    /^\s*WriteRegStr\s+HKCU\s+(['"`])\$\{INSTALL_REGISTRY_KEY\}\1\s+(['"`])IaLeftoverDir\2\s+(['"`])?\$iaBackupDirectory\3?\s*$/i
  let sites = 0
  for (let i = 0; i < lines.length; i += 1) {
    if (!deleteLine.test(lines[i])) continue
    sites += 1
    // A macro body only runs where the macro is inserted, so its statements are
    // not on the delete's path unless they share the delete's macro body
    // (round-5 R4: a `ClearErrors` in a macro defined above the delete used to
    // satisfy the check).
    const onDeletePath = (index) =>
      macroRanges.every(
        (range) =>
          !(index >= range.start && index <= range.end) || (i >= range.start && i <= range.end),
      )
    const blockStart = enclosingBlockStart(lines, i)
    // Which site is this (#904)? The backup-delete rules below are the strict
    // default (`backup`: the promote site). The other two recursive deletes of
    // the shipped scripts have documented, narrower contracts and are named
    // explicitly by the caller through `sitePolicies`; an unlisted site keeps
    // the strict default, and a site name the table lists but the source does
    // not use simply never matches.
    const siteName = (
      /^\s*(?:Function|!macro)\s+(\S+)/i.exec(lines[blockStart] ?? '')?.[1] ?? ''
    ).toLowerCase()
    const policy =
      Object.entries(sitePolicies).find(
        ([name]) => name.toLowerCase() === siteName,
      )?.[1] ?? 'backup'
    const wants = {
      clearCall: policy === 'backup',
      readCount: policy === 'backup' || policy === 'sweep',
      record: policy === 'backup',
      // `partial` (the rollback delete of the partial install): its verifier is
      // the rename that follows, so the delete's own error is deliberately not
      // read — what it must guarantee is a recorded leftover somewhere in the
      // same function for the restore-failure path.
      deferredRecord: policy === 'partial',
      // `sweep` (the uninstaller): the application's registry key is on its way
      // out, so the record is the report and the exit code instead.
      sweepReport: policy === 'sweep',
    }
    // The target only exists after this call, and the call is what validates
    // the name (shape) and the tree (reparse points).
    const prepareIndex = (() => {
      for (let j = i - 1; j >= blockStart; j -= 1) {
        if (lines[j].trim() === '' || !onDeletePath(j)) continue
        if (prepareCall.test(lines[j])) return j
      }
      return -1
    })()
    if (prepareIndex === -1) {
      problems.push({
        line: i + 1,
        what: 'no Call iaPrepareDelete before the delete — the target is not shape-checked or scanned',
      })
    } else if (!onSameBranch(trace, prepareIndex, i)) {
      problems.push({
        line: i + 1,
        what: 'the Call iaPrepareDelete above the delete sits in a branch the delete is not on — it does not validate this target',
      })
    } else if (branchDividersBetween(trace, prepareIndex, i).length > 0) {
      problems.push({
        line: i + 1,
        what: 'the Call iaPrepareDelete above the delete sits in a sibling branch — it does not validate this target',
      })
    } else {
      const gateIndex = (() => {
        for (let j = i - 1; j > prepareIndex; j -= 1) {
          if (lines[j].trim() === '' || !onDeletePath(j)) continue
          if (statusGate.test(lines[j])) return j
        }
        return -1
      })()
      if (gateIndex === -1) {
        problems.push({
          line: i + 1,
          what: `no ${'StrCmp $iaDeleteStatus "ok"'} gate between iaPrepareDelete and the delete — a refused target would still be deleted`,
        })
      }
      // #904 item 3 (round 6): iaPrepareDelete runs the name check only when
      // this flag is armed, and the reparse scan alone is not the contract —
      // with the flag gone the delete runs on whatever name the caller built,
      // so the sweep would remove every "$INSTDIR.old-*" sibling, foreign or
      // unbraced (measured: a `"1"`→`"0"` mutation at installer.nsh:174 passed
      // every check of this guard and the whole unit suite).
      //
      // #919 Q1: the promote site's candidate is the other name that has to
      // pass the check. $iaBackupDirectory comes from HKCU, and only the
      // ".old-{guid}" shape separates the installer's own backup from any other
      // directory a user-writable value can name; the rule used to gate on the
      // sweep policy alone, so flipping the promote arming to "0" kept the
      // whole guard and suite green (measured). The rollback's candidate is the
      // partial install — a different contract, whose deliberate "0" is #904
      // item 3 — so this gate keys on the candidate, not on every non-sweep
      // site.
      const armsBackupName = (() => {
        // Every copy into $iaDeleteCandidate above the prepare call is what the
        // target can be built from: the Q2 reading, applied to the source. #919
        // review (F1): NSIS strings take all three quote forms and the bare
        // variable is the same copy (measured on 3.0.4.1), so the earlier
        // double-quote-only match read three legal copies as "no candidate".
        // The copy's OUTPUT may be quoted too — `StrCpy "$iaDeleteCandidate"
        // "$iaBackupDirectory"` compiles and stores (measured: `StrCpy "$vFlag"
        // "0"` wrote the 0, probe r9), and with only the bare spelling read, a
        // quoted output hid the copy from this list altogether, which skipped
        // the arming requirement the presence of a backup-name candidate
        // creates (the review's N2). The rollback's own partial install is the
        // one source that skips the requirement (#904 item 3) — and only when
        // it is the single source the window reads: a copy the guard cannot
        // read, or two different sources (different branches can each write the
        // candidate), leave the name the delete runs with unreadable, so the
        // site is held to the arming (fail-closed). Reading only the textually
        // last copy was the disposition review's N1: a branch-divergent layout
        // hid the backup spelling the presence rule had caught.
        const sources = []
        for (let j = blockStart; j < prepareIndex; j += 1) {
          if (!onDeletePath(j)) continue
          if (!CANDIDATE_COPY_OUT.test(lines[j])) continue
          const copy = CANDIDATE_COPY.exec(lines[j])
          sources.push(copy === null ? null : copy[3] ?? copy[4])
        }
        if (sources.length === 0) return false
        const readable = sources
          .filter((source) => source !== null)
          .map((source) => source.toLowerCase())
        if (readable.length !== sources.length || new Set(readable).size > 1) return true
        return !/^\$iaFinalDirectory$/i.test(readable[0])
      })()
      // #919 Q2: the flag is read when iaPrepareDelete runs, so the LAST
      // assignment above the call is the one that decides. The window used to
      // be satisfied by any `"1"` in it, so arming and then turning the flag
      // off again right after passed the whole guard and suite (measured).
      // #919 review (F1): the value takes any quote form or none — a
      // single-quoted `'0'` disarmed the site while the old match read the
      // line as not-an-assignment — and an assignment whose value the guard
      // cannot read (a register source) may be the disarm, so it cannot count
      // as an arming: it is carried as not-armed. #919 review (N2): the
      // output may be quoted as well (measured, probe r9), and reading only
      // the bare spelling let a quoted-output disarm hide behind a line the
      // loop never collected. The disposition review's R5: the operand list is
      // part of the statement's meaning — measured (probe r6), `"1" 1`, `"1" 2`
      // and `"1" 1 0` all store "1" while `"1" 0` and `"1" 2 1` store "" — so
      // a maxlen of at least 1 with an offset of 0 arms, and the truncating or
      // unreadable forms do not (fail-closed; the maxlen spelling used to be
      // reported although it arms).
      const arming = []
      for (let j = blockStart; j < prepareIndex; j += 1) {
        if (!onDeletePath(j)) continue
        if (!SHAPE_ARMING_OUT.test(lines[j])) continue
        const assignment = SHAPE_ARMING.exec(lines[j])
        const value = assignment === null ? null : (assignment[3] ?? assignment[4])
        const maxlen = assignment?.[5]
        const offset = assignment?.[6]
        arming.push({
          index: j,
          armed:
            value === '1' &&
            (maxlen === undefined || Number(maxlen) >= 1) &&
            (offset === undefined || Number(offset) === 0),
        })
      }
      // #905's ruling is to fail closed on macro insertion, and the arming
      // window is where it matters most: an `!insertmacro`, a `${define}` or an
      // `!include` on the delete's path expands statements this text does not
      // show, so a body that touches the candidate or the flag leaves the delete
      // running with a state no line above reads (measured: a disarm inserted
      // between the arming and the prepare call passed the whole guard and suite
      // — #919 review F1/A4). The map holds the `!macro` bodies of this text and
      // its single-line `!define` bodies; the lookup is name-keyed and resolves
      // nested tokens. A macro this text does not define counts as touching the
      // variables (the include boundary, which the guard does not follow —
      // round-5 item 4); a `${NAME}` this text does not define is a
      // LogicLib/FileFunc helper and is not resolved (a define from an
      // `!include`d file is the same boundary); an `!include` is refused
      // outright.
      const substitutions = new Map()
      for (const range of macroRanges) {
        const name = /^\s*!macro\s+(\S+)/i.exec(lines[range.start])?.[1]
        if (name !== undefined) substitutions.set(name.toLowerCase(), lines.slice(range.start + 1, range.end))
      }
      for (let j = 0; j < lines.length; j += 1) {
        const define = /^\s*!define\s+(\S+)\s+(.+)$/i.exec(lines[j])
        if (define === null) continue
        // A trailing `\` continues the value on the next line, and the
        // preprocessor joins the pieces before inserting them — measured on
        // 3.0.4.1: `!define D "StrCpy $vFlag \` + `0"` compiles and the `${D}`
        // insertion executes the statement (probe u4). Reading only the first
        // fragment left the tail of such a body unread, so a write carried
        // there counted as clean (the review's N3).
        let value = define[2]
        while (/\\\s*$/.test(value) && j + 1 < lines.length) {
          value = `${value.replace(/\\\s*$/, '')}${lines[j + 1].trim()}`
          j += 1
        }
        value = value.trim()
        // A define's value is inserted verbatim, quotes included, and `${NAME}`
        // inserts it at the use site. The outer quote pair is stripped here, or
        // the string rule would hide whatever the value carries — measured on
        // makensis 3.0.4.1: `!define D "!insertmacro M"` + `${D}` does expand
        // the insertion and the body runs (probe u5), so the body is read even
        // though the inserted text sits inside the value's own quotes there.
        // Dot- and dash-spelled names are legal too (measured), so the token
        // below takes any name up to a brace.
        const unquoted = /^(['"`])([\s\S]*)\1$/.exec(value)?.[2] ?? value
        substitutions.set(define[1].toLowerCase(), [unquoted])
        // The define's body is text the delete-site scans cannot see: the value
        // sits inside its own quotes, so a `RMDir /r` inside it is blanked with
        // them (measured: `!define IAHIDDEN 'RMDir /r "…"'` plus `${IAHIDDEN}`
        // compiled, deleted the directory, and passed every scan — the fix
        // round's review, P3). A recursive delete is reported where it is
        // written; the delete sites this guard reads are statements of this
        // file, not text a define inserts.
        if (/RMDir\s+\/r/i.test(unquoted)) {
          problems.push({
            line: j + 1,
            what: `the define ${define[1]} carries a recursive delete (\`RMDir /r\`) in its body — the delete sites this scan reads are statements of this file, and this one is text the define inserts wherever it is used`,
          })
        }
      }
      const touchesDeleteVars = (name, seen = new Set()) => {
        if (seen.has(name)) return false
        seen.add(name)
        const body = substitutions.get(name)
        if (body === undefined) return true
        // A body that is one bare name — any single non-whitespace token, dash
        // spellings included (measured, probe n6) — continues the chain:
        // `${${IA.SEL}}` with `!define IA.SEL "IA.DISARM"` resolves the inner
        // token to that text and
        // uses it as the outer name (measured, probe r5) — `defineName` follows
        // the measured step, and a body that is itself a defined name is
        // followed too (fail-closed: following can only find more writes).
        if (body.length === 1) {
          const only = body[0].trim()
          if (!/\s/.test(only) && substitutions.has(only.toLowerCase())) {
            return touchesDeleteVars(only.toLowerCase(), seen)
          }
        }
        return body.some((line) => {
          const code = outsideStrings(line)
          if (/\$iaDeleteCandidate\b|\$iaDeleteShapeCheck\b/i.test(code)) return true
          const insert = INSERT_MACRO.exec(code)?.[1]
          if (insert !== undefined && touchesDeleteVars(insert.toLowerCase(), seen)) return true
          return defineTokens(code).some((token) => {
            const nested = defineName(token, substitutions)
            return substitutions.has(nested) && touchesDeleteVars(nested, seen)
          })
        })
      }
      // The window's own statements are read as the two tracked copies, and a
      // copy whose value the guard cannot read is carried as such (F1 above).
      // Anything else that uses either variable is a state the guard cannot
      // follow: `Pop`/`IntOp`/`ReadRegStr`/`StrLen` can write them, a status
      // compare reads them, and the insertion/define/include forms expand text
      // from elsewhere. All of it is reported (fail-closed; the disposition
      // review's R1 measured the instruction forms green at both the
      // disposition and its base).
      const windowHazards = []
      for (let j = blockStart; j < prepareIndex; j += 1) {
        if (!onDeletePath(j)) continue
        const code = outsideStrings(lines[j])
        const insert = INSERT_MACRO.exec(code)
        if (insert !== null && touchesDeleteVars(insert[1].toLowerCase())) {
          windowHazards.push(
            `!insertmacro ${insert[1]} at line ${j + 1} expands inside the arming window and touches $iaDeleteCandidate or $iaDeleteShapeCheck — the state the delete runs with is not in this window's text (fail-closed)`,
          )
          continue
        }
        if (/^\s*!include\b/i.test(code)) {
          windowHazards.push(
            `!include at line ${j + 1} sits in the arming window — the included text is not read here (round-5 item 4) and can write the candidate or the flag (fail-closed)`,
          )
          continue
        }
        const defineUse = defineTokens(code).find((token) => {
          const name = defineName(token, substitutions)
          return substitutions.has(name) && touchesDeleteVars(name)
        })
        if (defineUse !== undefined) {
          windowHazards.push(
            `${defineUse} at line ${j + 1} expands to a define that touches $iaDeleteCandidate or $iaDeleteShapeCheck — the state the delete runs with is not in this window's text (fail-closed)`,
          )
          continue
        }
        // Comments are not part of this test (the scans above read `code` the
        // same way, and a comment mention reports — over-report). Quoted text is
        // read only where a quote can hold nothing but the variable itself: a
        // quoted `StrCpy` output is a write one quote form away (the review's
        // N2 — `StrCpy "$vFlag" "0"` compiled and wrote the 0, probe r9), and
        // the same is true of another instruction's `$var`-typed parameter,
        // while a mention inside a message string is not a write and stays
        // unreported (the disposition review's N3/R4 negative).
        const quotedTarget = [...lines[j].matchAll(/(['"`])([^'"`]*)\1/g)]
          .map((match) => match[2].trim())
          .filter((span) => /^\$(?:iaDeleteCandidate|iaDeleteShapeCheck)$/i.test(span))
        const mention = `${code} ${quotedTarget.join(' ')}`
        if (
          /\$iaDeleteCandidate\b|\$iaDeleteShapeCheck\b/i.test(mention) &&
          !CANDIDATE_COPY_OUT.test(lines[j]) &&
          !SHAPE_ARMING_OUT.test(lines[j])
        ) {
          windowHazards.push(
            `a statement at line ${j + 1} uses $iaDeleteCandidate or $iaDeleteShapeCheck outside the two copies the guard reads (StrCpy $iaDeleteCandidate <source>, StrCpy $iaDeleteShapeCheck "[01]") — the state the delete runs with cannot be read from this window (fail-closed)`,
          )
        }
      }
      const windowHazard = windowHazards[0]
      if (windowHazard !== undefined) problems.push({ line: i + 1, what: windowHazard })
      const lastArmed = arming[arming.length - 1]
      const mustArm = policy === 'sweep' || armsBackupName
      // #919 Q3: the arming only counts when it runs on the path the prepare
      // call is on — the same branch conditions the prepare and gate rules
      // already carry. Inside a branch that closes above the call the flag is
      // never set when the delete runs, and inside the other side of an
      // `${Else}` it is set somewhere the call does not reach (measured: both
      // forms passed the whole guard and suite).
      const armed =
        lastArmed !== undefined &&
        lastArmed.armed &&
        onSameBranch(trace, lastArmed.index, prepareIndex) &&
        branchDividersBetween(trace, lastArmed.index, prepareIndex).length === 0
      if (mustArm && !armed) {
        problems.push({
          line: i + 1,
          what: `${policy === 'sweep' ? 'the sweep' : 'the delete of a $iaBackupDirectory candidate'} has no readable StrCpy $iaDeleteShapeCheck "1" above its prepare call — an arming whose value, maxlen or offset truncates the flag, or whose shape this scan cannot read, counts as not armed (fail-closed), and iaPrepareDelete then skips the name check and deletes whatever name the caller built`,
        })
      }
    }
    const blockEnd = (() => {
      for (let j = i + 1; j < lines.length; j += 1) {
        if (CLOSES_FUNCTION.test(lines[j])) return j
      }
      return lines.length - 1
    })()
    // `ClearErrors` has to be on the delete's own path: a clear in the previous
    // function, in a macro definition, in a branch the delete is not on, or on
    // the far side of an `${Else}` does not clear this one.
    const candidates = []
    for (let j = i - 1; j >= blockStart; j -= 1) {
      if (lines[j].trim() === '' || !onDeletePath(j)) continue
      candidates.push(j)
      if (candidates.length === 3) break
    }
    const clearCandidate = candidates.find((j) => clearErrors.test(lines[j]))
    if (clearCandidate === undefined) {
      problems.push({ line: i + 1, what: 'missing ClearErrors before the delete' })
    } else if (!onSameBranch(trace, clearCandidate, i)) {
      problems.push({
        line: i + 1,
        what: 'the ClearErrors above the delete sits in a branch the delete is not on — it does not clear this path',
      })
    } else if (branchDividersBetween(trace, clearCandidate, i).length > 0) {
      problems.push({
        line: i + 1,
        what: 'the ClearErrors above the delete sits in a sibling branch — it does not clear this path',
      })
    }
    const rest = lines.slice(i + 1)
    const enclosing = [
      rest.findIndex((line) => /^\s*FunctionEnd\b/i.test(line)),
      rest.findIndex((line) => /^\s*!macroend\b/i.test(line)),
    ].filter((index) => index !== -1)
    const limit = enclosing.length === 0 ? -1 : Math.min(...enclosing)
    const clearIndex = rest.findIndex(
      (line, offset) => clearCall.test(line) && onDeletePath(i + 1 + offset),
    )
    if (wants.clearCall && (clearIndex === -1 || (limit !== -1 && limit < clearIndex))) {
      problems.push({ line: i + 1, what: 'no !insertmacro iaClearBackupDir after the delete' })
    }
    const bounds = [...enclosing, clearIndex].filter((index) => index !== -1)
    const windowEnd = bounds.length === 0 ? rest.length : Math.min(...bounds)
    const window = []
    for (let offset = 0; offset < windowEnd; offset += 1) {
      if (onDeletePath(i + 1 + offset)) window.push({ line: rest[offset], index: i + 1 + offset })
    }
    const readHits = window.filter(({ line }) => errorsRead.test(line))
    const strayErrors = window.filter(({ line }) => errorsMacro.test(line) && !errorsRead.test(line))
    // The window's far end is the clear call at the promote site and the end of
    // the block everywhere else, so the messages name the bound they use.
    const windowEndLabel = wants.clearCall ? 'iaClearBackupDir' : 'the end of the delete\'s block'
    if (wants.readCount) {
      if (readHits.length === 0) {
        problems.push({ line: i + 1, what: `missing ${ERRORS} check before ${windowEndLabel}` })
      } else if (readHits.length > 1) {
        problems.push({
          line: i + 1,
          what: `${readHits.length} ${ERRORS} reads between the delete and ${windowEndLabel} — only the first can see the failed delete`,
        })
      }
      for (const stray of strayErrors) {
        problems.push({
          line: stray.index + 1,
          what: `an unrecognized ${ERRORS} usage between the delete and ${windowEndLabel} — only a ${'${If}/${IfNot}/${Unless}'} condition or a raw IfErrors reads the flag`,
        })
      }
      const firstRead = readHits.length > 0 ? readHits[0].index : -1
      if (
        firstRead !== -1 &&
        window.some(({ line, index }) => index < firstRead && clearErrors.test(line))
      ) {
        problems.push({
          line: i + 1,
          what: `ClearErrors between the delete and the ${ERRORS} read discards the failed delete`,
        })
      }
    }
    // The record has to land where the reader looks (HKCU, this app's own key)
    // and carry the whole directory, not a name built from it: a record of
    // "$iaBackupDirectory-tmp" or one written to another hive names something
    // the next promote will never find — the R1 orphan again.
    const recordHit = window.find(({ line }) => recordWrite.test(line))
    if (wants.record && recordHit === undefined) {
      problems.push({
        line: i + 1,
        what: 'failed delete is not recorded (no IaLeftoverDir write of $iaBackupDirectory)',
      })
    }
    if (wants.deferredRecord) {
      // The rollback site: the delete's own error is not read (the rename that
      // follows is the operation whose failure has to be visible), but the
      // restore-failure branch has to record the backup — that branch, not the
      // function at large: the refused branch carries its own record, so
      // pinning the record to the block let a rollback drop the restore
      // failure's record while every check stayed green (#919 Q7, measured).
      const failure = restoreFailureWindow(
        lines,
        trace,
        { start: blockStart, end: blockEnd },
        onDeletePath,
        (line) => errorsRead.test(line),
      )
      const recorded =
        failure !== null &&
        lines
          .slice(failure.from, failure.to)
          .some((line, offset) => onDeletePath(failure.from + offset) && recordWrite.test(line))
      if (!recorded) {
        problems.push({
          line: i + 1,
          what:
            failure === null
              ? 'the rollback site\'s restore-failure read is not in a form this scan can measure (a raw IfErrors/StrCmp jump leaves no readable branch end), so the record that path has to carry cannot be verified here (fail-closed)'
              : 'the rollback site does not record a leftover on the restore-failure path (no IaLeftoverDir write of $iaBackupDirectory under the post-Rename ${Errors} read)',
        })
      }
    }
    // #919 Q3: the sweep's exit code is found here so it can join the
    // dead-branch scan below like every other guarded statement (-1 elsewhere).
    let exitCodeIndex = -1
    // #919 review (S2): the declined branch's exit code belongs to the scan for
    // the same reason — a `SetErrorLevel 2` the declined branch never reaches
    // still satisfied the declined-exit rule.
    let declinedExitIndex = -1
    if (wants.sweepReport) {
      // The uninstaller sweep: the application's registry key is being removed,
      // so the record is the report and the exit code. Enumerating by name shape
      // is what makes the sweep independent of the single-slot records, and the
      // prompt is the user's say before anything is deleted.
      const block = lines.slice(blockStart, blockEnd + 1)
      const required = [
        {
          re: /FindFirst\s+\$\d\s+\$\d\s+"\$INSTDIR\.old-\*"/i,
          what: 'the sweep does not enumerate "$INSTDIR.old-*" by name shape',
        },
        {
          re: /^\s*\$\{GetParent\}\s+"\$INSTDIR"\s+\$\d/im,
          what:
            'the sweep does not cut the parent off $INSTDIR in FileFunc\'s ("[path]" $result) order — swapped, the macro\'s last Pop lands on $INSTDIR and the delete pass then enumerates ".old-*" relative and finds nothing (measured on the real uninstaller)',
        },
        {
          re: /\$\(iaLeftoverSweep\)/,
          what: 'the sweep does not ask before deleting (no $(iaLeftoverSweep) prompt)',
        },
      ]
      for (const { re, what } of required) {
        if (!block.some((line) => re.test(line))) problems.push({ line: i + 1, what })
      }
      // #919 fix round (Q9 follow-up): the failure path has to keep counting.
      // The ${ERRORS} rule above pins that the delete's own error is read; it
      // does not pin where that read's fall-through goes. Measured gap: a
      // `Goto iaSweepNext` between the failure-branch status write and the kept
      // label made every failed delete skip the kept accounting — the counter
      // never moved for a failed candidate, so a sweep whose deletes all failed
      // reported nothing and left the exit code at its 0 default (the fix
      // round's own probe: that edit kept the whole suite green). The walk below
      // follows the read's own failure leg through this block's label table —
      // the leg the read's form names: the fall-through for `IfErrors 0 label`,
      // the target for `IfErrors label` / `IfErrors label 0`, the then-arm for
      // `${If} ${Errors}`, and the `${Else}` arm (or the
      // line after the `${EndIf}` when there is none) for the `${IfNot}` and
      // `${Unless}` `${Errors}` spellings. The failure path must reach the label
      // the status gate jumps to — the refusal landing, where a kept candidate
      // is counted — or pass a counter bump of its own (a sweep that keeps its
      // own books in a branch of its own is legal). A bump in a branch that
      // cannot run (`1 == 0`) does not count, and a leg a second divider
      // (`${ElseIf}`) forks is reported rather than guessed at. A path that
      // loops back, leaves the block, or runs out of statements without either
      // is reported (fail-closed).
      const gateTarget = (() => {
        for (const line of block) {
          const match = /^\s*StrCmp\s+\$iaDeleteStatus\s+"ok"\s+0\s+(\S+)/i.exec(line)
          if (match !== null) return match[1].toLowerCase()
        }
        return null
      })()
      const failureReads = []
      for (let k = i - blockStart + 1; k < block.length; k += 1) {
        if (errorsRead.test(block[k])) failureReads.push(k)
      }
      // The read rule owns the missing/multiple report; this rule only judges
      // where a single readable read's failure path goes.
      if (gateTarget !== null && failureReads.length === 1) {
        const reachOffsets = new Map()
        for (let k = 0; k < block.length; k += 1) {
          for (const { name } of labelDeclarations(block[k])) {
            if (!reachOffsets.has(name)) reachOffsets.set(name, k)
          }
        }
        const gateOffset = reachOffsets.get(gateTarget)
        const countBump = /^\s*IntOp\s+(\$\S+)\s+\1\s*\+\s*(?:[1-9]\d*)\s*$/i
        const bareJump = /^\s*Goto\s+(\S+)\s*$/i
        const terminal = /^\s*(?:Return|Abort|Quit)\b/i
        // Which leg of the read is the failure path comes from the read's own
        // form (the fix round's review, P1): `IfErrors 0 label` jumps when there
        // is *no* error, so the failure path is the line after the read, while
        // `IfErrors label` and `IfErrors label 0` jump *on* error, so the
        // failure path is that label — reading the fall-through for every form
        // stayed green on a one-argument sweep whose failure path skipped the
        // kept count and reported its correct twin (measured, probe n10).
        // `${If} ${Errors}` has the failure path in its own arm; the `${IfNot}`
        // and `${Unless}` spellings have it in the `${Else}` arm, or after the
        // `${EndIf}` when there is none. An arm a second divider forks
        // (`${ElseIf}`) is reported rather than guessed at, and a target built
        // from a variable reads as undeclared, so that read reports too
        // (fail-closed).
        const failureLegOf = (readAt) => {
          const text = stripNsisComments(block[readAt])
          const jump = /^\s*IfErrors\s+(\S+)(?:\s+(\S+))?/i.exec(text)
          if (jump !== null && jump[1] !== '0') {
            const target = jump[1].replace(/^(['"`])([\s\S]*)\1$/, '$2').toLowerCase()
            return { at: reachOffsets.get(target) ?? -1 }
          }
          if (!/^\s*\$\{(?:IfNot|Unless)\}\s+\$\{Errors\}/i.test(text)) {
            return { at: readAt + 1 }
          }
          const close = trace.closeOf.get(blockStart + readAt)
          if (close === undefined) return { at: readAt + 1 }
          const divide =
            trace.dividers.find((divider) => divider.opener === blockStart + readAt)?.line ?? -1
          if (divide === -1) return { at: close - blockStart + 1 }
          if (!/^\s*\$\{Else\}/i.test(lines[divide])) return { blocked: divide - blockStart }
          return { at: divide - blockStart + 1 }
        }
        // A branch whose condition is a constant false never runs, so a counter
        // bump inside it is not the kept accounting (the review's minimal fix
        // reuses the dead-branch reading the exit-code scan uses).
        const deadRanges = []
        for (let k = 0; k < block.length; k += 1) {
          if (deadBranchText(block[k]) === undefined) continue
          deadRanges.push([k, (trace.closeOf.get(blockStart + k) ?? blockEnd) - blockStart])
        }
        const leg = failureLegOf(failureReads[0])
        let blocked = leg.blocked
        const reads = new Set(failureReads)
        const seen = new Set()
        let keeps = false
        let at = leg.at ?? -1
        while (at >= 0 && at < block.length && !seen.has(at)) {
          seen.add(at)
          if (gateOffset !== undefined && at === gateOffset) {
            keeps = true
            break
          }
          const dead = deadRanges.find(([from, to]) => at >= from && at <= to)
          if (dead !== undefined) {
            at = dead[1] + 1
            continue
          }
          // Reaching an `${Else}`-family divider from inside its own block means
          // the block's end (LogicLib jumps there), so the walk continues after
          // the matching close instead of walking into a sibling arm.
          const divider = trace.dividers.find(
            (entry) =>
              entry.line === blockStart + at &&
              trace.stackBefore[blockStart + at].includes(entry.opener),
          )
          if (divider !== undefined) {
            at = (trace.closeOf.get(divider.opener) ?? blockEnd) - blockStart + 1
            continue
          }
          if (reads.has(at)) {
            const next = failureLegOf(at)
            if (next.blocked !== undefined) {
              blocked = next.blocked
              break
            }
            at = next.at
            continue
          }
          const declaration = labelDeclarations(block[at]).at(-1) ?? null
          const statement = stripNsisComments(
            declaration === null ? block[at] : declaration.rest,
          ).trim()
          if (countBump.test(statement)) {
            keeps = true
            break
          }
          if (terminal.test(statement)) break
          const hop = bareJump.exec(statement)
          if (hop !== null) {
            at = reachOffsets.get(hop[1].toLowerCase()) ?? -1
            continue
          }
          at += 1
        }
        if (!keeps) {
          problems.push({
            line: blocked === undefined ? i + 1 : blockStart + blocked + 1,
            what:
              blocked === undefined
                ? 'the failed delete\'s path does not reach the kept accounting — it jumps away, loops back, or leaves the block before the label the status gate jumps to (or a counter bump this scan can read), so a failed delete is neither counted nor reported: the sweep can finish with the leftovers on disk and the exit code still at 0'
                : 'the failed delete\'s path leaves the read through an arm this scan cannot read — a second divider (`${ElseIf}`) forks the failure leg, so whether a failed delete is counted or reported cannot be read here (fail-closed)',
          })
        }
      }
      // #919 Q8: declining the prompt is a third "kept" outcome — the leftovers
      // are still on disk — and it is UI-only (a silent run takes the /SD IDOK
      // default), so nothing else here reads it. The prompt's own IDCANCEL
      // target names the branch: it has to exist (a prompt with no way to say
      // no is not the contract), and the branch has to set the exit code
      // exactly like the kept path below the delete. A cancel that lands on the
      // success label used to pass every check (measured).
      const promptOffset = block.findIndex((line) => /\$\(iaLeftoverSweep\)/.test(line))
      if (promptOffset !== -1) {
        // The prompt's branch table is `<action> <label>` pairs after the
        // `/SD <default>` marker: `MessageBox … /SD IDOK IDOK iaSweepDelete
        // IDCANCEL iaSweepDeclined`. The first action token after `/SD` is the
        // silent default, not a branch — the old `/IDCANCEL\s+(\S+)/` scan read
        // that marker as the branch keyword and paired it with the next action
        // keyword instead of a label, so a legal `/SD IDCANCEL` default was
        // reported as a declined branch that does not set the exit code
        // (measured, #919 review N5: false positive pre-fix, this parser
        // green). String contents are blanked before tokenizing, so the prompt
        // text cannot contribute tokens.
        const tokens = outsideStrings(block[promptOffset]).trim().split(/\s+/)
        const ACTION = /^(IDOK|IDCANCEL|IDABORT|IDRETRY|IDIGNORE|IDYES|IDNO|IDCLOSE)$/i
        const branchLabels = new Map()
        for (let k = 0; k + 1 < tokens.length; k += 1) {
          if (tokens[k - 1] === '/SD') continue
          if (ACTION.test(tokens[k]) && !ACTION.test(tokens[k + 1])) {
            branchLabels.set(tokens[k].toUpperCase(), tokens[k + 1])
          }
        }
        const cancelTarget = branchLabels.get('IDCANCEL')
        if (cancelTarget === undefined) {
          problems.push({
            line: blockStart + promptOffset + 1,
            what: 'the sweep prompt has no IDCANCEL branch — a UI uninstall cannot decline the sweep',
          })
        } else {
          // A chain declares both names at one position (`A: B:` — measured
          // legal, probe r8), so the prompt's target may be any name on the
          // line: matching only the first reported a legal chain as a missing
          // exit code (the fix round's review, F-5).
          const canceledBy = (line) =>
            labelDeclarations(line).some((declaration) => declaration.name === cancelTarget.toLowerCase())
          const labelOffset = block.findIndex(canceledBy)
          const nextLabelOffset = (() => {
            for (let j = labelOffset + 1; j < block.length; j += 1) {
              if (labelDeclaration(block[j]) !== null) return j
            }
            return -1
          })()
          const branchEnd = nextLabelOffset === -1 ? blockEnd : blockStart + nextLabelOffset - 1
          const declinedExit =
            labelOffset === -1
              ? -1
              : keptExitLevel(
                  lines,
                  blockStart + labelOffset,
                  branchEnd,
                  onDeletePath,
                  labelDeclarations(block[labelOffset]).at(-1)?.rest ?? '',
                )
          declinedExitIndex = declinedExit
          if (declinedExit === -1) {
            // The fix round's review (P4) measured `IDCANCEL +4` — a relative
            // jump, so no label in this block carries the name — reported as
            // "does not set the exit code to 2", which says the branch was read
            // and found wanting. It was not read at all: the target is what
            // cannot be resolved, and that is what is reported.
            problems.push({
              line: labelOffset === -1 ? blockStart + promptOffset + 1 : blockStart + labelOffset + 1,
              what:
                labelOffset === -1
                  ? "the sweep prompt's IDCANCEL target is not a label this block declares — a relative or `$`-built target cannot be followed, so whether the declined branch keeps cannot be read here (fail-closed)"
                  : 'the declined sweep branch does not set the exit code to 2 — a declined sweep reports success',
            })
          }
          // #919 review (F3): setting the exit code is not enough — the branch
          // has to keep. The delete pass is the labelled loop the delete sits
          // in: a jump that lands on a label above the delete runs into it
          // again, and so does one that lands on a label inside the loop below
          // the `RMDir` (the disposition review's R3 measured `Goto iaSweepNext`
          // re-entering the loop tail with the enumeration handle already
          // closed). The loop's span runs from the back-referenced label down to
          // the statement carrying the reference — read from any statement, not
          // only a bare `Goto`: a two-target `StrCmp … iaSweepLoopEnd iaSweepLoop`
          // is a legal back-jump the earlier bare-`Goto` reading saw as no loop
          // at all (the fix round's own review measured the tail legs going
          // silent under it). A label whose own body enters the pass counts as
          // the pass too: the same review's hop label — declared below the loop,
          // jumping back into the tail — laundered a declined `Goto` into a
          // silent green. A jump to a label outside the span, or to a label this
          // block does not declare, is the kept path. The branch must also end in
          // a jump out: an unterminated branch, or one whose last statement the
          // guard cannot read (an `${EndIf}` over arms that each jump), is
          // reported — the declined contract is "keep only", so the fail-closed
          // direction is the right one.
          if (labelOffset !== -1) {
            // A label's own line can carry its first statement (`iaSweepDeclined:
            // SetErrorLevel 2` — measured legal in a Section and in a `!macro`
            // body, probes r1/r2; reading the label line as "bare label or
            // nothing" was the review's F-1: an inline-declared hop was invisible
            // to the label table), so the branch body is the label's inline rest
            // followed by the lines below it, and a label's span below starts
            // from the same rest.
            const restOf = (at) => labelDeclarations(block[at]).at(-1)?.rest ?? ''
            const body = [restOf(labelOffset), ...block.slice(labelOffset + 1, nextLabelOffset === -1 ? block.length : nextLabelOffset)]
            const labelOffsets = new Map()
            const labelNames = new Map()
            for (let k = 0; k < block.length; k += 1) {
              for (const { name } of labelDeclarations(block[k])) {
                if (!labelOffsets.has(name)) {
                  labelOffsets.set(name, k)
                  labelNames.set(k, name)
                }
              }
            }
            // Every token that names a label this block declares. Reference
            // positions vary (`Goto L`, `IfErrors 0 L`, `StrCmp $x "" A B`), so
            // the token, not the instruction, is what is read — and a target may
            // be quoted (`Goto "L"` compiles and jumps, measured in the review's
            // q2/q3), which `outsideStrings` alone blanks away (the review's N1).
            const labelTokens = (line) => lineTokens(line).filter((token) => labelOffsets.has(token))
            const deleteOffset = i - blockStart
            const loopHeads = []
            const loopJumps = []
            for (let k = deleteOffset; k < block.length; k += 1) {
              for (const token of labelTokens(block[k])) {
                const head = labelOffsets.get(token)
                if (head <= deleteOffset) {
                  loopHeads.push(head)
                  loopJumps.push(k)
                }
              }
            }
            const passStart = loopHeads.length === 0 ? deleteOffset : Math.min(...loopHeads)
            const passEnd = loopJumps.length === 0 ? deleteOffset : Math.max(...loopJumps)
            const landsInDeletePass = (landing) =>
              landing !== -1 && (landing <= deleteOffset || (landing >= passStart && landing <= passEnd))
            // A label enters the pass when it is declared in it or when its own
            // span (up to the next label) names a label that does — a bounded
            // closure, cycle-safe, so a hop label cannot launder the contract.
            const reachesDeletePass = (() => {
              const memo = new Map()
              const visit = (name, path) => {
                const known = memo.get(name)
                if (known !== undefined) return known
                if (path.has(name) || !labelOffsets.has(name)) return false
                const at = labelOffsets.get(name)
                if (landsInDeletePass(at)) return true
                let next = -1
                for (let k = at + 1; k < block.length; k += 1) {
                  if (labelNames.has(k)) {
                    next = k
                    break
                  }
                }
                const span = [restOf(at), ...block.slice(at + 1, next === -1 ? block.length : next)]
                const walk = new Set(path)
                walk.add(name)
                const hit = span.some((line) =>
                  labelTokens(line).some((token) => visit(token, walk)),
                )
                memo.set(name, hit)
                return hit
              }
              return (name) => visit(name, new Set())
            })()
            const reaches = body.some((line) =>
              labelTokens(line).some((token) => reachesDeletePass(token)),
            )
            // The branch has to end in a jump out, and the text's last line is
            // not enough to decide that: a declined branch that closes with an
            // `${If}`/`${Else}`/`${EndIf}` over arms that each jump out was
            // reported as unterminated (the disposition review's R6 — false
            // report on a legal shape). Comments are stripped first (`Goto X ;
            // done` ends the branch like `Goto X` does), and an arm that can
            // fall through still fails the test, so the direction stays
            // fail-closed.
            const statements = body.map((line) => stripNsisComments(line))
            const fallsThrough =
              nextLabelOffset !== -1 &&
              reachesDeletePass(labelNames.get(nextLabelOffset) ?? '') &&
              !endsInJump(statements)
            const coversDelete = body.some((line) => deleteLine.test(line))
            // A jump to a label this block does not declare leaves the text this
            // analysis can see: sibling `!macro` bodies share the label namespace
            // (measured: a cross-body `Goto` compiles and jumps, probe r4), so
            // the target's own body can enter the delete pass unseen. Fail-closed:
            // any token of the branch that names a label the file declares
            // outside this block is reported.
            const foreign = new Set()
            for (let k = 0; k < lines.length; k += 1) {
              if (k >= blockStart && k <= blockEnd) continue
              for (const { name } of labelDeclarations(lines[k])) foreign.add(name)
            }
            const branchTokens = body.flatMap((line) => lineTokens(line))
            const foreignJump = branchTokens.find((token) => !labelOffsets.has(token) && foreign.has(token))
            if (foreignJump !== undefined) {
              problems.push({
                line: blockStart + labelOffset + 1,
                what: `the declined sweep branch references ${foreignJump}, a label the file declares outside this block — where that jump lands cannot be read here (fail-closed)`,
              })
            }
            if (reaches || fallsThrough || coversDelete) {
              problems.push({
                line: blockStart + labelOffset + 1,
                what: 'the declined sweep branch can reach the delete pass, or does not end in a jump out of it — a declined sweep must only keep',
              })
            }
          }
        }
      }
      // #904 item 4 (round 6): the exit code is the contract the caller reads,
      // so its value is pinned — 2, the reading the launch-form probe and the
      // real-machine driver assert — and it has to sit below the delete: a
      // `SetErrorLevel 0` next to it, or one above the delete that runs before
      // anything was kept, used to satisfy a presence check. #919 Q3: it also
      // has to sit on the delete's path (a `SetErrorLevel 2` inside another
      // macro body in the same span only runs where that macro is inserted),
      // and it joins the dead-branch scan below — a `2` inside a
      // constant-false branch never runs at all, and both forms used to
      // satisfy this rule (measured).
      exitCodeIndex = keptExitLevel(lines, i, blockEnd, onDeletePath)
      if (exitCodeIndex === -1) {
        problems.push({
          line: i + 1,
          what: 'nothing sets the exit code to 2 when leftovers are kept (SetErrorLevel 2)',
        })
      }
      // Fail-closed on the swapped form itself: FileFunc passes the path as the
      // first, quoted argument, so a ${GetParent} whose first argument is a bare
      // variable is result-first by construction and writes into it.
      if (block.some((line) => /\$\{GetParent\}\s+\$/.test(line))) {
        problems.push({
          line: i + 1,
          what:
            'a ${GetParent} in the sweep passes a variable as its first argument (FileFunc\'s order is "[path]" $result)',
        })
      }
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
      } else if (branchDividersBetween(trace, i, i + 1 + clearIndex).length > 0) {
        problems.push({
          line: i + 1,
          what: 'iaClearBackupDir sits in a sibling branch of the delete — a path skips the clear',
        })
      }
    }
    // A dead branch around the checks is the `!ifdef NOPE` bypass written in
    // LogicLib's syntax: `${If} 1 == 0` turns the delete and its checks into text
    // that never runs. Only literal comparisons are decided — this is not
    // reachability analysis (round-5 R3/R5).
    const guarded = [
      { line: i, label: 'the delete' },
      ...readHits.map((hit) => ({ line: hit.index, label: `the ${ERRORS} read` })),
      ...(recordHit === undefined ? [] : [{ line: recordHit.index, label: 'the leftover record' }]),
      ...(exitCodeIndex === -1 ? [] : [{ line: exitCodeIndex, label: 'the exit code' }]),
      ...(declinedExitIndex === -1 ? [] : [{ line: declinedExitIndex, label: 'the declined exit code' }]),
      ...(clearIndex === -1 ? [] : [{ line: i + 1 + clearIndex, label: 'iaClearBackupDir' }]),
    ]
    const lastGuarded = Math.max(...guarded.map((target) => target.line))
    for (let j = blockStart + 1; j <= lastGuarded; j += 1) {
      if (!onDeletePath(j)) continue
      const condition = deadBranchText(lines[j])
      if (condition === undefined) continue
      const close = trace.closeOf.get(j) ?? lines.length - 1
      const covered = guarded.filter((target) => target.line > j && target.line <= close)
      if (covered.length === 0) continue
      problems.push({
        line: i + 1,
        what:
          'the constant-false condition `' +
          condition +
          '` covers ' +
          covered.map((target) => target.label).join(' and ') +
          ' — that code can never run',
      })
    }
    // A delete inside a macro body only runs where that macro is inserted: with
    // no `!insertmacro` for it anywhere in the file the delete never runs, and
    // the window's `!macroend` bound made it look complete (round-5 item 4).
    // Fail-closed: the file has to insert the macro after defining it. The
    // uninstaller sweep is the one exception (#904): its `customUnInstall` body
    // is inserted by the electron-builder template, not by our file, and the
    // uninstaller build fails outright if the template's insertion has no macro
    // to expand.
    const macroName = /^\s*!macro\s+(\S+)/i.exec(lines[blockStart])?.[1]
    if (
      policy !== 'sweep' &&
      macroName !== undefined &&
      !lines.some(
        (line, index) =>
          index > blockStart &&
          (INSERT_MACRO.exec(line)?.[1] ?? '').toLowerCase() === macroName.toLowerCase(),
      )
    ) {
      problems.push({
        line: i + 1,
        what: `the delete sits inside !macro ${macroName}, which the file never !insertmacro's — the delete never runs`,
      })
    }
  }
  // #904 (round 6): the sites above are the only recursive deletes these files
  // may have. The rules all bind `RMDir /r "$iaDeleteTarget"`, so a second
  // recursive delete of anything else — a registry-read path, say — used to
  // pass every check while the delete that really runs is the unvalidated one.
  // #919 Q4: the scan reads code only — a `RMDir /r` inside a message string is
  // not a delete — and the prepared-target delete is excluded in its
  // option-carrying spelling too (both measured as false reports).
  let sawRecursiveDelete = false
  for (let i = 0; i < lines.length; i += 1) {
    if (!/RMDir\s+\/r/i.test(outsideStrings(lines[i]))) continue
    sawRecursiveDelete = true
    if (deleteLine.test(lines[i])) continue
    // The fix round's review (P4) measured both spellings of the message being
    // wrong for their statement: `RMDir /r $iaDeleteTarget` (unquoted) is the
    // prepared target, and the old text called it "something other than
    // $iaDeleteTarget" — the truth is that the rules bind the quoted form and
    // the unquoted one is not read (it compiles and deletes).
    problems.push({
      line: i + 1,
      what: /\$iaDeleteTarget\b/i.test(outsideStrings(lines[i]))
        ? "the recursive delete's target is not in the quoted form the rules bind — `RMDir /r $iaDeleteTarget` compiles and deletes, but no rule reads an unquoted target, so this delete is neither validated nor scanned here"
        : 'a recursive delete of something other than $iaDeleteTarget — only a target iaPrepareDelete built may be deleted',
    })
  }
  if (sites === 0 && requireSite) {
    problems.push({
      line: 0,
      what: sawRecursiveDelete
        ? 'no recursive RMDir of the prepared target in the quoted form the rules bind ("$iaDeleteTarget") was found, though this text does carry recursive deletes — the deletes it carries are the statements reported above'
        : 'no recursive RMDir of $iaDeleteTarget (a prepared target) found',
    })
  }
  return problems
}

/**
 * Delete sites whose contract deviates from the strict backup-delete default
 * (#904). The promote site keeps the default — clear, exactly one read, record,
 * clear call. The other two recursive deletes of the shipped scripts have
 * narrower, documented jobs:
 *
 *   - `iarollbackapplication`: it removes the PARTIAL install before renaming
 *     the backup back. The rename that follows is the operation whose failure
 *     has to be visible, so this delete's own error is deliberately not read;
 *     the function must record the backup for the restore-failure path instead.
 *   - `customuninstall`: the uninstaller's leftover sweep. Its enumeration is by
 *     name shape (the records are single slots and the uninstall section may
 *     remove the key), the prompt is the user's say, and a leftover that stays
 *     turns the exit code non-zero instead of being recorded in a key that is
 *     going away. Its enclosing macro is inserted by the electron-builder
 *     template, not by our file, which is why the macro-insertion rule below
 *     does not apply to it.
 */
export const DELETE_SITE_POLICIES = {
  iarollbackapplication: 'partial',
  customuninstall: 'sweep',
}

/** Build-time assertion for `unguardedBackupDelete` (#901 / R1, #904). */
export function validateBackupDeleteGuards(source, filename, options = {}) {
  const problems = unguardedBackupDelete(source, options)
  if (problems.length > 0) {
    const where = problems
      .map((p) => (p.line === 0 ? p.what : `line ${p.line}: ${p.what}`))
      .join('; ')
    throw new Error(`${filename}: unguarded backup delete — ${where}`)
  }
}

/**
 * Build-time assertion for the #904 cleanup primitives (installer-cleanup.nsh).
 *
 * The delete-site rules are only as strong as the helpers behind them, and the
 * site scan cannot see into an `!include`d helper's body: if the shape check,
 * the reparse scan, the long-path form or the prepare wrapper went away, every
 * site would still read as clean while the target is neither validated nor
 * scanned. Everything here is therefore fail-closed: each primitive has to be
 * present by name, the reparse mask and the UNC form have to be in the text,
 * and the file may not grow a recursive delete of its own (that is the sites'
 * job, where the guard can see its window).
 */
export function validateCleanupHelpers(source, filename) {
  const code = stripNsisComments(source)
  const required = [
    {
      what: 'Function iaCheckBackupShape',
      re: /^\s*Function\s+iaCheckBackupShape\b/im,
    },
    {
      what: 'the hex set of the shape check',
      re: /"0123456789abcdefABCDEF"/i,
    },
    {
      what: 'the ".old-{" literal of the shape check (braces included: that is the name System::Call\'s "g" GUID form produces)',
      re: /"\.old-\{"/,
    },
    {
      what: 'Function iaBuildLongPath',
      re: /^\s*Function\s+iaBuildLongPath\b/im,
    },
    {
      what: 'the "\\\\?\\UNC\\" long-path form for UNC paths',
      re: /\\\\\?\\UNC\\/i,
    },
    {
      what: 'Function iaScanReparsePoints',
      re: /^\s*Function\s+iaScanReparsePoints\b/im,
    },
    {
      what: 'the 0x400 FILE_ATTRIBUTE_REPARSE_POINT mask',
      re: /0x400/i,
    },
    {
      what: 'FindClose on the scan paths',
      re: /^\s*FindClose\b/im,
    },
    {
      what: 'Function iaPrepareDelete',
      re: /^\s*Function\s+iaPrepareDelete\b/im,
    },
    {
      what: 'the refusal that clears the delete target',
      re: /StrCpy\s+\$iaDeleteTarget\s+""/i,
    },
  ]
  const problems = required.filter(({ re }) => !re.test(code)).map(({ what }) => `missing ${what}`)
  if (/RMDir\s+\/r/im.test(code)) {
    problems.push('a recursive delete inside the helper file — deletes belong to the sites the guard inspects')
  }
  if (problems.length > 0) {
    throw new Error(`${filename}: cleanup helpers incomplete — ${problems.join('; ')}`)
  }
}

/**
 * Validate every NSIS script that runs inside `customHeader`: installer.nsh,
 * the installer-cleanup.nsh it `!include`s unconditionally, and the
 * installer-directories.nsh it includes for the installer build only. All three
 * are compiled after addLangs, so all three share the #831 hazard; the #901
 * long-path guard applies to all three. The backup-delete guard runs on the two
 * files that hold delete sites (installer-cleanup.nsh may not hold one at all),
 * with the #904 site policies applied, and the cleanup primitives themselves are
 * asserted separately.
 */
/** The `!include`s the three scanned installer scripts may carry. */
const KNOWN_INSTALLER_INCLUDES = new Set([
  'logiclib.nsh',
  'filefunc.nsh',
  'installer-cleanup.nsh',
  'installer-directories.nsh',
])

export function validateInstallerScripts(installerDir) {
  for (const file of ['installer.nsh', 'installer-cleanup.nsh', 'installer-directories.nsh']) {
    const source = readFileSync(join(installerDir, file), 'utf8')
    // A fourth file's text is not read by this guard, so an `!include` that can
    // point at one is a recursive delete none of the three scans sees (the fix
    // round's review, P3: a file holding its own unguarded `RMDir /r
    // "$iaDeleteTarget"`, included from a site file, passed). The set is the
    // includes the shipped scripts use, matched by basename.
    for (const match of source.matchAll(/^\s*!\s*include\s+(.+?)\s*$/gim)) {
      const spec = match[1].trim().replace(/^["']|["']$/g, '')
      const base = spec.split(/[\\/]/).pop().toLowerCase()
      if (!KNOWN_INSTALLER_INCLUDES.has(base)) {
        throw new Error(
          `${file}: !include "${spec}" is not one of the scanned installer files — its text is not read by this guard, so a recursive delete it carries is invisible`,
        )
      }
    }
    validateLangStringGuards(source, file)
    validateLongPathPrefixes(source, file)
  }
  for (const file of ['installer-directories.nsh', 'installer.nsh']) {
    validateBackupDeleteGuards(
      readFileSync(join(installerDir, file), 'utf8'),
      file,
      { sitePolicies: DELETE_SITE_POLICIES },
    )
  }
  validateCleanupHelpers(
    readFileSync(join(installerDir, 'installer-cleanup.nsh'), 'utf8'),
    'installer-cleanup.nsh',
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
