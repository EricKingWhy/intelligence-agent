/**
 * Shell-owned English and Chinese copy for the thin host.
 *
 * The Electron shell owns these strings (the renderer keeps its own copy from the
 * web build). Kept intentionally small: only the strings the shell itself shows.
 */

export const en = {
  aboutProduct: 'Intelligence Agent',
  openApplication: 'Open Intelligence Agent',
  quitApplication: 'Quit Intelligence Agent',
  quit: 'Quit',
  cancel: 'Cancel',
  quitTitle: 'Quit Intelligence Agent?',
  quitActiveTasks: 'The desktop-managed task will be safely paused before the app exits. It keeps running while another client still manages it.',
  backgroundNoticeBody: 'The window is hidden to the tray; running tasks keep going. Reopen it from the tray.',
  backgroundNoticeConfirm: 'Got it',
  startupFailed: 'The Intelligence Agent service could not start',
  startupSummary: 'The local service did not become ready.',
  startupRetry: 'Retry',
  startupOpenLog: 'Open redacted log',
  startupExit: 'Exit',
  startupLogOpened: 'Redacted log: {path}',
  startupNoLog: 'No log file is available yet.',
} as const

/** Every shell locale supplies the complete English key set. */
export type DesktopMessages = { readonly [Key in keyof typeof en]: string }

export const zh = {
  aboutProduct: 'Intelligence Agent',
  openApplication: '打开 Intelligence Agent',
  quitApplication: '退出应用',
  quit: '退出',
  cancel: '取消',
  quitTitle: '退出 Intelligence Agent？',
  quitActiveTasks: '退出前会安全暂停本桌面托管的任务；只要还有其它客户端托管同一任务，任务会继续运行。',
  backgroundNoticeBody: '窗口已隐藏到托盘，正在运行的任务会继续。可从托盘重新打开。',
  backgroundNoticeConfirm: '知道了',
  startupFailed: 'Intelligence Agent 服务无法启动',
  startupSummary: '本机服务未能就绪。',
  startupRetry: '重试',
  startupOpenLog: '打开脱敏日志',
  startupExit: '退出',
  startupLogOpened: '脱敏日志：{path}',
  startupNoLog: '暂时没有可用的日志文件。',
} as const satisfies DesktopMessages

/** Locale payload used by the shell-owned dialogs and tray. */
export interface DesktopLocale {
  readonly id: 'en' | 'zh-CN'
  readonly messages: DesktopMessages
}

/** Resolve the OS locale to one shipped dictionary, falling back to English. */
export function resolveDesktopLocale(locale: string): DesktopLocale {
  return locale.toLowerCase().startsWith('zh')
    ? { id: 'zh-CN', messages: zh }
    : { id: 'en', messages: en }
}

/** Replace named placeholders in one shell-owned message. */
export function formatDesktopMessage(
  message: string,
  values: Readonly<Record<string, string>>,
): string {
  return message.replaceAll(/\{([^{}]+)\}/gu, (placeholder, key: string) => values[key] ?? placeholder)
}
