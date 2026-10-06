/**
 * Preload: expose only the restricted bridge to the shell's own local page.
 *
 * Security instruction (W-15): `contextIsolation` + `sandbox` on, Node integration
 * off; the page gets directory picking, window state, and a non-secret service
 * bootstrap — never arbitrary file access, raw `ipcRenderer`, or credentials.
 * The host token never reaches the renderer at all: main injects it into the
 * loopback proxy it owns (src/main.ts), so the local page never holds it.
 */

import { contextBridge, ipcRenderer } from 'electron'
import { APP_HOST, DESKTOP_IPC, SCHEME, type DesktopBootstrap, type DesktopWindowState, type IaDesktopBridge } from './ipc.ts'

function createBridge(): IaDesktopBridge {
  return {
    protocolVersion: 1,
    bootstrap: () => ipcRenderer.invoke(DESKTOP_IPC.bootstrap) as Promise<DesktopBootstrap>,
    pickDirectory: () => ipcRenderer.invoke(DESKTOP_IPC.directoryPick) as Promise<string | null>,
    windowState: () => ipcRenderer.invoke(DESKTOP_IPC.windowState) as Promise<DesktopWindowState>,
  }
}

const isOwnLocalPage = location.protocol === `${SCHEME}:`
  && location.hostname === APP_HOST
  && process.isMainFrame

// An unowned renderer (a stray frame or a shell page) gets a version marker only;
// every privileged call is additionally re-checked in main by `assertDesktopSender`.
contextBridge.exposeInMainWorld('iaDesktop', isOwnLocalPage ? createBridge() : { protocolVersion: 1 })
