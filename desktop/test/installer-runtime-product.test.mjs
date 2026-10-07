/**
 * W-21 D2 (#812): the bundled runtime must contain the product, not just its
 * dependency closure. Pure unit tests for the lockfile product pin, the probe
 * and the afterPack assertion (the runner is injected, so no Windows/python
 * needed here; the real red→green run is in the #812 evidence).
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  assertRuntimeProduct,
  createWindowsInstallerConfig,
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
