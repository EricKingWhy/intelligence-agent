# ADR-0054 — Windows isolated sessions use private-volume workspaces with copy transfer

**Status**: Accepted (#1048 user decision, 2026-10-14)
**Related**: #1048; ADR-0001; ADR-0027; ADR-0051

## Context

The Windows PSEC backend grants a process write access to a host workspace path. On NTFS, a hard link inside that path can name the same file object as an outside path, so path authorization does not isolate writes to the workspace. A writable host bind mount has the same object-sharing problem even when the process runs under a different kernel or token.

## Decision

Windows isolated project sessions use a per-session private Docker named volume mounted at Linux /workspace. The selected host project directory is imported as a snapshot before the session can run; changes remain in the private workspace until the user explicitly exports a copy to a new destination. Existing PSEC session mappings migrate their current workspace snapshot to Docker on resume. If the Docker Engine or any transfer step is unavailable, creation or resume fails closed; it does not fall back to PSEC or local execution.

The container receives no writable host project bind mount. Runtime paths, project instructions, file tools, approvals, and fork copy operate through the Sandbox contract rather than treating /workspace as a host path. The host project path remains project provenance and import source. This decision does not claim that PSEC, ACL, or path checks isolate NTFS hard links.

The user invokes copy-out through `POST /api/sessions/{session_id}/workspace/export` with an absolute `destination_path`. The route uses the existing trusted-origin gate and host-directory path handling, and refuses an existing destination.

Workspace restore and hard-delete are serialized by a process-wide owner lock. The supported service deployment uses one process per sessions root; multi-worker processes sharing that root are unsupported because run ownership and active-session state are also process-local. Cross-process deployment requires coordinated session ownership and workspace locking before it can be supported.
