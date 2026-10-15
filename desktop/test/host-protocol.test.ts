/**
 * #914 ruling D2: `protocol_version` is the shell's only compatibility gate.
 *
 * The ruling behind #914 decided two things this file pins. First, an older
 * service answering a newer client is designed behaviour while
 * `protocol_version` matches, so the product version string must never become a
 * second gate — the shell must classify such a service as `ready`. Second, a
 * change that breaks the wire contract has to bump the version on BOTH ends;
 * nothing else in the tree pins the shell constant to the Python authority, and
 * the two are maintained by hand, so the parity and the rule's presence are
 * asserted here (ADR-0053).
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { HOST_PROTOCOL_VERSION, classifyHealth } from '../src/host-protocol.ts'

const SHELL_SOURCE = readFileSync(new URL('../src/host-protocol.ts', import.meta.url), 'utf8')
/** The server-side authority the shell mirrors (`desktop/` sits inside the repo). */
const PYTHON_SOURCE = readFileSync(new URL('../../src/agent_harness/host_service.py', import.meta.url), 'utf8')

/** Text around a declaration, used to check the rule is still documented next to it. */
function declarationDoc(source: string, declaration: RegExp, radius: number): string {
  const match = declaration.exec(source)
  assert.ok(match, `declaration not found in source: ${String(declaration)}`)
  const start = Math.max(0, match.index - radius)
  return source.slice(start, match.index + match[0].length)
}

describe('classifyHealth (#914 D2)', () => {
  it('accepts a service on the same protocol whatever product version it reports', () => {
    const verdict = classifyHealth(200, {
      status: 'ok',
      protocol_version: HOST_PROTOCOL_VERSION,
      version: '0.0.1-older-than-the-shell',
    })
    if (verdict.kind !== 'ready') {
      throw new Error(`expected ready, got ${verdict.kind}: ${verdict.reason}`)
    }
    assert.deepEqual(verdict.payload, {
      version: '0.0.1-older-than-the-shell',
      protocolVersion: HOST_PROTOCOL_VERSION,
    })
  })

  it('rejects another protocol version even when the product version matches', () => {
    const verdict = classifyHealth(200, {
      status: 'ok',
      protocol_version: HOST_PROTOCOL_VERSION + 1,
      version: '1.0.0',
    })
    if (verdict.kind !== 'incompatible') {
      throw new Error(`expected incompatible, got ${verdict.kind}`)
    }
    assert.match(verdict.reason, /协议版本不符/)
  })

  it('never reports ready when protocol_version is missing or not an integer', () => {
    for (const body of [
      { status: 'ok', version: '1.0.0' },
      { status: 'ok', protocol_version: '1', version: '1.0.0' },
      { status: 'ok', protocol_version: 1.5, version: '1.0.0' },
    ]) {
      assert.equal(classifyHealth(200, body).kind, 'not-ready')
    }
  })
})

describe('HOST_PROTOCOL_VERSION (#914 D2)', () => {
  it('equals the Python authority', () => {
    const match = /^HOST_PROTOCOL_VERSION = (\d+)$/m.exec(PYTHON_SOURCE)
    assert.ok(match, 'HOST_PROTOCOL_VERSION not found in src/agent_harness/host_service.py')
    assert.equal(HOST_PROTOCOL_VERSION, Number(match[1]))
  })

  it('keeps the bump rule documented at both ends', () => {
    // A text pin, not a behaviour pin: the rule is what makes the version a
    // usable gate, so deleting it from either constant must fail a test.
    const pythonDoc = declarationDoc(PYTHON_SOURCE, /^HOST_PROTOCOL_VERSION = \d+$/m, 600)
    assert.match(pythonDoc, /递增/)
    assert.match(pythonDoc, /唯一/)
    const shellDoc = declarationDoc(SHELL_SOURCE, /^export const HOST_PROTOCOL_VERSION = \d+$/m, 900)
    assert.match(shellDoc, /MUST bump/)
    assert.match(shellDoc, /never a gate/)
  })
})
