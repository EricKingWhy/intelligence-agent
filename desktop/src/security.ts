/**
 * Renderer security policy for the thin host: what counts as a local page, what
 * may keep a host token, and where a link may go.
 *
 * BUILD — no upstream module fits: this is the boundary the shell owns. The rules
 * implement the W-15 work instruction ("Host token only for the shell's own local
 * pages; external links open in the system browser and carry no token") so they
 * can be unit-tested independently of Electron.
 */

/** The hardened `BrowserWindow` renderer preferences the shell always uses. */
export interface DesktopWebPreferences {
  /** Absolute path to the compiled preload script. */
  readonly preload: string
  readonly nodeIntegration: false
  readonly contextIsolation: true
  readonly sandbox: true
  readonly webSecurity: true
  readonly webviewTag: false
}

/**
 * The one renderer configuration the thin host allows: Node integration off,
 * context isolation and the sandbox on, no `<webview>` tag. The preload path is
 * the only caller-supplied value, so the policy itself stays a constant and can
 * be asserted without Electron.
 * @param preload - absolute path to the compiled preload script.
 * @returns the `webPreferences` for the shell's main window.
 */
export function desktopWebPreferences(preload: string): DesktopWebPreferences {
  return { preload, nodeIntegration: false, contextIsolation: true, sandbox: true, webSecurity: true, webviewTag: false }
}

/** The shell's own renderer origins. */
export interface LocalPagePolicy {
  /**
   * Exact origin of the shell's own page. W-21 D3 (#815): the window loads the
   * packaged UI from the local service, because the shipped frontend addresses
   * its API and live channel relative to its own origin
   * (`web/src/lib/wsStream.ts`: `${proto}//${window.location.host}/api/ws`),
   * which only resolves for the service origin.
   */
  readonly origin: string
  /** A vite dev-server origin allowed only while developing, e.g. `http://127.0.0.1:5173`. */
  readonly devServerOrigin?: string | undefined
}

/** What the shell should do with a navigation or new-window target. */
export type NavigationDecision =
  | { readonly kind: 'allow-local'; readonly url: string }
  | { readonly kind: 'open-external'; readonly url: string }
  | { readonly kind: 'deny'; readonly reason: string }

function parse(rawUrl: string): URL | undefined {
  try {
    return new URL(rawUrl)
  } catch {
    return undefined
  }
}

/** True when the URL is one of the shell's own local pages. */
export function isLocalAppPage(rawUrl: string, policy: LocalPagePolicy): boolean {
  const url = parse(rawUrl)
  if (url === undefined) return false
  if (url.origin === policy.origin) return true
  if (policy.devServerOrigin !== undefined) {
    const dev = parse(policy.devServerOrigin)
    if (dev !== undefined && url.origin === dev.origin) return true
  }
  return false
}

/**
 * The host token is attached only to the shell's own local pages — never to an
 * external URL, and never to a `file:`/`data:`/custom page that is not ours.
 */
export function shouldAttachHostToken(rawUrl: string, policy: LocalPagePolicy): boolean {
  return isLocalAppPage(rawUrl, policy)
}

/**
 * Decide where a navigation or `window.open` target may go.
 *
 * Local pages stay in the window. Any other `http(s)` target is handed to the
 * system browser (and gets no token). Every other scheme — `javascript:`,
 * `file:`, `data:`, `blob:`, custom — is denied outright.
 * @param rawUrl - the requested target.
 * @param policy - the shell's own origins.
 * @returns the decision.
 */
export function decideNavigation(rawUrl: string, policy: LocalPagePolicy): NavigationDecision {
  const url = parse(rawUrl)
  if (url === undefined) return { kind: 'deny', reason: 'URL 无法解析' }
  if (isLocalAppPage(rawUrl, policy)) return { kind: 'allow-local', url: url.href }
  if (url.protocol === 'http:' || url.protocol === 'https:') {
    // Credentials in the URL must never reach an external launcher verbatim.
    const external = new URL(url.href)
    external.username = ''
    external.password = ''
    return { kind: 'open-external', url: external.href }
  }
  return { kind: 'deny', reason: `不允许的协议：${url.protocol}` }
}

/** True for the shell's own top frame — the only frame allowed to use privileged IPC. */
export function isOwnedFrame(args: { frameIsMain: boolean; isLocal: boolean }): boolean {
  return args.frameIsMain && args.isLocal
}

/**
 * Redact material that must never appear in a diagnostic log the user can open.
 *
 * The W-11 token travels as `Authorization: Bearer …` and model credentials may
 * appear as `"api_key": "…"`; both are replaced by `[redacted]`.
 * @param text - raw log text.
 * @returns the same text with credential-shaped values removed.
 */
export function redactDiagnostics(text: string): string {
  return text
    .replace(/(authorization\s*:\s*bearer\s+)\S+/giu, '$1[redacted]')
    .replace(/("(?:api[_-]?key|token|secret|password)"\s*:\s*")([^"]*)(")/giu, '$1[redacted]$3')
    .replace(/(\bsk-[A-Za-z0-9_-]{8,})/gu, '[redacted]')
}
