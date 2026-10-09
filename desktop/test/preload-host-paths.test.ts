/**
 * #826（MM-05）AC1/AC3：宿主路径桥的**行为**，以及它与消费侧的跨包漂移守卫。
 *
 * 为什么这里能真的跑 preload：`preload.cts` 是自包含 CJS（沙箱 renderer 没有模块解析），
 * `node:module` 的 `stripTypeScriptTypes(src, { mode: 'transform' })` 去掉类型后就是一段
 * 可执行 CJS（`import electron = require('electron')` → `const electron = require(...)`）。
 * 放进 `vm` 上下文并配一份**假的** `require('electron')`，就能在不打开任何窗口、不装
 * Electron 的前提下断言三件事：谁拿到桥、桥暴露了什么、`pathFor` 到底转给了谁。
 *
 * 需要 Node ≥ 22.13（`stripTypeScriptTypes`）；`desktop/package.json` 的 engines 是
 * `>= 22`，CI 的桌面车道用 Node 22 最新档。
 *
 * 关键字面量的**跨包**一致性（preload 写哪个全局 / web 应用读哪个全局）由本文件末尾的
 * 守卫钉住：preload 不能 import 任何东西，字面量必然是两份，靠这条守卫防它们各写各的。
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { stripTypeScriptTypes } from 'node:module'
import vm from 'node:vm'

import { HOST_PATHS_GLOBAL, SERVICE_ORIGIN_SWITCH } from '../src/ipc.ts'

const desktopDir = join(import.meta.dirname, '..')
const ORIGIN = 'http://127.0.0.1:5173'
const preloadSource = readFileSync(join(desktopDir, 'src', 'preload.cts'), 'utf8')

/**
 * `@types/node` 26.6.4 still declares `mode` as `'strip'` only, while the runtime accepts
 * `'transform'` (measured on Node 24.20: it is the mode that converts `import x = require('y')`,
 * which strip-only refuses). Named assertion with one reason; delete it when the types catch up.
 */
const transformToCjs = stripTypeScriptTypes as unknown as (
  code: string,
  options: { mode: 'transform' },
) => string

const preloadCode = transformToCjs(preloadSource, { mode: 'transform' })

/** Files with a disk backend — the only ones Electron's `webUtils` answers a path for. */
const diskBacked = new WeakMap<File, string>()

function diskFile(path: string): File {
  const name = path.split(/[/\\]/).pop() ?? path
  const file = new File([new Uint8Array(0)], name, { type: 'application/pdf' })
  diskBacked.set(file, path)
  return file
}

interface Renderer {
  /** What `contextBridge.exposeInMainWorld` was asked to publish, by global name. */
  exposed: Record<string, unknown>
  /** Files the bridge handed to `webUtils.getPathForFile`. */
  asked: File[]
}

/** Run the real preload against a faked Electron for one renderer framing. */
function runPreload(args: { origin: string; isMainFrame?: boolean }): Renderer {
  const exposed: Record<string, unknown> = {}
  const asked: File[] = []
  const electron = {
    ipcRenderer: { invoke: () => Promise.resolve(null) },
    contextBridge: {
      exposeInMainWorld: (name: string, api: unknown) => {
        exposed[name] = api
      },
    },
    webUtils: {
      // Electron answers '': a File with no disk backend (clipboard bytes, a File
      // built in page script) has no path. Modelled, not asserted, by the fake.
      getPathForFile: (file: File) => {
        asked.push(file)
        return diskBacked.get(file) ?? ''
      },
    },
  }
  vm.runInNewContext(preloadCode, {
    require: (request: string) => {
      assert.equal(request, 'electron', `the preload must import nothing but electron (got ${request})`)
      return electron
    },
    process: { argv: [`${SERVICE_ORIGIN_SWITCH}${ORIGIN}`], isMainFrame: args.isMainFrame ?? true },
    location: { origin: args.origin },
  })
  return { exposed, asked }
}

/**
 * The host-path lookup as the page would call it. Narrowed at runtime instead of
 * asserted: a missing or malformed bridge must fail these tests with a readable
 * message, not be cast past into a `TypeError` deeper down.
 */
function pathLookup(exposed: Record<string, unknown>): (file: File) => unknown {
  const bridge = exposed[HOST_PATHS_GLOBAL]
  if (typeof bridge !== 'object' || bridge === null) throw new Error('the host-path global was not exposed')
  if (!('pathFor' in bridge)) throw new Error('the host-path bridge carries no pathFor')
  const pathFor = bridge.pathFor
  if (typeof pathFor !== 'function') throw new Error('pathFor is not callable')
  return (file: File) => pathFor.call(bridge, file)
}

describe('preload host-path bridge (#826 AC1/AC3)', () => {
  it('exposes exactly two globals on the owned page, and the path lookup is one method', () => {
    const { exposed } = runPreload({ origin: ORIGIN })
    assert.deepEqual(Object.keys(exposed).sort(), [HOST_PATHS_GLOBAL, 'iaDesktop'].sort())
    const bridge = exposed[HOST_PATHS_GLOBAL]
    assert.ok(typeof bridge === 'object' && bridge !== null)
    assert.deepEqual(Object.keys(bridge), ['pathFor'])
  })

  it('answers the real path for a file the user picked or dropped', () => {
    const { exposed, asked } = runPreload({ origin: ORIGIN })
    const dropped = diskFile('C:\\Users\\u\\notes.pdf')
    assert.equal(pathLookup(exposed)(dropped), 'C:\\Users\\u\\notes.pdf')
    // Delegation, not a lookup of our own: Electron's helper saw this very File.
    assert.deepEqual(asked, [dropped])
  })

  it('answers nothing for a File with no disk backend (AC3 negative: no path for an unselected file)', () => {
    const { exposed } = runPreload({ origin: ORIGIN })
    // A File built in page script: the renderer holds the object, yet it has no
    // path — the bridge is not an oracle for files the user never selected.
    assert.equal(pathLookup(exposed)(new File([new Uint8Array(0)], 'C:\\Users\\u\\secret.txt')), '')
  })

  it('exposes no filesystem surface and not the webUtils object itself', () => {
    const { exposed } = runPreload({ origin: ORIGIN })
    const bridge = exposed[HOST_PATHS_GLOBAL]
    assert.ok(typeof bridge === 'object' && bridge !== null)
    for (const forbidden of ['fs', 'readFile', 'readFileSync', 'readdir', 'open', 'webUtils', 'ipcRenderer', 'path']) {
      assert.equal(forbidden in bridge, false, `${forbidden} must not be reachable through the host-path global`)
    }
    assert.deepEqual(Object.keys(exposed).filter((name) => /utils|fs|electron/i.test(name)), [])
  })

  it('gives a foreign page and a subframe no host-path lookup at all', () => {
    for (const framing of [{ origin: 'https://example.com' }, { origin: ORIGIN, isMainFrame: false }]) {
      const { exposed } = runPreload(framing)
      assert.equal(HOST_PATHS_GLOBAL in exposed, false, `no host-path bridge for ${JSON.stringify(framing)}`)
      // The restricted bridge keeps its designed shape: version marker only.
      // (Compared through JSON: these objects come from another realm, so their
      // prototype differs and `deepStrictEqual` would reject an identical literal.)
      assert.equal(JSON.stringify(exposed['iaDesktop']), '{"protocolVersion":1}')
    }
  })

  it('reads the same global name the web app looks for (cross-package drift guard)', () => {
    const consumer = readFileSync(join(desktopDir, '..', 'web', 'src', 'lib', 'hostFiles.ts'), 'utf8')
    assert.ok(
      consumer.includes(`'${HOST_PATHS_GLOBAL}'`),
      `web/src/lib/hostFiles.ts must read the global this preload writes (${HOST_PATHS_GLOBAL}); ` +
        'a rename on one side silently downgrades every desktop drop to an upload',
    )
    assert.ok(preloadSource.includes(`'${HOST_PATHS_GLOBAL}'`))
  })
})
