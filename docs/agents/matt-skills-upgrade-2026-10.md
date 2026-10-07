# Matt skills 1.3.1 compatibility migration

## Scope and source

User requested an upgrade across Codex, ZCode, Claude Code and WorkBuddy, preserving compatibility with this project's older Matt workflow. Upstream: https://github.com/mattpocock/skills/tree/2237a047bd95abfd1427df94f65165c39e24d3c1 (plugin version 1.3.1). Existing plugin version: 1.2.3. This is a documentation and developer-host upgrade; the Engineering Specification and runtime code remain frozen.

## Compatibility patch

The original CONTEXT glossary is copied byte-for-byte to GLOSSARY.md. CONTEXT.md remains a pointer for legacy readers. Active AGENTS/CLAUDE/domain pointers use GLOSSARY; old historical and frozen references continue to resolve through CONTEXT. Routing and host adaptation have a single authority: SDD_WORKFLOW_PROTOCOL.md §9.1. The upstream skill files and manual/implicit invocation metadata stay unchanged.

## Host rollout

The 27 promoted skills are staged outside discovery roots before activation. Codex and ZCode retain canonical directory names; WorkBuddy retains mp-eng-/mp-prod- directory prefixes while keeping canonical frontmatter names. Claude Code's official marketplace currently pins c55ee46073ed923f86ce59a5eb3b6d895095d1b7 (1.2.3); a normal plugin update cannot deliver the audited 1.3.1. Activation therefore requires a native, separately named pinned marketplace for the original upstream package and disabling the older official plugin, avoiding duplicate active skills.

## Write-direction audit

Skill instructions describe methods and expected artifacts; they do not grant file or Git permissions. The previous four-skill whitelist was incomplete: research writes repository Markdown, prototype and wizard create artifacts, and writing-for-agents is a reference for any agent-consumed document. SDD_WORKFLOW_PROTOCOL.md §9.1 now applies the existing Task authorization (AGENTS §4.4) to all skills and preserves file-specific rules. Git authorization remains separate under AGENTS §14.4. There is no default read-only classification based on omission from a skill list.
## Rollback and verification

Back up each existing Matt directory and Claude plugin registry/settings outside skill discovery roots before activation. Keep the old resolving-merge-conflicts copy in that backup, not in an active skills directory. Roll back host files/registrations from those backups; preserve unrelated skills, plugins and settings. Verify staged and installed file manifests against the pinned source, metadata preservation, legacy glossary byte identity, pointer targets, project routes, and git diff --check. Directory verification is not evidence of a live model invoking each host's skills; report that distinction.

## Delivery status

Compatibility replay starts at 1ba082cd on docs/matt-skills-1.3.1-compat. Six documents retain the reviewed c1c2c7ba content; the protocol additionally contains the corrected Task-authorization rule and the complete 1.3.1 TDD quotation. The verification map retains mainline rows. User approved local compatibility synchronization and all four host updates on 2026-10-08; User separately approved push, PR creation and PR merge on 2026-10-08; required GitHub gate0 must pass before merging. Targeted verification and rollout results are recorded below.

## Approved local rollout — 2026-10-08

User approved Task-authorization repair, local compatibility synchronization and four-host activation. All three project checkouts now retain the original glossary in GLOSSARY.md, the five-line CONTEXT.md pointer, unchanged AGENTS headings, the per-ticket implement route, and their existing verification-map rows. Their current branches are preserved; this is a documentation-only local application, not a merge of their unrelated code histories.

| Host | Installed source | Verification |
| --- | --- | --- |
| Codex | User skills, canonical folders | 27 skills / 79 files match the pinned upstream |
| ZCode | User skills, canonical folders | 27 skills / 79 files match the pinned upstream |
| WorkBuddy | Existing mp-eng-/mp-prod- folder mapping, canonical frontmatter names | 27 skills / 79 files match the pinned upstream |
| Claude Code | mattpocock-skills@mattpocock-pinned-1-3-1 | Native CLI lists version 1.3.1 enabled and 27 skills; official 1.2.3 stays disabled |

Source commit: 2237a047bd95abfd1427df94f65165c39e24d3c1. File manifests include invocation metadata; retired resolving-merge-conflicts directories are kept outside discovery roots. Claude uses a native local marketplace with its plugin source pinned to this commit; marketplace strict validation passes.

Evidence: C:/Users/王浩宇/.codex/tmp/matt-upgrade-20261006/approved-rollout-verification.json. Rollback backup: C:/Users/王浩宇/.codex/backups/matt-skills/20261008-005157-approved-1.3.1. Restore only this task's host directories/settings, preserving subsequent unrelated changes. For Claude, disable the new plugin and restore the prior settings rather than enabling both sources.

Per user instruction, acceptance uses targeted file, pointer, route, chapter, manifest, registration and diff checks. No A/B, full pytest or frontend full suite was run. Earlier temporary test-attribution claims are not acceptance evidence. No live model invocation is claimed; existing sessions may need reopening to refresh skills. Remote publication is approved on 2026-10-08. A docs-only release branch based on origin/main 9e1c065b carries this compatibility patch; publication and merge results are tracked in its GitHub PR and must not be inferred from the local rollout checks.
