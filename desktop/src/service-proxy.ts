/**
 * Loopback proxy owned by the shell: the window talks to one origin and only
 * this process ever holds the host token (W-21 D3 / #815).
 *
 * Why a proxy at all: the service is fail-closed (a generated JWT secret means
 * every request without a Bearer token is 401), and the frontend addresses its
 * API and its live channel relative to its own origin. Electron's native
 * `webRequest.onBeforeSendHeaders` would cover navigation, subresources and
 * `fetch` — but NOT the WebSocket handshake (measured on Electron 44,
 * 2026-10-07: the upgrade arrives with no Authorization at all), and the live
 * channel is what streams session events. One forward hop covers both.
 *
 * Caller verification (#891 P2): the proxy is a token-injection gateway on
 * 127.0.0.1, so any local process or web page could drive the API through it.
 * Each start mints an unguessable launch token and an HMAC secret (shell
 * memory only); `GET /?token=<launch>` exchanges the token for an
 * authority-bound, HMAC-signed, HttpOnly/Strict session cookie and 302s to a
 * clean path — the token never survives in the address bar. Every later
 * request, HTTP and the WebSocket upgrade, must carry that cookie; anything
 * else is refused fail-closed (401 / refused handshake) and never reaches
 * upstream.
 *
 * Mechanism ADAPTED from DeepSeek Harness (MIT License):
 *   apps/desktop/src/web-document.ts:75-100 — "Forward local application requests
 *   to its authenticated Host", the credential attached by the shell, hop-by-hop
 *   response headers withheld from the renderer.
 *   packages/client/connection/src/browser-auth.ts — the launch-token → signed
 *   authority-bound cookie → fail-closed verify shape (token exchange, HMAC
 *   signature, timing-safe compare, authority + expiry inside the payload).
 *   https://github.com/deepseek-ai/deepseek-harness
 *   commit 5badb15009ae1756c3afe0ae0cef1faafc290ccc
 * Changes: Node `http` server (the upgrade needs a socket splice, which a
 * Request/Response handler cannot express); the caller is bound by an
 * HMAC-signed cookie instead of DSH's persistent credential store; the window
 * still receives `Bearer <host token>` on the upstream hop (unchanged).
 * Full MIT text: desktop/THIRD_PARTY_NOTICES.md.
 */

import type { Duplex } from 'node:stream'
import { createHmac, randomBytes, timingSafeEqual } from 'node:crypto'
import { createServer, request as upstreamRequest, type IncomingMessage, type ServerResponse } from 'node:http'

/** Origin and credential for the service the window is served through. */
export interface ServiceProxyOptions {
  /** Origin of the Python service hosting the API and the renderer build. */
  readonly serviceOrigin: string
  /** Host token from the credential channel; absent means an unauthenticated proxy. */
  readonly token?: string | undefined
  /** Test seam: fixed launch token; production mints a fresh one per start. */
  readonly launchToken?: string | undefined
  /** Test seam: session-cookie lifetime; production uses a generous default. */
  readonly cookieMaxAgeMs?: number | undefined
}

/** A running proxy: the origin to load the window from, and its shutdown. */
export interface ServiceProxy {
  readonly origin: string
  /** Initial navigation URL carrying the one-shot launch token (#891). */
  readonly launchUrl: string
  close(): Promise<void>
}

/** Response headers that describe this hop only and must not reach the renderer. */
const HOP_BY_HOP = new Set(['connection', 'keep-alive', 'transfer-encoding', 'upgrade'])

interface Target {
  readonly host: string
  readonly port: number
}

/** Parse and validate the loopback service origin (fail closed on anything else). */
export function parseLoopbackOrigin(origin: string): Target {
  const url = new URL(origin)
  if (url.protocol !== 'http:') throw new Error(`service proxy requires an http loopback origin: ${origin}`)
  if (!['127.0.0.1', '::1', 'localhost'].includes(url.hostname)) {
    throw new Error(`service proxy refuses a non-loopback origin: ${origin}`)
  }
  const port = url.port === '' ? 80 : Number(url.port)
  if (!Number.isInteger(port) || port <= 0) throw new Error(`service proxy origin has no usable port: ${origin}`)
  return { host: url.hostname, port }
}

/**
 * Headers for the upstream hop: the client's own headers minus its identity, plus
 * the shell's token. `connection`/`upgrade` are deliberately kept — the same path
 * carries the WebSocket handshake, which is meaningless without them.
 */
export function upstreamHeaders(
  incoming: IncomingMessage,
  token: string | undefined,
): Record<string, string | string[]> {
  const headers: Record<string, string | string[]> = {}
  for (const [name, value] of Object.entries(incoming.headers)) {
    if (value === undefined || name === 'host' || name === 'authorization') continue
    headers[name] = value
  }
  if (token !== undefined) headers['authorization'] = `Bearer ${token}`
  return headers
}

/** Headers of a relayed response, minus the ones that describe this hop. */
function relayable(headers: Record<string, unknown>): [string, string | string[]][] {
  return Object.entries(headers)
    .filter(([name, value]) => value !== undefined && !HOP_BY_HOP.has(name))
    .map(([name, value]) => [name, value as string | string[]])
}

// --- Caller verification (#891 P2) ---------------------------------------------

/** Cookie name fixed by the #891 contract; authority is bound inside the payload. */
const COOKIE_NAME = 'ia-proxy-auth'
const COOKIE_PAYLOAD_VERSION = 1
/** 32 bytes of entropy for both the launch token and the HMAC secret. */
const SECRET_BYTES = 32
const TOKEN_QUERY = 'token'
/** Session lifetime when production does not pass `cookieMaxAgeMs`. */
const DEFAULT_COOKIE_MAX_AGE_MS = 24 * 60 * 60 * 1000
const BASE64URL_PATTERN = /^[A-Za-z0-9_-]*$/

/** The minted state of one proxy start: launch token, signing secret, lifetime. */
interface CallerAuth {
  readonly launchToken: string
  readonly secret: Buffer
  readonly cookieMaxAgeMs: number
}

/** Payload signed into the session cookie (version + authority + timestamps). */
interface CookiePayload {
  readonly version: number
  readonly authority: string
  readonly issuedAt: number
  readonly expiresAt: number
}

function encodeBase64Url(value: Uint8Array): string {
  return Buffer.from(value).toString('base64')
    .replaceAll('+', '-')
    .replaceAll('/', '_')
    .replace(/=+$/u, '')
}

function decodeBase64Url(value: string): Buffer | undefined {
  if (!BASE64URL_PATTERN.test(value) || value.length % 4 === 1) return undefined
  const padding = '='.repeat((4 - value.length % 4) % 4)
  const decoded = Buffer.from(value.replaceAll('-', '+').replaceAll('_', '/') + padding, 'base64')
  return encodeBase64Url(decoded) === value ? decoded : undefined
}

function tokenMatches(actual: string, expected: string): boolean {
  const actualBytes = Buffer.from(actual, 'utf8')
  const expectedBytes = Buffer.from(expected, 'utf8')
  return actualBytes.byteLength === expectedBytes.byteLength && timingSafeEqual(actualBytes, expectedBytes)
}

/** Canonical request authority (host including port) used as the signed audience. */
function requestAuthority(headers: IncomingMessage['headers']): string | undefined {
  const host = headers['host']
  if (typeof host !== 'string') return undefined
  try {
    return new URL(`http://${host}`).host
  } catch {
    return undefined
  }
}

/** Read one cookie's value without implementing a general Cookie parser. */
function cookieValue(headerValue: string, name: string): string | undefined {
  for (const segment of headerValue.split(';')) {
    const at = segment.indexOf('=')
    if (at === -1 || segment.slice(0, at).trim() !== name) continue
    return segment.slice(at + 1).trim()
  }
  return undefined
}

function signature(secret: Buffer, body: string): Buffer {
  return createHmac('sha256', secret).update(body).digest()
}

function encodeCookie(payload: CookiePayload, secret: Buffer): string {
  const body = encodeBase64Url(Buffer.from(JSON.stringify(payload), 'utf8'))
  return `v1.${body}.${encodeBase64Url(signature(secret, body))}`
}

function sessionCookie(value: string, expiresAt: number): string {
  const maxAgeSeconds = Math.max(1, Math.floor((expiresAt - Date.now()) / 1000))
  return `${COOKIE_NAME}=${value}; Max-Age=${String(maxAgeSeconds)}; Path=/; Expires=${new Date(expiresAt).toUTCString()}; HttpOnly; SameSite=Strict`
}

/**
 * Verify the signed cookie. Any failure — malformed shape, bad signature,
 * wrong authority, expired, or an exception while decoding — returns undefined;
 * the caller must fail closed on that.
 */
function decodeCookie(value: string, secret: Buffer): CookiePayload | undefined {
  const parts = value.split('.')
  const [version, body, encodedSignature] = parts
  if (parts.length !== 3 || version !== 'v1' || body === undefined || encodedSignature === undefined) {
    return undefined
  }
  const expected = signature(secret, body)
  const actual = decodeBase64Url(encodedSignature)
  if (actual === undefined || actual.byteLength !== expected.byteLength) return undefined
  if (!timingSafeEqual(actual, expected)) return undefined
  const bodyBytes = decodeBase64Url(body)
  if (bodyBytes === undefined) return undefined
  let decoded: unknown
  try {
    decoded = JSON.parse(bodyBytes.toString('utf8'))
  } catch {
    return undefined
  }
  if (typeof decoded !== 'object' || decoded === null || Array.isArray(decoded)) return undefined
  const record = decoded as Record<string, unknown>
  const issuedAt = record.issuedAt
  const expiresAt = record.expiresAt
  if (record.version !== COOKIE_PAYLOAD_VERSION
    || typeof record.authority !== 'string'
    || typeof issuedAt !== 'number'
    || !Number.isSafeInteger(issuedAt)
    || typeof expiresAt !== 'number'
    || !Number.isSafeInteger(expiresAt)) return undefined
  return {
    version: record.version,
    authority: record.authority,
    issuedAt,
    expiresAt,
  }
}

/** True for an unexpired cookie signed by this start's secret and bound to this authority. */
function isAuthenticated(request: IncomingMessage, auth: CallerAuth): boolean {
  const authority = requestAuthority(request.headers)
  const rawCookie = request.headers['cookie']
  if (authority === undefined || typeof rawCookie !== 'string') return false
  const value = cookieValue(rawCookie, COOKIE_NAME)
  if (value === undefined) return false
  const payload = decodeCookie(value, auth.secret)
  if (payload === undefined || payload.authority !== authority) return false
  const now = Date.now()
  return payload.issuedAt <= now
    && payload.expiresAt > now
    && payload.expiresAt > payload.issuedAt
    && payload.expiresAt - payload.issuedAt <= auth.cookieMaxAgeMs
}

function writeUnauthorized(response: ServerResponse): void {
  response.writeHead(401, {
    'cache-control': 'no-store',
    'content-type': 'text/plain; charset=utf-8',
  })
  response.end('proxy authentication required; reopen the app window\n')
}

/**
 * Decide whether this request may be forwarded. A valid launch token on `GET /`
 * mints the session cookie and redirects to the clean path; a valid cookie lets
 * the request through; anything else is refused (401). Whenever this returns
 * false the response has already been written.
 */
function authorizeRequest(request: IncomingMessage, response: ServerResponse, auth: CallerAuth): boolean {
  const url = new URL(request.url ?? '/', 'http://proxy.invalid')
  const tokens = url.searchParams.getAll(TOKEN_QUERY)
  if (tokens.length > 0) {
    const authority = requestAuthority(request.headers)
    const launch = tokens[0]
    if (request.method === 'GET' && url.pathname === '/'
      && tokens.length === 1 && launch !== undefined && authority !== undefined
      && tokenMatches(launch, auth.launchToken)) {
      const issuedAt = Date.now()
      const expiresAt = issuedAt + auth.cookieMaxAgeMs
      const value = encodeCookie({
        version: COOKIE_PAYLOAD_VERSION,
        authority,
        issuedAt,
        expiresAt,
      }, auth.secret)
      response.writeHead(302, {
        'cache-control': 'no-store',
        'location': './',
        'referrer-policy': 'no-referrer',
        'set-cookie': sessionCookie(value, expiresAt),
      })
      response.end()
      return false
    }
    // A request carrying a token is never forwarded: either the token is stale
    // (401) or the caller already holds a valid cookie, in which case the token
    // is dropped and the URL cleared to the same clean 302.
    if (request.method === 'GET' && url.pathname === '/' && isAuthenticated(request, auth)) {
      response.writeHead(302, { 'cache-control': 'no-store', 'location': './', 'referrer-policy': 'no-referrer' })
      response.end()
      return false
    }
    writeUnauthorized(response)
    return false
  }
  if (isAuthenticated(request, auth)) return true
  writeUnauthorized(response)
  return false
}

/**
 * Start the proxy on a random loopback port.
 * @param options - upstream service origin, host token and test seams.
 * @returns the origin / launch URL the window must load, plus `close()`.
 */
export async function startServiceProxy(options: ServiceProxyOptions): Promise<ServiceProxy> {
  const target = parseLoopbackOrigin(options.serviceOrigin)
  const auth: CallerAuth = {
    launchToken: options.launchToken ?? encodeBase64Url(randomBytes(SECRET_BYTES)),
    secret: randomBytes(SECRET_BYTES),
    cookieMaxAgeMs: options.cookieMaxAgeMs ?? DEFAULT_COOKIE_MAX_AGE_MS,
  }
  const forward = (request: IncomingMessage) => upstreamRequest({
    host: target.host,
    port: target.port,
    method: request.method,
    path: request.url,
    headers: upstreamHeaders(request, options.token),
  })

  const server = createServer((request, response) => {
    if (!authorizeRequest(request, response, auth)) return
    const upstream = forward(request)
    upstream.on('response', (reply) => {
      for (const [name, value] of relayable(reply.headers)) response.setHeader(name, value)
      response.writeHead(reply.statusCode ?? 502)
      reply.pipe(response)
    })
    upstream.on('error', () => {
      if (response.headersSent) response.destroy()
      else {
        response.setHeader('content-type', 'application/json')
        response.writeHead(502)
        response.end(JSON.stringify({ detail: 'desktop proxy: service unreachable' }))
      }
    })
    request.pipe(upstream)
  })

  // Upgraded sockets are not tracked by the HTTP server once they leave it, so
  // they are held here: a window closing its live channel must not leave the
  // upstream hop (or this server's shutdown) waiting forever.
  const live = new Set<Duplex>()

  // The live channel: the same forward call, but Node hands back the upgraded
  // socket instead of a response. The 101 head is relayed verbatim — dropping
  // its `Connection`/`Upgrade` headers would break the handshake.
  server.on('upgrade', (request, socket, head) => {
    if (!isAuthenticated(request, auth)) {
      // Fail closed: refuse the handshake before the upstream ever learns that
      // a caller without a valid session cookie exists.
      socket.write('HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n')
      socket.destroy()
      return
    }
    const upstream = forward(request)
    upstream.on('upgrade', (reply, upstreamSocket, upstreamHead) => {
      const lines = [`HTTP/1.1 ${String(reply.statusCode ?? 101)} ${reply.statusMessage ?? ''}`]
      for (const [name, value] of Object.entries(reply.headers)) {
        if (value !== undefined) lines.push(`${name}: ${Array.isArray(value) ? value.join(', ') : String(value)}`)
      }
      socket.write(lines.join('\r\n') + '\r\n\r\n')
      if (upstreamHead.length > 0) socket.write(upstreamHead)
      upstreamSocket.pipe(socket)
      socket.pipe(upstreamSocket)
      live.add(upstreamSocket)
      // Either side going away ends the hop; `end` matters as much as `close`,
      // because a peer's FIN arrives as half-close on these sockets.
      const end = (): void => { live.delete(upstreamSocket); upstreamSocket.destroy(); socket.destroy() }
      for (const event of ['close', 'end', 'error'] as const) {
        upstreamSocket.on(event, end)
        socket.on(event, end)
      }
    })
    upstream.on('error', () => { socket.destroy() })
    // A plain response to a handshake is a refusal (401 without a valid token,
    // 404 for a wrong path). Relay its status line so the renderer's WebSocket
    // fails at once — leaving the socket open would hang the live channel.
    upstream.on('response', (reply) => {
      socket.write(`HTTP/1.1 ${String(reply.statusCode ?? 502)} ${reply.statusMessage ?? 'Handshake Refused'}\r\n\r\n`)
      socket.destroy()
    })
    upstream.end(head)
  })

  await new Promise<void>((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', resolve)
  })
  const address = server.address()
  if (address === null || typeof address === 'string') {
    throw new Error('desktop proxy: listen() produced no port')
  }

  const origin = `http://127.0.0.1:${String(address.port)}`
  return {
    origin,
    // #891: the window opens through this URL; the launch token appears only
    // here and is exchanged for the session cookie before the page itself loads.
    launchUrl: `${origin}/?token=${encodeURIComponent(auth.launchToken)}`,
    close: () => new Promise<void>((resolve, reject) => {
      for (const socket of live) socket.destroy()
      server.closeAllConnections()
      server.close((error) => { if (error) reject(error); else resolve() })
    }),
  }
}
