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
 *   - the compiled desktop app (npm run build -> dist/)
 *
 * Resource layout (PORT DESIGN from OpenHands electron-builder.config.mjs,
 * MIT, commit b0a1a2d1368a50a890b69ef45c54e1a74140b677 — asar:false so
 * spawned files are real on disk; extraResources for runtimes; NSIS
 * perMachine:false):
 *   <install>/Intelligence Agent.exe
 *   <install>/resources/app/...          (this package)
 *   <install>/resources/python/python.exe (bundled runtime, from the lockfile)
 * User data is NOT in the install dir: %APPDATA%\\intelligence-agent.
 */
import { createHash } from 'node:crypto'
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
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
}

/** SHA-256 hex of a file. */
export function sha256File(path) {
  return createHash('sha256').update(readFileSync(path)).digest('hex')
}

/** The `${LANG_<NAME>}` symbol a LangString line references, or null. */
function langStringSymbol(line) {
  const match = /^\s*LangString\s+\S+\s+\$\{LANG_([A-Z0-9_]+)\}/.exec(line)
  return match ? match[1] : null
}

/**
 * Open a conditional-compilation frame. Only `!ifdef` / `!ifndef` carry
 * symbols; every opener is pushed so `!endif` pops the right frame.
 */
function conditionalFrame(line) {
  const match = /^\s*!(if|ifdef|ifndef|ifmacrodef|ifmacrondef)\b(.*)$/.exec(line)
  if (!match) return null
  const kind = match[1]
  const symbols =
    kind === 'ifdef' || kind === 'ifndef'
      ? new Set(match[2].match(/[A-Za-z_][A-Za-z0-9_]*/g) ?? [])
      : new Set()
  return { kind, symbols }
}

/**
 * Every `LangString <name> ${LANG_<X>}` in an NSIS script that is NOT enclosed
 * in a matching `!ifdef LANG_<X>` block, as `{ line, symbol }` (line is 1-based).
 *
 * #831: the stock NSIS template inserts `customHeader` right after
 * `!insertmacro addLangs`, so the `${LANG_*}` symbols defined by
 * `LoadLanguageFile` are visible there. A single-language installer
 * (`installerLanguages: [language]`) does not load the other language, so an
 * unguarded `LangString ... ${LANG_SIMPCHINESE}` makes makensis emit warning
 * 7025 — fatal, because electron-builder runs makensis with warnings-as-errors.
 * The `!else` branch of an enclosing guard drops its symbols (fail-closed).
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
 * Build-time assertion (#831): every `LangString <name> ${LANG_<X>}` in the
 * installer script must sit inside a matching `!ifdef LANG_<X>` guard, so a
 * language-set bug fails the build instead of aborting deep inside makensis.
 */
export function validateLangStringGuards(source) {
  const bad = unguardedLangStrings(source)
  if (bad.length > 0) {
    const where = bad.map((b) => `line ${b.line}: ${b.symbol}`).join(', ')
    throw new Error(`installer.nsh: LangString not guarded by !ifdef LANG_<NAME> (${where})`)
  }
}

/**
 * Pure electron-builder configuration for the Windows x64 installer.
 * Kept pure (no electron-builder import) so it is unit-testable on any OS.
 */
export function createWindowsInstallerConfig({ version, appId, productName, installerDir }) {
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
    ],
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
    },
  }
}

function loadLockfile() {
  const path = join(installerDir, 'python-runtime.lock.json')
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
  console.log('lockfile OK:', lock.python.implementation, lock.python.version, `(${lock.wheels.length} pinned wheels)`)

  const pkg = JSON.parse(readFileSync(join(desktopDir, 'package.json'), 'utf8'))
  const config = createWindowsInstallerConfig({
    version: pkg.version,
    appId: 'com.intelligence-agent.desktop',
    productName: 'Intelligence Agent',
    installerDir,
  })
  config.directories.output = outDir

  for (const required of ['installer.nsh', 'installer-directories.nsh', 'python-runtime.lock.json']) {
    if (!existsSync(join(installerDir, required))) {
      throw new Error(`missing installer input: ${required}`)
    }
  }
  // #831: fail early when a LangString is not guarded by its language.
  const installerNsh = join(installerDir, 'installer.nsh')
  validateLangStringGuards(readFileSync(installerNsh, 'utf8'))
  console.log('installer LangString guards OK')
  console.log('installer inputs OK')

  if (compileOnly) {
    console.log('compile-only: config valid, NSIS includes present')
    return
  }
  if (process.platform !== 'win32' || process.arch !== 'x64') {
    throw new Error('full installer build requires Windows x64 (use --compile-only elsewhere)')
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
  const { readdirSync } = await import('node:fs')
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
