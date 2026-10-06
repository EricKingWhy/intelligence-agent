/**
 * Host attach protocol shapes shared by the shell (client side of W-11 / #355).
 *
 * The server-side authority is `src/agent_harness/host_service.py`; the constants
 * and field names below mirror it exactly so the shell can read the endpoint file
 * and validate the health payload without inventing a second protocol.
 */

/** Must equal `agent_harness.host_service.HOST_PROTOCOL_VERSION`. */
export const HOST_PROTOCOL_VERSION = 1

/** Endpoint state file written by `agent-harness serve` into the workspace dir. */
export const ENDPOINT_FILENAME = '.host-service.json'

/** Credential-channel username prefix; server-side `_TOKEN_USERNAME_PREFIX`. */
export const TOKEN_USERNAME_PREFIX = 'host-service/'

/** Environment variable that selects the credential backend (server-side seam). */
export const HOST_CREDENTIALS_ENV = 'AGENT_HARNESS_HOST_CREDENTIALS'

/** Endpoint file payload; secrets never appear here (the host token uses the credential channel). */
export interface HostEndpointInfo {
  readonly pid: number
  readonly port: number
  readonly protocol_version: number
  readonly auth_required: boolean
  readonly owner: string
  readonly started_at: string
  readonly service_uuid: string
}

function isInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value)
}

/**
 * Parse an endpoint payload fail-closed.
 * @param payload - Parsed JSON of `.host-service.json`.
 * @returns the endpoint info, or undefined when the shape is not the protocol's.
 */
export function parseHostEndpoint(payload: unknown): HostEndpointInfo | undefined {
  if (typeof payload !== 'object' || payload === null) return undefined
  const record = payload as Record<string, unknown>
  const { pid, port, protocol_version: protocolVersion, auth_required: authRequired } = record
  if (!isInteger(pid) || pid <= 0) return undefined
  if (!isInteger(port) || port <= 0 || port > 65535) return undefined
  if (!isInteger(protocolVersion)) return undefined
  if (typeof authRequired !== 'boolean') return undefined
  return {
    pid,
    port,
    protocol_version: protocolVersion,
    auth_required: authRequired,
    owner: typeof record.owner === 'string' ? record.owner : '',
    started_at: typeof record.started_at === 'string' ? record.started_at : '',
    service_uuid: typeof record.service_uuid === 'string' ? record.service_uuid : '',
  }
}

/** Health payload fields the shell relies on (`GET /api/health`, W-11 #355). */
export interface HostHealthPayload {
  readonly version: string
  readonly protocolVersion: number
}

/** Health classification: only `ready` permits loading the packaged web page. */
export type HealthVerdict =
  | { readonly kind: 'ready'; readonly payload: HostHealthPayload }
  | { readonly kind: 'not-ready'; readonly reason: string }
  | { readonly kind: 'incompatible'; readonly reason: string }

/**
 * Classify one `GET /api/health` response.
 *
 * Mirrors `host_service.attach_probe`'s second step: HTTP 200 + `status == "ok"`
 * is required, and a `protocol_version` that differs from ours is a hard
 * incompatibility (an older/newer service is not attachable, never "wait longer").
 * @param status - HTTP status code.
 * @param body - Parsed JSON body.
 * @returns the verdict.
 */
export function classifyHealth(status: number, body: unknown): HealthVerdict {
  if (status !== 200) return { kind: 'not-ready', reason: `健康面 HTTP ${String(status)}` }
  if (typeof body !== 'object' || body === null) return { kind: 'not-ready', reason: '健康面载荷不是对象' }
  const record = body as Record<string, unknown>
  if (record.status !== 'ok') return { kind: 'not-ready', reason: '健康面 status != ok' }
  const protocolVersion = record.protocol_version
  if (!isInteger(protocolVersion)) return { kind: 'not-ready', reason: '健康面缺少 protocol_version' }
  if (protocolVersion !== HOST_PROTOCOL_VERSION) {
    return {
      kind: 'incompatible',
      reason: `协议版本不符（服务 ${String(protocolVersion)} != 本端 ${String(HOST_PROTOCOL_VERSION)}）`,
    }
  }
  return {
    kind: 'ready',
    payload: { version: typeof record.version === 'string' ? record.version : '', protocolVersion },
  }
}
