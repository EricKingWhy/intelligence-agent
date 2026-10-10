/**
 * #891 P2: the shell-owned loopback proxy must verify its caller (token
 * injection gateway). Written RED against the current implementation, which
 * forwards every request with `Bearer <host token>` and checks nothing.
 *
 * Contract under test (ADAPTED from the DSH browser-auth shape, 5badb150):
 *   - a single unguessable launch token on `GET /` exchanges for an
 *     authority-bound, HMAC-signed, HttpOnly/Strict session cookie and 302s to
 *     a clean path (the token never survives in the address bar);
 *   - every later request (HTTP and the WebSocket upgrade) must carry that
 *     cookie; anything else is refused fail-closed and never reaches upstream;
 *   - verification failures (tampered/expired/wrong-authority/malformed
 *     cookies, an exception during verification) refuse rather than allow.
 *
 * `launchToken` / `cookieMaxAgeMs` are test-only seams: production never
 * passes them, and `startServiceProxy` mints both when they are absent. They
 * are supplied here as ordinary `ServiceProxyOptions` fields so the launch
 * flow and expiry can be exercised deterministically.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { connect } from 'node:net'
import { once } from 'node:events'

import { startServiceProxy, type ServiceProxy, type ServiceProxyOptions } from '../src/service-proxy.ts'

const TOKEN = 'unit-test-host-token'
const LAUNCH = 'unit-test-launch-token'

interface Upstream {
  readonly origin: string
  readonly seen: { method: string; url: string; authorization?: string; body: string }[]
  readonly upgrades: string[]
  readonly upgradeUrls: string[]
  close(): Promise<void>
}

/** Upstream stand-in for the Python service: echoes what it received. */
async function startUpstream(options: { readonly refuseUpgrade?: boolean } = {}): Promise<Upstream> {
  const seen: Upstream['seen'] = []
  const upgrades: string[] = []
  const upgradeUrls: string[] = []
  const upgraded = new Set<{ destroy(): void }>()
  const server = createServer((request, response) => {
    const chunks: Buffer[] = []
    request.on('data', (chunk: Buffer) => { chunks.push(chunk) })
    request.on('end', () => {
      seen.push({
        method: request.method ?? '',
        url: request.url ?? '',
        ...(request.headers.authorization === undefined ? {} : { authorization: request.headers.authorization }),
        body: Buffer.concat(chunks).toString('utf8'),
      })
      if ((request.url ?? '').startsWith('/api/')) {
        response.writeHead(200, { 'content-type': 'application/json' })
        response.end(JSON.stringify({ ok: true }))
        return
      }
      response.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
      response.end('<!doctype html><title>app</title><div id="root"></div>')
    })
  })
  server.on('upgrade', (request, socket) => {
    upgrades.push(request.headers.authorization ?? 'none')
    upgradeUrls.push(request.url ?? '')
    if (options.refuseUpgrade === true) {
      socket.end('HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n')
      return
    }
    upgraded.add(socket)
    socket.write('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n')
  })
  server.listen(0, '127.0.0.1')
  await once(server, 'listening')
  const address = server.address()
  if (address === null || typeof address === 'string') throw new Error('upstream has no port')
  return {
    origin: `http://127.0.0.1:${String(address.port)}`,
    seen,
    upgrades,
    upgradeUrls,
    close: () => new Promise<void>((resolve) => {
      for (const socket of upgraded) socket.destroy()
      server.closeAllConnections()
      server.close(() => { resolve() })
    }),
  }
}

function startProxy(options: ServiceProxyOptions): Promise<ServiceProxy> {
  return startServiceProxy(options)
}

/** The initial navigation URL the shell must load the window from. */
function launchUrl(origin: string, launch: string): string {
  return `${origin}/?token=${encodeURIComponent(launch)}`
}

/** First `name=value` of the minted cookie, or undefined when none is set. */
function sessionCookieFrom(response: Response): string | undefined {
  const setCookie = response.headers.get('set-cookie')
  if (setCookie === null || setCookie === undefined) return undefined
  const first = setCookie.split(';')[0]
  return first === undefined ? undefined : first
}

/**
 * Perform the launch exchange and return the minted cookie. Red until the
 * proxy implements token→cookie minting: the current proxy answers the launch
 * URL with the rendered page instead of a 302 + Set-Cookie.
 */
async function mintSessionCookie(proxy: ServiceProxy): Promise<string> {
  const exchange = await fetch(launchUrl(proxy.origin, LAUNCH), { redirect: 'manual' })
  assert.equal(exchange.status, 302, 'the launch exchange must redirect, not serve')
  const cookie = sessionCookieFrom(exchange)
  assert.ok(cookie, 'the launch exchange must mint a session cookie')
  return cookie
}

/** Raw one-shot HTTP GET to the proxy so Host can differ from the origin. */
function rawGet(origin: string, pathAndQuery: string, headers: Record<string, string>): Promise<string> {
  return new Promise((resolve, reject) => {
    const url = new URL(origin)
    const socket = connect({ host: url.hostname, port: Number(url.port) })
    const chunks: Buffer[] = []
    socket.on('data', (chunk: Buffer) => { chunks.push(chunk) })
    socket.on('error', reject)
    socket.on('close', () => resolve(Buffer.concat(chunks).toString('utf8')))
    socket.on('connect', () => {
      const lines = [`GET ${pathAndQuery} HTTP/1.1`, `Host: ${headers['host'] ?? url.host}`]
      for (const [name, value] of Object.entries(headers)) {
        if (name !== 'host') lines.push(`${name}: ${value}`)
      }
      socket.write(lines.join('\r\n') + '\r\n\r\n')
      socket.end()
    })
  })
}

/** Raw WebSocket handshake, returning what the proxy wrote to the socket. */
function rawHandshake(origin: string, headers: Record<string, string>, path = '/api/ws'): Promise<string> {
  return new Promise((resolve) => {
    const url = new URL(origin)
    const socket = connect({ host: url.hostname, port: Number(url.port) })
    const chunks: string[] = []
    socket.on('data', (chunk: Buffer) => { chunks.push(chunk.toString('utf8')) })
    socket.on('connect', () => {
      const lines = [
        `GET ${path} HTTP/1.1`,
        `Host: ${url.host}`,
        'Upgrade: websocket',
        'Connection: Upgrade',
        'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==',
        'Sec-WebSocket-Version: 13',
      ]
      for (const [name, value] of Object.entries(headers)) lines.push(`${name}: ${value}`)
      socket.write(lines.join('\r\n') + '\r\n\r\n')
      setTimeout(() => { socket.destroy(); resolve(chunks.join('')) }, 200)
    })
  })
}

describe('proxy caller authentication (#891 P2)', () => {
  it('rejects a plain GET / with no launch token (401) and never reaches the upstream', async () => {
    const upstream = await startUpstream()
    const proxy = await startProxy({ serviceOrigin: upstream.origin, token: TOKEN, launchToken: LAUNCH })
    try {
      const response = await fetch(`${proxy.origin}/`)
      assert.equal(response.status, 401)
      assert.equal(upstream.seen.length, 0, 'a rejected caller must not reach the service')
    } finally {
      await proxy.close()
      await upstream.close()
    }
  })

  it('rejects a wrong launch token (401)', async () => {
    const upstream = await startUpstream()
    const proxy = await startProxy({ serviceOrigin: upstream.origin, token: TOKEN, launchToken: LAUNCH })
    try {
      const response = await fetch(`${proxy.origin}/?token=wrong`)
      assert.equal(response.status, 401)
      assert.equal(upstream.seen.length, 0)
    } finally {
      await proxy.close()
      await upstream.close()
    }
  })

  it('rejects a launch token on a non-root path (401)', async () => {
    const upstream = await startUpstream()
    const proxy = await startProxy({ serviceOrigin: upstream.origin, token: TOKEN, launchToken: LAUNCH })
    try {
      const response = await fetch(`${proxy.origin}/api/health?token=${LAUNCH}`)
      assert.equal(response.status, 401)
      assert.equal(upstream.seen.length, 0)
    } finally {
      await proxy.close()
      await upstream.close()
    }
  })

  it('rejects a launch token given twice (401)', async () => {
    const upstream = await startUpstream()
    const proxy = await startProxy({ serviceOrigin: upstream.origin, token: TOKEN, launchToken: LAUNCH })
    try {
      const response = await fetch(`${proxy.origin}/?token=${LAUNCH}&token=again`)
      assert.equal(response.status, 401)
      assert.equal(upstream.seen.length, 0)
    } finally {
      await proxy.close()
      await upstream.close()
    }
  })

  it('exchanges one valid launch token for a session cookie and redirects to a clean path', async () => {
    const upstream = await startUpstream()
    const proxy = await startProxy({ serviceOrigin: upstream.origin, token: TOKEN, launchToken: LAUNCH })
    try {
      const response = await fetch(launchUrl(proxy.origin, LAUNCH), { redirect: 'manual' })
      assert.equal(response.status, 302)
      assert.equal(response.headers.get('location'), './', 'the token must not survive in the clean path')
      const setCookie = response.headers.get('set-cookie') ?? ''
      assert.ok(sessionCookieFrom(response), 'a session cookie must be minted')
      assert.match(setCookie, /HttpOnly/)
      assert.match(setCookie, /SameSite=Strict/)
    } finally {
      await proxy.close()
      await upstream.close()
    }
  })

  it('serves the renderer page on the clean path once a valid cookie is presented', async () => {
    const upstream = await startUpstream()
    const proxy = await startProxy({ serviceOrigin: upstream.origin, token: TOKEN, launchToken: LAUNCH })
    try {
      const cookie = await mintSessionCookie(proxy)
      const page = await fetch(`${proxy.origin}/`, { headers: { cookie } })
      assert.equal(page.status, 200)
      assert.match(await page.text(), /id="root"/)
      assert.equal(upstream.seen[0]?.authorization, `Bearer ${TOKEN}`, 'the host token must still be injected')
    } finally {
      await proxy.close()
      await upstream.close()
    }
  })

  it('rejects an /api/* request with no cookie (401) and never reaches the upstream', async () => {
    const upstream = await startUpstream()
    const proxy = await startProxy({ serviceOrigin: upstream.origin, token: TOKEN, launchToken: LAUNCH })
    try {
      const response = await fetch(`${proxy.origin}/api/health`)
      assert.equal(response.status, 401)
      assert.equal(upstream.seen.length, 0, 'a rejected caller must not reach the service')
    } finally {
      await proxy.close()
      await upstream.close()
    }
  })

  it('rejects a tampered session cookie (401)', async () => {
    const upstream = await startUpstream()
    const proxy = await startProxy({ serviceOrigin: upstream.origin, token: TOKEN, launchToken: LAUNCH })
    try {
      const cookie = await mintSessionCookie(proxy)
      const separator = cookie.indexOf('=')
      const name = cookie.slice(0, separator)
      const tampered = `${name}=${cookie.slice(separator + 1, -1)}${cookie.endsWith('-') ? '+' : '-'}`
      const response = await fetch(`${proxy.origin}/api/health`, { headers: { cookie: tampered } })
      assert.equal(response.status, 401)
    } finally {
      await proxy.close()
      await upstream.close()
    }
  })

  it('rejects an expired session cookie (401)', async () => {
    const upstream = await startUpstream()
    const proxy = await startProxy({ serviceOrigin: upstream.origin, token: TOKEN, launchToken: LAUNCH, cookieMaxAgeMs: 40 })
    try {
      const cookie = await mintSessionCookie(proxy)
      await new Promise((resolve) => { setTimeout(resolve, 80) })
      const response = await fetch(`${proxy.origin}/api/health`, { headers: { cookie } })
      assert.equal(response.status, 401)
    } finally {
      await proxy.close()
      await upstream.close()
    }
  })

  it('rejects a cookie minted for a different authority (401)', async () => {
    const upstream = await startUpstream()
    const proxy = await startProxy({ serviceOrigin: upstream.origin, token: TOKEN, launchToken: LAUNCH })
    try {
      const cookie = await mintSessionCookie(proxy)
      const response = await rawGet(proxy.origin, '/api/health', { host: '127.0.0.1:1', cookie })
      assert.match(response, /HTTP\/1\.1 401/)
    } finally {
      await proxy.close()
      await upstream.close()
    }
  })

  it('refuses a WebSocket handshake with no cookie (no 101, upstream never sees it)', async () => {
    const upstream = await startUpstream()
    const proxy = await startProxy({ serviceOrigin: upstream.origin, token: TOKEN, launchToken: LAUNCH })
    try {
      const received = await rawHandshake(proxy.origin, {})
      assert.doesNotMatch(received, /101 Switching Protocols/)
      assert.equal(upstream.upgrades.length, 0, 'a refused handshake must not reach the service')
    } finally {
      await proxy.close()
      await upstream.close()
    }
  })

  it('accepts a WebSocket handshake with a valid cookie (101 and the splice works)', async () => {
    const upstream = await startUpstream()
    const proxy = await startProxy({ serviceOrigin: upstream.origin, token: TOKEN, launchToken: LAUNCH })
    try {
      const cookie = await mintSessionCookie(proxy)
      const received = await rawHandshake(proxy.origin, { cookie })
      assert.match(received, /101 Switching Protocols/)
      assert.equal(upstream.upgrades[0], `Bearer ${TOKEN}`)
    } finally {
      await proxy.close()
      await upstream.close()
    }
  })

  it('refuses a WebSocket handshake with a bad cookie (no 101, upstream never sees it)', async () => {
    const upstream = await startUpstream()
    const proxy = await startProxy({ serviceOrigin: upstream.origin, token: TOKEN, launchToken: LAUNCH })
    try {
      const received = await rawHandshake(proxy.origin, { cookie: 'ia-proxy-auth=garbage' })
      assert.doesNotMatch(received, /101 Switching Protocols/)
      assert.equal(upstream.upgrades.length, 0)
    } finally {
      await proxy.close()
      await upstream.close()
    }
  })

  it('fails closed when cookie verification cannot complete (401, never 500/forward)', async () => {
    const upstream = await startUpstream()
    const proxy = await startProxy({ serviceOrigin: upstream.origin, token: TOKEN, launchToken: LAUNCH })
    try {
      // A well-formed cookie whose signature is not valid base64url of the
      // expected byte length is rejected by the decode/length guards before
      // any constant-time compare runs; either way the request must 401 and
      // never reach the upstream.
      const cookie = `ia-proxy-auth=v1.${'AAAA'.repeat(11)}.short-sig`
      const response = await fetch(`${proxy.origin}/api/health`, { headers: { cookie } })
      assert.equal(response.status, 401)
      assert.equal(upstream.seen.length, 0, 'a verification failure must not reach the service')
    } finally {
      await proxy.close()
      await upstream.close()
    }
  })

  it('survives an absolute-form request line that new URL cannot parse (401, no crash)', async () => {
    const upstream = await startUpstream()
    const proxy = await startProxy({ serviceOrigin: upstream.origin, token: TOKEN, launchToken: LAUNCH })
    try {
      const response = await rawGet(proxy.origin, 'http://example.com:notaport/', {})
      assert.match(response, /HTTP\/1\.1 401/)
      assert.equal(upstream.seen.length, 0, 'an unparseable request must not reach the service')
    } finally {
      await proxy.close()
      await upstream.close()
    }
  })

  it('strips the launch token from the upgraded request before it reaches the upstream', async () => {
    const upstream = await startUpstream()
    const proxy = await startProxy({ serviceOrigin: upstream.origin, token: TOKEN, launchToken: LAUNCH })
    try {
      const cookie = await mintSessionCookie(proxy)
      const received = await rawHandshake(proxy.origin, { cookie }, `/api/ws?token=${LAUNCH}&x=1`)
      assert.match(received, /101 Switching Protocols/)
      assert.equal(upstream.upgrades[0], `Bearer ${TOKEN}`, 'the host token must still be injected')
      const url = new URL(`http://upstream.invalid${upstream.upgradeUrls[0] ?? ''}`)
      assert.equal(url.searchParams.get('token'), null, 'the launch token must not be forwarded upstream')
      assert.equal(url.searchParams.get('x'), '1', 'other query parameters must be preserved')
    } finally {
      await proxy.close()
      await upstream.close()
    }
  })
})
