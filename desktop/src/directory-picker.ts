/**
 * Window-owned workspace directory dialogs for the local shell renderer.
 *
 * ADAPTED from DeepSeek Harness (MIT License):
 *   apps/desktop/src/directory-picker.ts:1-30
 *   https://github.com/deepseek-ai/deepseek-harness
 *   commit 5badb15009ae1756c3afe0ae0cef1faafc290ccc
 * Full MIT text and provenance ledger: desktop/THIRD_PARTY_NOTICES.md.
 * Changes: channel names, error text, and the sender guard now come from this
 * package's own ipc module. The single-flight-per-window behaviour (cancelling
 * has no side effect) is unchanged.
 */

import { dialog, ipcMain, type BrowserWindow } from 'electron'
import { DESKTOP_IPC, assertDesktopSender } from './ipc.ts'

/**
 * Install the application-lifetime directory picker IPC handler.
 * @param getWindow - Current local application window; shell pages and subframes cannot open dialogs.
 * @param allowedOrigins - Origins of the shell's own document (see `assertDesktopSender`).
 */
export function installDesktopDirectoryPicker(
  getWindow: () => BrowserWindow | undefined,
  allowedOrigins: () => readonly string[],
): void {
  const pending = new WeakMap<BrowserWindow, Promise<string | null>>()
  ipcMain.handle(DESKTOP_IPC.directoryPick, async (event) => {
    const window = getWindow()
    if (window === undefined || window.isDestroyed() || event.sender !== window.webContents
      || event.senderFrame !== window.webContents.mainFrame) {
      throw new Error('ia desktop: rejected directory picker from an unowned renderer')
    }
    assertDesktopSender(event, allowedOrigins())
    const existing = pending.get(window)
    if (existing !== undefined) return existing
    if (window.isMinimized()) window.restore()
    window.show()
    window.focus()
    const result = dialog.showOpenDialog(window, { properties: ['openDirectory', 'createDirectory'] }).then(
      ({ canceled, filePaths }) => window.isDestroyed() || canceled ? null : filePaths[0] ?? null,
    ).finally(() => { pending.delete(window) })
    pending.set(window, result)
    return result
  })
}
