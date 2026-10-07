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
 * Mechanism ADAPTED from DeepSeek Harness (MIT License):
 *   apps/desktop/src/web-document.ts:75-100 — "Forward local application requests
 *   to its authenticated Host", the credential attached by the shell, hop-by-hop
 *   response headers withheld from the renderer.
 *   https://github.com/deepseek-ai/deepseek-harness
 *   commit 5badb15009ae1756c3afe0ae0cef1faafc290ccc
 * Changes: Node `http` server (the upgrade needs a socket splice, which a
 * Request/Response handler cannot express) and a Bearer host token instead of
 * DSH's authority-bound cookie. Full MIT text: desktop/THIRD_PARTY_NOTICES.md.
 */

import type { Duplex } from 'node:stream'
import { createServer, request as upstreamRequest, type IncomingMessage } from 'node:http'

/** Origin and credential for the service the window is served through. */
export interface ServiceProxyOptions {
  /** Origin of the Python service hosting the API and the renderer build. */
  readonly serviceOrigin: string
  /** Host token from the credential channel; absent means an unauthenticated proxy. */
  readonly token?: string | undefined
}

/** A running proxy: the origin to load the window from, and its shutdown. */
export interface ServiceProxy {
  readonly origin: string
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

/**
 * Start the proxy on a random loopback port.
 * @param options - upstream service origin and the host token.
 * @returns the origin the window must load, plus `close()`.
 */
export async function startServiceProxy(options: ServiceProxyOptions): Promise<ServiceProxy> {
  const target = parseLoopbackOrigin(options.serviceOrigin)
  const forward = (request: IncomingMessage) => upstreamRequest({
    host: target.host,
    port: target.port,
    method: request.method,
    path: request.url,
    headers: upstreamHeaders(request, options.token),
  })

  const server = createServer((request, response) => {
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

  return {
    origin: `http://127.0.0.1:${String(address.port)}`,
    close: () => new Promise<void>((resolve, reject) => {
      for (const socket of live) socket.destroy()
      server.closeAllConnections()
      server.close((error) => { if (error) reject(error); else resolve() })
    }),
  }
}
