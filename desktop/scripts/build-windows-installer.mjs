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
 * Pure electron-builder configuration for the Windows x64 installer.
 * Kept pure (no electron-builder import) so it is unit-testable on any OS.
 */
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
  assertNsisIncludePlacement(
    readFileSync(join(installerDir, 'installer.nsh'), 'utf8'),
    'installer-directories.nsh',
  )
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
  // …and for the staged runtime the TUI runs on.
  const stagedNode = join(installerDir, 'staging', nodeLock.layout.executable)
  if (!existsSync(stagedNode)) {
    throw new Error(
      `bundled node runtime not staged: ${stagedNode} — ` +
        'run `python scripts/prepare_node_runtime.py` (installer/README.md)',
    )
  }
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
