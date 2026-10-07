/**
 * #361 [W-16] Explicit user-data cleanup.
 *
 * Uninstall NEVER deletes user data (see installer/installer.nsh
 * customUnInstall). When the user really wants the data gone, this script is
 * the separate, explicit action — and it previews first:
 *
 *   node scripts/clean-user-data.mjs --preview   # list targets, delete nothing
 *   node scripts/clean-user-data.mjs --confirm   # delete file targets
 *
 * Running without --preview/--confirm is an error (never silent).
 * Windows credentials live in the Windows Credential Manager, not in files:
 * they are listed, never touched — the report tells the user how to remove
 * them by hand.
 */
import { existsSync, readdirSync, rmSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

/** File-backed data kinds under the data dir (plus the credential store). */
const FILE_TARGETS = [
  { kind: 'sessions', dir: 'sessions', describe: 'SessionEvent history' },
  { kind: 'artifacts', dir: 'artifacts', describe: 'stored artifacts' },
  { kind: 'workspace', dir: 'workspace', describe: 'workspace files' },
  { kind: 'logs', dir: 'logs', describe: 'install/upgrade/uninstall logs' },
  { kind: 'cache', dir: 'cache', describe: 'caches' },
]

function dirSizeBytes(dir) {
  let total = 0
  let entries = []
  try {
    entries = readdirSync(dir, { withFileTypes: true })
  } catch {
    return 0
  }
  for (const entry of entries) {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) total += dirSizeBytes(full)
    else {
      try {
        total += statSync(full).size
      } catch { /* raced deletion: ignore */ }
    }
  }
  return total
}

function childNames(dir) {
  try {
    return readdirSync(dir).filter((n) => n !== '.' && n !== '..').slice(0, 20)
  } catch {
    return []
  }
}

/**
 * Inventory the data dir. Pure-ish (reads the fs only), testable.
 * @returns targets with kind/path/sizeBytes/evidenceRefs (session ids,
 * artifact names, ... — the "evidence ref impact" preview #361 requires).
 */
export function collectDataTargets(dataDir) {
  const targets = []
  for (const { kind, dir, describe } of FILE_TARGETS) {
    const path = join(dataDir, dir)
    if (!existsSync(path)) continue
    targets.push({
      kind,
      path,
      describe,
      sizeBytes: dirSizeBytes(path),
      evidenceRefs: childNames(path),
    })
  }
  // Credentials are NOT files: never delete them here, just disclose.
  targets.push({
    kind: 'credentials',
    path: 'Windows Credential Manager (vault)',
    describe: 'model provider keys managed by the OS credential vault',
    sizeBytes: 0,
    evidenceRefs: [],
    manualOnly: true,
  })
  return targets
}

/**
 * Run preview or confirm. `stdout` is injectable for tests.
 * @returns { deleted: boolean, targets, skipped }
 */
export async function runCleanUserData({ dataDir, mode, stdout }) {
  if (mode !== 'preview' && mode !== 'confirm') {
    throw new Error('refusing to run: pass --preview (list only) or --confirm (delete)')
  }
  const targets = collectDataTargets(dataDir)
  const skipped = []
  for (const target of targets) {
    const refs = target.evidenceRefs.length > 0 ? ` refs=[${target.evidenceRefs.join(', ')}]` : ''
    stdout.write(`${target.kind}: ${target.path} (${target.sizeBytes} bytes)${refs}\n`)
    if (target.manualOnly) {
      stdout.write('  -> manual only: remove via Windows Credential Manager; this script never touches it\n')
      skipped.push(target)
    }
  }
  if (mode === 'preview') {
    stdout.write(`preview: ${targets.length} targets listed, nothing deleted\n`)
    return { deleted: false, targets, skipped }
  }
  for (const target of targets) {
    if (target.manualOnly) continue
    rmSync(target.path, { recursive: true, force: true })
    stdout.write(`deleted ${target.path}\n`)
  }
  return { deleted: true, targets, skipped }
}

function defaultDataDir() {
  if (process.env.IA_DATA_DIR) return process.env.IA_DATA_DIR
  if (process.platform === 'win32' && process.env.APPDATA) {
    return join(process.env.APPDATA, 'intelligence-agent')
  }
  throw new Error('cannot determine the data dir: set IA_DATA_DIR')
}

const isMainModule =
  process.argv[1] !== undefined && import.meta.url === pathToFileURL(process.argv[1]).href
if (isMainModule) {
  const args = process.argv.slice(2)
  const mode = args.includes('--confirm') ? 'confirm' : args.includes('--preview') ? 'preview' : 'none'
  ;(async () => {
    await runCleanUserData({ dataDir: defaultDataDir(), mode, stdout: process.stdout })
  })().catch((error) => {
    console.error(error instanceof Error ? error.message : error)
    process.exit(1)
  })
}
