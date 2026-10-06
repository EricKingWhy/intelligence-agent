/**
 * One-time Windows confirmation before hiding the application in the tray.
 *
 * ADAPTED from DeepSeek Harness (MIT License):
 *   apps/desktop/src/background-notice.ts:1-56
 *   https://github.com/deepseek-ai/deepseek-harness
 *   commit 5badb15009ae1756c3afe0ae0cef1faafc290ccc
 * Full MIT text and provenance ledger: desktop/THIRD_PARTY_NOTICES.md.
 * Changes: the locale module is this package's own; wording states that running
 * tasks keep going. Dialog flow and the marker-file acknowledgement are unchanged.
 */

import { existsSync, mkdirSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import type { MessageBoxOptions, MessageBoxReturnValue } from 'electron'
import type { DesktopLocale } from './messages.ts'

/** Persistent acknowledgement and the shared shell dialog. */
export interface DesktopBackgroundNoticeOptions {
  /** Acknowledgement under Electron userData; updates retain it and uninstall removes it. */
  readonly markerPath: string
  readonly locale: () => DesktopLocale
  readonly show: (options: MessageBoxOptions) => Promise<MessageBoxReturnValue>
  readonly focus: () => void
}

/** Only an explicit acknowledgement permits the first hide; cancelled prompts remain eligible. */
export class DesktopBackgroundNotice {
  private acknowledged = false
  private pending = false
  private disposed = false

  /** @param options - Marker path, localized copy, and shell dialog actions. */
  private readonly options: DesktopBackgroundNoticeOptions

  constructor(options: DesktopBackgroundNoticeOptions) {
    this.options = options}

  /**
   * Request a window hide, prompting until acknowledged and coalescing repeated requests.
   * @param hide - Hide the still-owned window after acknowledgement, or immediately when already recorded.
   */
  close(hide: () => void): void {
    if (this.disposed) return
    if (this.pending) { this.options.focus(); return }
    if (this.acknowledged || existsSync(this.options.markerPath)) { hide(); return }
    this.pending = true
    void this.confirm(hide)
  }

  /** Ignore late dialog responses after application shutdown begins. */
  dispose(): void { this.disposed = true }

  private async confirm(hide: () => void): Promise<void> {
    try {
      const { messages } = this.options.locale()
      const result = await this.options.show({ type: 'info', title: messages.aboutProduct,
        message: messages.backgroundNoticeBody, buttons: [messages.backgroundNoticeConfirm], defaultId: 0, cancelId: -1 })
      if (this.disposed || result.response !== 0) return
      this.acknowledged = true
      try {
        mkdirSync(dirname(this.options.markerPath), { recursive: true })
        writeFileSync(this.options.markerPath, '')
      } catch (error) { console.warn('desktop tray: could not record background confirmation', error) }
      hide()
    } catch (error) { console.warn('desktop tray: background confirmation unavailable', error) }
    finally { this.pending = false }
  }
}
