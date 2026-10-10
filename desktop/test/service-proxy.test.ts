/**
 * W-21 D3 (#815): the shell-owned loopback proxy.
 *
 * The window loads the proxy origin; the proxy is the only process that holds the
 * host token, and it must attach that token to both plain HTTP and the live
 * channel's WebSocket handshake (Electron's `webRequest` never sees the upgrade —
 * measured 2026-10-07, /d/w21-work/header-probe).
 *
 * #891 P2: every request must first pass the caller check — a launch exchange
 * (`GET /?token=…`) mints the session cookie and 302s to a clean path; the callers
 * below present that cookie the way the real window does after the redirect.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { connect } from 'node:net'
import { once } from 'node:events'

import { parseLoopbackOrigin, startServiceProxy, upstreamHeaders, type ServiceProxy, type ServiceProxyOptions } from '../src/service-proxy.ts'

const TOKEN = 'unit-test-token'
const LAUNCH = 'unit-test-launch-token'

interface Upstream {
  readonly origin: string
  readonly seen: { method: string; url: string; authorization?: string; body: string }[]
  readonly upgrades: string[]
  close(): Promise<void>
}

/** Upstream stand-in for the Python service: echoes what it received. */
async function startUpstream(options: { readonly refuseUpgrade?: boolean } = {}): Promise<Upstream> {
  const seen: Upstream['seen'] = []
  const upgrades: string[] = []
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
    if (options.refuseUpgrade === true) {
      // What a fail-closed service does with a handshake it will not authorize:
      // a plain status line, no 101.
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
    close: () => new Promise<void>((resolve) => {
      // An upgraded socket leaves the HTTP server's connection tracking, so
      // close() would otherwise wait for it forever.
      for (const socket of upgraded) socket.destroy()
      server.closeAllConnections()
      server.close(() => { resolve() })
    }),
  }
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

/** Perform the launch exchange and return the minted cookie (the #891 flow). */
async function mintSessionCookie(proxy: ServiceProxy): Promise<string> {
  const exchange = await fetch(launchUrl(proxy.origin, LAUNCH), { redirect: 'manual' })
  assert.equal(exchange.status, 302, 'the launch exchange must redirect, not serve')
  const cookie = sessionCookieFrom(exchange)
  assert.ok(cookie, 'the launch exchange must mint a session cookie')
  return cookie
}

function startProxy(options: ServiceProxyOptions): Promise<ServiceProxy> {
  return startServiceProxy({ ...options, launchToken: LAUNCH })
}

describe('parseLoopbackOrigin', () => {
  it('accepts a loopback http origin', () => {
    assert.deepEqual(parseLoopbackOrigin('http://127.0.0.1:60942'), { host: '127.0.0.1', port: 60942 })
  })

  it('refuses anything that is not loopback http', () => {
    assert.throws(() => parseLoopbackOrigin('https://127.0.0.1:60942'), /http loopback origin/)
    assert.throws(() => parseLoopbackOrigin('http://example.com:80'), /non-loopback origin/)
  })
})

describe('upstreamHeaders', () => {
  it('replaces host/authorization and keeps the handshake framing headers', () => {
    const incoming = {
      headers: { host: '127.0.0.1:5000', authorization: 'Bearer page-supplied', connection: 'Upgrade', accept: 'application/json' },
    } as unknown as Parameters<typeof upstreamHeaders>[0]
    const headers = upstreamHeaders(incoming, TOKEN)
    assert.equal(headers['authorization'], `Bearer ${TOKEN}`)
    assert.equal(headers['accept'], 'application/json')
    assert.equal('host' in headers, false, 'the upstream hop sets its own Host')
    // `connection`/`upgrade` carry the WebSocket handshake through the same path.
    assert.equal(headers['connection'], 'Upgrade')
  })

  it('leaves the caller-supplied authorization alone when the shell has no token', () => {
    const incoming = {
      headers: { host: 'x', authorization: 'Bearer page-supplied' },
    } as unknown as Parameters<typeof upstreamHeaders>[0]
    const headers = upstreamHeaders(incoming, undefined)
    assert.equal('authorization' in headers, false)
  })
})

describe('startServiceProxy', () => {
  it('serves the renderer page and forwards the host token instead of the page credential', async () => {
    const upstream = await startUpstream()
    const proxy = await startProxy({ serviceOrigin: upstream.origin, token: TOKEN })
    try {
      const cookie = await mintSessionCookie(proxy)
      const page = await fetch(`${proxy.origin}/`, { headers: { cookie } })
      assert.equal(page.status, 200)
      assert.match(await page.text(), /id="root"/)
      assert.equal(upstream.seen[0]?.authorization, `Bearer ${TOKEN}`)

      const api = await fetch(`${proxy.origin}/api/health`, { headers: { cookie, authorization: 'Bearer page-supplied' } })
      assert.equal(api.status, 200)
      assert.equal(upstream.seen[1]?.authorization, `Bearer ${TOKEN}`, 'the page credential must not reach the service')
      assert.equal(upstream.seen[1]?.url, '/api/health')
    } finally {
      await proxy.close()
      await upstream.close()
    }
  })

  it('preserves method, path, query and body', async () => {
    const upstream = await startUpstream()
    const proxy = await startProxy({ serviceOrigin: upstream.origin, token: TOKEN })
    try {
      const cookie = await mintSessionCookie(proxy)
      const response = await fetch(`${proxy.origin}/api/sessions?limit=2`, {
        method: 'POST',
        headers: { cookie, 'content-type': 'application/json' },
        body: JSON.stringify({ client_id: 'desktop' }),
      })
      assert.equal(response.status, 200)
      const received = upstream.seen[0]
      assert.equal(received?.method, 'POST')
      assert.equal(received?.url, '/api/sessions?limit=2')
      assert.equal(received?.body, '{"client_id":"desktop"}')
    } finally {
      await proxy.close()
      await upstream.close()
    }
  })

  it('attaches the token to the live channel WebSocket handshake', async () => {
    const upstream = await startUpstream()
    const proxy = await startProxy({ serviceOrigin: upstream.origin, token: TOKEN })
    const address = new URL(proxy.origin)
    try {
      const cookie = await mintSessionCookie(proxy)
      const socket = connect({ host: address.hostname, port: Number(address.port) })
      const chunks: string[] = []
      socket.on('data', (chunk: Buffer) => { chunks.push(chunk.toString('utf8')) })
      await once(socket, 'connect')
      socket.write(
        'GET /api/ws HTTP/1.1\r\nHost: ' + address.host + '\r\nCookie: ' + cookie + '\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
        + 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n',
      )
      await new Promise((resolve) => { setTimeout(resolve, 200) })
      socket.destroy()
      assert.match(chunks.join(''), /101 Switching Protocols/)
      assert.equal(upstream.upgrades[0], `Bearer ${TOKEN}`)
    } finally {
      await proxy.close()
      await upstream.close()
    }
  })

  it('relays a refused handshake instead of hanging the live channel', async () => {
    const upstream = await startUpstream({ refuseUpgrade: true })
    const proxy = await startProxy({ serviceOrigin: upstream.origin, token: TOKEN })
    const address = new URL(proxy.origin)
    try {
      const cookie = await mintSessionCookie(proxy)
      const socket = connect({ host: address.hostname, port: Number(address.port) })
      const chunks: string[] = []
      socket.on('data', (chunk: Buffer) => { chunks.push(chunk.toString('utf8')) })
      await once(socket, 'connect')
      socket.write(
        'GET /api/ws HTTP/1.1\r\nHost: ' + address.host + '\r\nCookie: ' + cookie + '\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
        + 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n',
      )
      await once(socket, 'close')
      assert.match(chunks.join(''), /401 Unauthorized/)
      socket.destroy()
    } finally {
      await proxy.close()
      await upstream.close()
    }
  })

  it('answers 502 instead of hanging when the service is gone', async () => {
    const upstream = await startUpstream()
    const origin = upstream.origin
    await upstream.close()
    const proxy = await startProxy({ serviceOrigin: origin, token: TOKEN })
    try {
      // The launch exchange is handled by the proxy itself and does not touch
      // the service, so a session cookie can be minted even once it is gone.
      const cookie = await mintSessionCookie(proxy)
      const response = await fetch(`${proxy.origin}/api/health`, { headers: { cookie } })
      assert.equal(response.status, 502)
      assert.match(await response.text(), /service unreachable/)
    } finally {
      await proxy.close()
    }
  })
})
