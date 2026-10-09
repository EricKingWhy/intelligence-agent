/**
 * W-21 D2 (#812): the bundled runtime must contain the product, not just its
 * dependency closure. Pure unit tests for the lockfile product pin, the probe
 * and the afterPack assertion (the runner is injected, so no Windows/python
 * needed here; the real red→green run is in the #812 evidence).
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, readFileSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  assertRuntimeProduct,
  assertStagedProductMatchesSource,
  createWindowsInstallerConfig,
  productCodeDigest,
  runtimeProductProbe,
  validateProductPin,
  validateRuntimeLockfile,
  GOOD_LOCK,
} from '../scripts/build-windows-installer.mjs'

const desktopDir = join(dirname(fileURLToPath(import.meta.url)), '..')
const repoRoot = join(desktopDir, '..')
const lockfile = JSON.parse(
  readFileSync(join(desktopDir, 'installer', 'python-runtime.lock.json'), 'utf8'),
)

/** Extract one INI-ish table from pyproject.toml text. */
function pyprojectSection(text, header) {
  const start = text.indexOf(`[${header}]`)
  assert.notEqual(start, -1, `pyproject.toml has no [${header}]`)
  const body = text.slice(start + header.length + 2)
  const end = body.indexOf('\n[')
  return end === -1 ? body : body.slice(0, end)
}

describe('runtime lockfile product pin', () => {
  it('accepts the lockfile shipped in desktop/installer/', () => {
    validateRuntimeLockfile(lockfile)
  })

  it('agrees with pyproject.toml (name, version, module)', () => {
    const pyproject = readFileSync(join(repoRoot, 'pyproject.toml'), 'utf8')
    const project = pyprojectSection(pyproject, 'project')
    const buildBackend = pyprojectSection(pyproject, 'tool.uv.build-backend')
    const version = /^version = "(.+)"$/m.exec(project)?.[1]
    const name = /^name = "(.+)"$/m.exec(project)?.[1]
    const module = /^module-name = "(.+)"$/m.exec(buildBackend)?.[1]
    assert.equal(lockfile.product.name, name)
    assert.equal(lockfile.product.version, version)
    assert.equal(lockfile.product.module, module)
  })

  it('rejects a missing product pin', () => {
    const bad = JSON.parse(JSON.stringify(GOOD_LOCK))
    delete bad.product
    assert.throws(() => validateRuntimeLockfile(bad), /missing "product" pin/)
  })

  it('rejects a wheel filename that does not match the pinned version', () => {
    const bad = JSON.parse(JSON.stringify(GOOD_LOCK))
    bad.product.version = '1.0.1'
    assert.throws(() => validateRuntimeLockfile(bad), /does not match product\.version/)
  })

  it('rejects a wheel filename that does not match the pinned name', () => {
    const bad = JSON.parse(JSON.stringify(GOOD_LOCK))
    bad.product.wheel = 'other_agent-1.0.0-py3-none-any.whl'
    assert.throws(() => validateRuntimeLockfile(bad), /does not match product\.name/)
  })

  it('rejects a non-wheel product artifact', () => {
    const bad = JSON.parse(JSON.stringify(GOOD_LOCK))
    bad.product.wheel = 'intelligence_agent-1.0.0.tar.gz'
    assert.throws(() => validateRuntimeLockfile(bad), /not a wheel filename/)
  })

  it('rejects a module that is not a dotted Python path', () => {
    assert.throws(
      () => validateProductPin({ ...GOOD_LOCK.product, module: 'agent-harness' }),
      /not a dotted Python module path/,
    )
  })
})

describe('runtimeProductProbe', () => {
  it('imports the pinned module CLI and prints the pinned distribution', () => {
    const probe = runtimeProductProbe(lockfile.product)
    assert.match(probe, /import_module\("agent_harness" \+ '\.cli'\)/)
    assert.match(probe, /callable\(mod\.main\)/)
    assert.match(probe, /m\.version\("intelligence-agent"\)/)
  })
})

describe('assertRuntimeProduct', () => {
  it('throws with the staging recipe when the product is not installed', () => {
    const run = () => ({
      status: 1,
      stdout: '',
      stderr: 'Traceback (most recent call last):\nModuleNotFoundError: No module named \'agent_harness\'\n',
    })
    assert.throws(
      () => assertRuntimeProduct({ pythonExe: 'python.exe', product: lockfile.product, run }),
      (error) =>
        /lacks the product package intelligence-agent==1\.0\.0/.test(error.message) &&
        /ModuleNotFoundError/.test(error.message) &&
        /prepare_python_runtime\.py/.test(error.message),
    )
  })

  it('throws when the bundled version drifts from the pin', () => {
    const run = () => ({ status: 0, stdout: '0.9.0\n', stderr: '' })
    assert.throws(
      () => assertRuntimeProduct({ pythonExe: 'python.exe', product: lockfile.product, run }),
      /bundled intelligence-agent is 0\.9\.0, python-runtime\.lock\.json pins 1\.0\.0/,
    )
  })

  it('throws when the bundled interpreter cannot start at all', () => {
    const run = () => ({ error: new Error('ENOENT'), status: null, stdout: '', stderr: '' })
    assert.throws(
      () => assertRuntimeProduct({ pythonExe: 'missing.exe', product: lockfile.product, run }),
      /bundled python failed to start/,
    )
  })

  it('passes the probe to the staged interpreter and returns the version', () => {
    const calls = []
    const run = (exe, args, options) => {
      calls.push({ exe, args, options })
      return { status: 0, stdout: '1.0.0\n', stderr: '' }
    }
    assert.equal(
      assertRuntimeProduct({ pythonExe: 'C:\\stage\\python.exe', product: lockfile.product, run }),
      '1.0.0',
    )
    assert.equal(calls.length, 1)
    assert.equal(calls[0].exe, 'C:\\stage\\python.exe')
    assert.equal(calls[0].args[0], '-c')
    assert.equal(calls[0].args[1], runtimeProductProbe(lockfile.product))
  })

  it('throws when the lockfile carries no product pin', () => {
    assert.throws(() => assertRuntimeProduct({ pythonExe: 'python.exe' }), /missing "product" pin/)
  })
})

describe('afterPack runtime assertion', () => {
  const config = createWindowsInstallerConfig({
    version: '0.1.0',
    appId: 'com.intelligence-agent.desktop',
    productName: 'Intelligence Agent',
    installerDir: join(desktopDir, 'installer'),
    runtimeProduct: lockfile.product,
  })

  it('fails the build when no runtime was staged', async () => {
    const appOutDir = mkdtempSync(join(tmpdir(), 'ia-afterpack-'))
    mkdirSync(join(appOutDir, 'resources'), { recursive: true })
    await assert.rejects(
      () => config.afterPack({ electronPlatformName: 'win32', appOutDir }),
      /bundled python runtime missing/,
    )
  })
})

/** In-memory package tree keyed by absolute path, for the injectable filesystem. */
function fakeTree(files) {
  const normalize = (path) => path.replaceAll('\\', '/')
  const entries = new Map(Object.entries(files).map(([path, content]) => [normalize(path), content]))
  return {
    readFile: (path) => {
      const key = normalize(path)
      if (!entries.has(key)) throw Object.assign(new Error(`ENOENT: ${key}`), { code: 'ENOENT' })
      return Buffer.from(entries.get(key))
    },
    readdir: (dir) => {
      const prefix = `${normalize(dir).replace(/\/$/, '')}/`
      const names = new Map()
      for (const path of entries.keys()) {
        if (!path.startsWith(prefix)) continue
        const rest = path.slice(prefix.length)
        const slash = rest.indexOf('/')
        names.set(slash === -1 ? rest : rest.slice(0, slash), slash !== -1)
      }
      if (names.size === 0) throw Object.assign(new Error(`ENOENT: ${prefix}`), { code: 'ENOENT' })
      return [...names.entries()].map(([name, isDirectory]) => ({ name, isDirectory: () => isDirectory }))
    },
  }
}

/**
 * W-21 D9 (#835): a staged runtime whose product code lags the checkout used to pass
 * every existing check — the distribution version never moves between commits — and
 * the installer shipped it. These tests cover the content comparison that closes it.
 */
describe('assertStagedProductMatchesSource', () => {
  const sourceDir = join('C:\\repo', 'src', 'agent_harness')
  const stagedDir = join('C:\\stage', 'Lib', 'site-packages', 'agent_harness')
  const cliSource = 'def main():\n    return 0\n'
  const appSource = 'def mount_static(app, web_dist_dir=None):\n    ...\n'
  const sourceFiles = {
    [join(sourceDir, 'cli.py')]: cliSource,
    [join(sourceDir, 'web', 'app.py')]: appSource,
  }

  it('accepts a staged copy that matches the checkout', () => {
    assertStagedProductMatchesSource({
      sourceDir,
      stagedDir,
      ...fakeTree({
        ...sourceFiles,
        [join(stagedDir, 'cli.py')]: cliSource,
        [join(stagedDir, 'web', 'app.py')]: appSource,
      }),
    })
  })

  it('rejects the same version with drifted module content', () => {
    assert.throws(
      () => assertStagedProductMatchesSource({
        sourceDir,
        stagedDir,
        ...fakeTree({
          ...sourceFiles,
          [join(stagedDir, 'cli.py')]: cliSource,
          // The pre-D5 web/app.py: same distribution version, older code.
          [join(stagedDir, 'web', 'app.py')]: 'def mount_static(app, web_dist_dir=None):\n    pass\n',
        }),
      }),
      (error) => {
        assert.match(error.message, /staged product differs from the checkout/)
        assert.match(error.message, /web\/app\.py/)
        assert.match(error.message, /python desktop\/scripts\/prepare_python_runtime\.py/)
        return true
      },
    )
  })

  it('rejects a staged tree that is missing a module', () => {
    assert.throws(
      () => assertStagedProductMatchesSource({
        sourceDir,
        stagedDir,
        ...fakeTree({ ...sourceFiles, [join(stagedDir, 'cli.py')]: cliSource }),
      }),
      /web\/app\.py \(missing\)/,
    )
  })

  it('ignores __pycache__ on both sides', () => {
    assertStagedProductMatchesSource({
      sourceDir,
      stagedDir,
      ...fakeTree({
        ...sourceFiles,
        [join(sourceDir, '__pycache__', 'cli.cpython-312.pyc')]: 'binary',
        [join(stagedDir, 'cli.py')]: cliSource,
        [join(stagedDir, 'web', 'app.py')]: appSource,
        [join(stagedDir, '__pycache__', 'cli.cpython-312.pyc')]: 'binary',
      }),
    })
  })

  it('rejects a source tree with no modules at all', () => {
    assert.throws(
      () => assertStagedProductMatchesSource({ sourceDir, stagedDir, ...fakeTree({}) }),
      /no product modules under/,
    )
  })

  // The real trees: green only while installer/staging is refreshed after every
  // Python change (`python desktop/scripts/prepare_python_runtime.py`).
  const stagedPython = join(desktopDir, 'installer', 'staging', 'python', 'python.exe')
  it('matches the checkout for the staged runtime on this machine', {
    skip: existsSync(stagedPython) ? false : 'no staged Python runtime on this machine',
  }, () => {
    assertStagedProductMatchesSource({
      sourceDir: join(repoRoot, 'src', 'agent_harness'),
      stagedDir: join(desktopDir, 'installer', 'staging', 'python', 'Lib', 'site-packages', 'agent_harness'),
    })
  })
})

describe('productCodeDigest', () => {
  const root = join('C:\\repo', 'src', 'agent_harness')

  it('is stable across directory listing order and sensitive to content', () => {
    const a = productCodeDigest(root, fakeTree({
      [join(root, 'cli.py')]: 'a',
      [join(root, 'web', 'app.py')]: 'b',
    }))
    const b = productCodeDigest(root, fakeTree({
      [join(root, 'web', 'app.py')]: 'b',
      [join(root, 'cli.py')]: 'a',
    }))
    const c = productCodeDigest(root, fakeTree({
      [join(root, 'cli.py')]: 'a',
      [join(root, 'web', 'app.py')]: 'c',
    }))
    assert.match(a, /^[0-9a-f]{64}$/)
    assert.equal(a, b)
    assert.notEqual(a, c)
  })

  it('rejects a tree with no modules', () => {
    assert.throws(() => productCodeDigest(root, fakeTree({})), /no Python modules under/)
  })
})
