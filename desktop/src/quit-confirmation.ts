/**
 * Native quit confirmation; quitting proceeds silently only when the inspection reports nothing to interrupt.
 *
 * ADAPTED from DeepSeek Harness (MIT License):
 *   apps/desktop/src/quit-confirmation.ts:1-96
 *   https://github.com/deepseek-ai/deepseek-harness
 *   commit 5badb15009ae1756c3afe0ae0cef1faafc290ccc
 * Full MIT text and provenance ledger: desktop/THIRD_PARTY_NOTICES.md.
 * Changes: the inspection type is this package's own `DesktopQuitInspection`
 * (busy / unknown) instead of DSH's active/scheduled-task shape, so
 * `resolveDesktopQuitPrompt` now has a single conservative prompt. The
 * "unknown warns rather than quitting silently" rule and the one-decision-at-a-time
 * class are unchanged.
 */

import type { MessageBoxOptions, MessageBoxReturnValue, NativeImage } from 'electron'
import type { DesktopQuitInspection } from './quit-inspection.ts'
import type { DesktopLocale, DesktopMessages } from './messages.ts'

/** Locale key of the explanation shown before quitting, or none when quitting interrupts nothing. */
export type DesktopQuitPrompt = Extract<keyof DesktopMessages, 'quitActiveTasks'> | undefined

/**
 * Choose the confirmation copy for one inspection result.
 * @param inspection - Inspection answer, or `unknown` when the inspection failed or missed its deadline.
 * @returns the explanation key; an unknown state warns about running tasks rather than quitting silently.
 */
export function resolveDesktopQuitPrompt(inspection: DesktopQuitInspection | 'unknown'): DesktopQuitPrompt {
  if (inspection === 'unknown') return 'quitActiveTasks'
  if (inspection.busy) return 'quitActiveTasks'
  return undefined
}

/** Main-process collaborators of the confirmation. */
export interface DesktopQuitConfirmationOptions {
  readonly locale: () => DesktopLocale
  /** Start one inspection; undefined while no backend is ready, when nothing can be running and the quit proceeds. */
  readonly inspect: () => Promise<DesktopQuitInspection | 'unknown'> | undefined
  /** Native message box without an owner window: hidden windows stay hidden and the box comes to the front. */
  readonly show: (options: MessageBoxOptions) => Promise<MessageBoxReturnValue>
  /** Bring the open confirmation to the front when quit is requested again while it is open. */
  readonly focus: () => void
  readonly platform?: NodeJS.Platform
  /** Application icon for the Windows task dialog. */
  readonly icon?: NativeImage
}

/** One quit decision at a time; repeated quit requests join the open confirmation instead of stacking. */
export class DesktopQuitConfirmation {
  private pending: Promise<boolean> | undefined
  private disposed = false

  /** @param options - Locale, inspection, and native dialog collaborators. */
  private readonly options: DesktopQuitConfirmationOptions

  constructor(options: DesktopQuitConfirmationOptions) {
    this.options = options}

  /**
   * Decide whether the quit may proceed. The inspection runs once per decision; work that starts or ends
   * while the confirmation is open does not change its copy, and approval never re-inspects.
   * @returns true to quit now, false when the user cancelled.
   */
  confirm(): Promise<boolean> {
    if (this.disposed) return Promise.resolve(false)
    if (this.pending !== undefined) {
      this.options.focus()
      return this.pending
    }
    const pending = this.decide().finally(() => { if (this.pending === pending) this.pending = undefined })
    this.pending = pending
    return pending
  }

  /**
   * The application is quitting through a path that does not ask: a pending decision resolves to
   * false without opening a box, and later requests do the same.
   */
  dispose(): void { this.disposed = true }

  private async decide(): Promise<boolean> {
    const inspection = this.options.inspect()
    if (inspection === undefined) return true
    const prompt = resolveDesktopQuitPrompt(await inspection.catch((error: unknown) => {
      console.warn('desktop quit: task inspection unavailable', error)
      return 'unknown' as const
    }))
    if (this.disposed) return false
    if (prompt === undefined) return true
    const { messages } = this.options.locale()
    const windows = (this.options.platform ?? process.platform) === 'win32'
    // Button order follows each platform: macOS lays buttons out right-to-left from index 0, so
    // "Quit" sits right of "Cancel"; the Windows task dialog keeps array order, "Quit" left of "Cancel".
    const result = await this.options.show({
      type: windows ? 'none' : 'warning',
      ...(windows && this.options.icon !== undefined ? { icon: this.options.icon } : {}),
      title: messages.aboutProduct,
      message: messages.quitTitle,
      detail: messages[prompt],
      buttons: [messages.quit, messages.cancel],
      defaultId: 0,
      cancelId: 1,
      noLink: true,
    })
    // A bypassing quit can dispose while the box is open.
    if (this.disposed) return false
    return result.response === 0
  }
}
