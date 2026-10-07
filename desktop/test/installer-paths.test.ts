/**
 * #361 [W-16]: Windows installer path resolution unit tests.
 * Pure functions; Windows paths are built with backslash joins so the
 * tests run on any platform (no node:path, no process.env).
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import {
  installerAppId,
  installerProductName,
  resolveUserDataDir,
  defaultInstallDir,
  uninstallRegistryKey,
  resolvePythonPath,
} from '../src/installer/paths.ts'

describe('installer identity', () => {
  it('appId is stable (registry key + installer GUID derive from it)', () => {
    assert.equal(installerAppId(), 'com.intelligence-agent.desktop')
  })

  it('productName is stable', () => {
    assert.equal(installerProductName(), 'Intelligence Agent')
  })
})

describe('resolveUserDataDir', () => {
  it('places user data under %APPDATA%\\intelligence-agent', () => {
    assert.equal(
      resolveUserDataDir('C:\\Users\\x\\AppData\\Roaming'),
      'C:\\Users\\x\\AppData\\Roaming\\intelligence-agent',
    )
  })

  it('throws when APPDATA is missing', () => {
    assert.throws(() => resolveUserDataDir(undefined), /APPDATA/)
    assert.throws(() => resolveUserDataDir(''), /APPDATA/)
  })
})

describe('defaultInstallDir', () => {
  it('installs per-user under %LOCALAPPDATA%\\Programs', () => {
    assert.equal(
      defaultInstallDir('C:\\Users\\x\\AppData\\Local', 'Intelligence Agent'),
      'C:\\Users\\x\\AppData\\Local\\Programs\\Intelligence Agent',
    )
  })

  it('throws when LOCALAPPDATA is missing', () => {
    assert.throws(() => defaultInstallDir(undefined, 'Intelligence Agent'), /LOCALAPPDATA/)
  })
})

describe('uninstallRegistryKey', () => {
  it('builds the per-user uninstall key from the app id', () => {
    assert.equal(
      uninstallRegistryKey('com.intelligence-agent.desktop'),
      'Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\com.intelligence-agent.desktop',
    )
  })
})

describe('resolvePythonPath', () => {
  const dev = (execPath: string) => ({
    platform: 'linux',
    execPath,
    resourcesPath: '/r',
    existsSync: (_p: string) => false,
  })

  it('prefers the bundled runtime when it exists (packaged app)', () => {
    const deps = {
      platform: 'win32',
      execPath: 'C:\\Apps\\Intelligence Agent\\Intelligence Agent.exe',
      resourcesPath: 'C:\\Apps\\Intelligence Agent\\resources',
      existsSync: (p: string) => p === 'C:\\Apps\\Intelligence Agent\\resources\\python\\python.exe',
    }
    assert.equal(
      resolvePythonPath(deps),
      'C:\\Apps\\Intelligence Agent\\resources\\python\\python.exe',
    )
  })

  it('falls back to the dev heuristic when no runtime is bundled', () => {
    assert.equal(
      resolvePythonPath({
        platform: 'win32',
        execPath: 'C:\\Apps\\Intelligence Agent\\Intelligence Agent.exe',
        resourcesPath: 'C:\\Apps\\Intelligence Agent\\resources',
        existsSync: (_p: string) => false,
      }),
      'C:\\Apps\\Intelligence Agent\\Intelligence Agent.exe',
    )
    assert.equal(
      resolvePythonPath(dev('/home/u/repo/node_modules/electron/dist/electron')),
      '/home/u/repo/node_modules/electron/dist/python',
    )
  })
})
