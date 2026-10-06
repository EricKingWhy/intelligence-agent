/**
 * Typed preload operations exposed only by this Electron shell.
 *
 * ADAPTED from DeepSeek Harness (MIT License):
 *   apps/desktop/src/ipc.ts:1-104 (channel-name table, custom scheme, and
 *   `assertDesktopSender`)
 *   https://github.com/deepseek-ai/deepseek-harness
 *   commit 5badb15009ae1756c3afe0ae0cef1faafc290ccc
 * Full MIT text and provenance ledger: desktop/THIRD_PARTY_NOTICES.md.
 * Changes: channel names, the exposed surface (directory pick / window state /
 * service bootstrap — no shortcuts, browser, updates, or credentials), and the
 * product API type are reduced to the W-15 instruction. The sender guard is kept.
 */

import type { IpcMainInvokeEvent } from 'electron'

/** IPC channel names kept private to the desktop application bundle. */
export const DESKTOP_IPC = {
  bootstrap: 'ia-desktop:bootstrap',
  directoryPick: 'ia-desktop:directory-pick',
  windowState: 'ia-desktop:window-state',
} as const

/** Scheme of shell-owned application documents. */
export const SCHEME = 'ia-app'

/** Host of the packaged page. */
export const APP_HOST = 'app'

/** Non-secret service connection facts handed to the local page. Never the token. */
export interface DesktopBootstrap {
  readonly origin: string
  readonly protocolVersion: 1
}

/** Window state the local page may render. */
export interface DesktopWindowState {
  readonly maximized: boolean
  readonly fullscreen: boolean
}

/**
 * The restricted bridge. Deliberately has no arbitrary file access, no raw
 * `ipcRenderer`, and no credentials — the W-15 security instruction.
 */
export interface IaDesktopBridge {
  readonly protocolVersion: 1
  bootstrap(): Promise<DesktopBootstrap>
  pickDirectory(): Promise<string | null>
  windowState(): Promise<DesktopWindowState>
}

/**
 * Reject IPC outside the allowed shell document origins.
 * @param event - IPC caller whose frame URL supplies the origin.
 * @param hostnames - shell document hosts allowed for this operation.
 */
export function assertDesktopSender(event: IpcMainInvokeEvent, hostnames: readonly string[] = [APP_HOST]): void {
  const senderFrame = event.senderFrame
  if (senderFrame === null) throw new Error('ia desktop: rejected IPC without a sender frame')
  let url: URL
  try {
    url = new URL(senderFrame.url)
  } catch {
    throw new Error('ia desktop: rejected IPC from an unparseable frame URL')
  }
  if (url.protocol !== `${SCHEME}:` || !hostnames.includes(url.hostname)) {
    throw new Error('ia desktop: rejected IPC from an unowned renderer')
  }
}
