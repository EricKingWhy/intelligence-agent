/**
 * #361 [W-16] Windows installer smoke test.
 *
 * ADAPTED from DeepSeek Harness (MIT License):
 *   apps/desktop/scripts/test-windows-installer.mjs (lines 1-125)
 *   https://github.com/deepseek-ai/deepseek-harness
 *   commit 5badb15009ae1756c3afe0ae0cef1faafc290ccc
 * Full MIT text and provenance ledger: desktop/THIRD_PARTY_NOTICES.md.
 *
 * What is kept from upstream: build an isolated native payload through the
 * production NSIS configuration with a random GUID per run
 * (`com.intelligence-agent.installertest.n<id>`), run the real install /
 * uninstall UI in both languages (en_US / zh_CN), `--uninstall-only` and
 * `--compile-only` modes, and the hard requirement that the UI checks run
 * on a real Windows x64 machine (anything else throws immediately).
 *
 * What is NOT reproduced: DSH's signing infrastructure, custom
 * electron-builder fork hooks, brand assets and updater machinery — this
 * repo's installer is stock electron-builder + installer/installer.nsh.
 *
 * Usage (on Windows x64):
 *   node scripts/test-windows-installer.mjs [--compile-only] [--uninstall-only]
 */
import { execFile } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'

if (process.platform !== 'win32' || process.arch !== 'x64') {
  throw new Error('Installer UI checks require an interactive Windows x64 desktop')
}

const execute = promisify(execFile)
const appRoot = fileURLToPath(new URL('..', import.meta.url))
const require = createRequire(import.meta.url)
const { build, Platform, Arch } = require('electron-builder')
const { getMakeNsisPath } = require('app-builder-lib/out/toolsets/windows.js')

const guid = randomUUID()
const id = guid.replaceAll('-', '')
const productName = `IA Installer Test ${id.slice(0, 8)}`
// Unique app id per run so parallel / repeated runs never collide in the
// registry or on disk.
const appId = `com.intelligence-agent.installertest.n${id}`
const uninstallOnly = process.argv.includes('--uninstall-only')
const compileOnly = process.argv.includes('--compile-only')

const outputRoot = join(appRoot, '.desktop-build', 'installer-tests')
await mkdir(outputRoot, { recursive: true })
const output = await mkdtemp(join(outputRoot, 'run-'))
const payload = join(output, 'payload')
await mkdir(join(payload, 'resources'), { recursive: true })

const { createWindowsInstallerConfig, validateInstallerScripts } = await import('./build-windows-installer.mjs')
const childOptions = { windowsHide: true, maxBuffer: 8 * 1024 * 1024 }

// #831: run the same build-time LangString assertion the production build runs,
// so a language-guard regression fails this smoke build early — before the
// per-language makensis runs below — instead of aborting inside makensis.
validateInstallerScripts(join(appRoot, 'installer'))

let succeeded = false
try {
  // A tiny native payload so the production NSIS script (with the atomic
  // directory swap in installer/installer.nsh) runs for real.
  const payloadSource = join(output, 'payload.nsi')
  await writeFile(payloadSource, `Unicode true
RequestExecutionLevel user
ManifestDPIAware true
Name "${productName}"
OutFile "${join(payload, `${productName}.exe`)}"
SilentInstall silent
Section
  FileOpen $0 "$EXEDIR\\\\launched.txt" w
  FileWrite $0 "launched"
  FileClose $0
SectionEnd
`)
  const compiler = await getMakeNsisPath()
  await execute(compiler.path, ['/V2', payloadSource], { ...childOptions, env: { ...childOptions.env, ...compiler.env } })

  for (const language of ['en_US', 'zh_CN']) {
    const languageOutput = join(output, language)
    await mkdir(languageOutput)
    const baseConfig = createWindowsInstallerConfig({
      version: '0.0.0-test',
      appId,
      productName,
      installerDir: join(appRoot, 'installer'),
    })
    await build({
      projectDir: appRoot,
      prepackaged: payload,
      targets: Platform.WINDOWS.createTarget(['nsis'], Arch.x64),
      publish: 'never',
      config: {
        ...baseConfig,
        extraMetadata: { name: `@ia-installer-test-${id.slice(0, 8)}/app-${id}` },
        directories: { output: languageOutput },
        // The target-level artifactName (from the production config) wins over
        // a top-level one, so the override has to live in `nsis`: without it
        // the build writes `<productName>-Setup-<version>.exe` while the smoke
        // script below is handed `installer-test.exe` and never runs (#831).
        nsis: {
          ...baseConfig.nsis,
          guid,
          installerLanguages: [language],
          artifactName: 'installer-test.exe',
        },
      },
    })
    if (compileOnly) continue
    const result = await execute(
      'powershell.exe',
      [
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
        join(appRoot, 'scripts', uninstallOnly ? 'windows-uninstall-smoke.ps1' : 'windows-installer-smoke.ps1'),
        '-Installer', join(languageOutput, 'installer-test.exe'),
        '-ProductName', productName,
        '-RegistryKey', guid,
        '-OutputDirectory', languageOutput,
      ],
      childOptions,
    )
    process.stdout.write(`${language}\n${result.stdout}`)
  }
  succeeded = true
} finally {
  process.stdout.write(`Installer test artifacts: ${output} (succeeded=${succeeded})\n`)
}
