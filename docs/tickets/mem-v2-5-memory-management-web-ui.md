# #301 / MEM-V2-5 — Memory management Web UI

**Priority:** P2

**Parent GitHub issue:** #296

**Parent PRD:** `docs/PRD_PRODUCTION_LONG_TERM_MEMORY_V2.md`

## Objective

Deliver an accessible Web UI in which users can understand, search, inspect, edit, disable, and delete their long-term memories without the browser becoming a second source of truth.

## Context

The existing Memory panel lists and deletes memories with loading/empty/error/pagination states. The V2 API adds typed records, search/filter, versions, edits, bulk deletion, independent extraction/recall settings, and per-session recall explanations. Automatic updates must be visible but unobtrusive: default text is `已更新 N 条记忆`, with content hidden until expanded.

## Current Behavior

- The panel shows basic content/scope/time and supports single hard-delete confirmation.
- It does not expose type/status/source/version/project scope, editing, bulk deletion, settings, or why-recalled information.
- Existing local hook state is a recent API view, not durable truth; this invariant must remain.

## Desired Behavior

Users can manage all authorized V2 memories using the API as truth. Destructive actions are explicit and recover from concurrent changes. Automatic update notices reveal only a count by default. All workflows work with keyboard/screen-reader behavior and at both supported viewport widths.

## Scope

### Must Do

- Extend the Memory panel for search, kind/status/scope filters, pagination, and refresh.
- Display kind, tier/scope, status, source type, project/global distinction, timestamps, and version/source summary.
- Add detail/version inspection and editing of canonical content plus kind-matching payload.
- Add single delete, delete-by-kind, and delete-all confirmation workflows.
- Add independent automatic extraction and recall settings.
- Display per-session “why recalled” information from the backend contract.
- Display `已更新 N 条记忆` after committed updates and reveal contents only after an explicit expand action.
- Display authorized tombstones through the existing deleted-status, detail, and version read routes, with a strict content-free response.
- Preserve loading, empty, partial/degraded, forbidden/not-found, retry, stale/concurrent-change, and general failure states.

### Must Not Do

- Do not cache a second durable memory database in the browser.
- Do not optimistically claim a mutation succeeded before the server confirms it.
- Do not show deleted content from tombstones or raw evidence excerpts unavailable through the API.
- Do not expose another user's/project's existence through error copy.
- Do not redesign unrelated application surfaces.

## Requirements

- **R1:** Filters and search are server-backed and reflected in the rendered result; clearing filters returns the unfiltered authorized view.
- **R2:** Editing validates required per-kind fields and 500-character canonical content before submission, while server errors remain authoritative.
- **R3:** User-edit version creation is shown after refresh with version/source status; superseded history is inspectable but not presented as active.
- **R4:** Single delete has item-level confirmation. Kind/all bulk delete shows the exact target category/count available from the server and requires the API confirmation token.
- **R5:** Extraction and recall toggles are independent, persisted by the server, and retain their previous confirmed value on mutation failure.
- **R6:** Recall explanations show the approved redacted fields and never fabricate ranking reasons client-side.
- **R7:** Automatic update notifications show only count by default; expand fetches/displays authorized updated records. Zero logical updates show no update notification.
- **R8:** Concurrent 404/stale-version responses remove or refresh stale rows while leaving a visible, dismissible explanation.
- **R9:** All interactive controls have programmatic names, visible focus, keyboard operation, and correct dialog focus restoration.
- **R10:** Tombstone reads require the matching trusted identity and resolved project context; expired and unauthorized tombstones fail closed. Responses contain only `id`, `root_id`, `scope`, `project_id`, `status="deleted"`, and `deleted_at`. Deleted search matches only opaque IDs; kind filtering returns no tombstones because tombstones retain no kind.
- **R11 (user-approved PRD §5.4 exception, 2026-09-25):** Tombstone history preserves descending original version order using an internal ordinal that is not returned by the API. Existing tombstones without an ordinal use deterministic deletion-time/ID ordering until their 30-day expiry; their original version order cannot be recovered.

## Contracts

The UI consumes the APIs in PRD §6.4 and event behavior in §6.5. Authorized tombstone reads use the existing `GET /api/memories?status=deleted`, `GET /api/memories/{id}`, and `GET /api/memories/{id}/versions` paths. Every tombstone response has exactly six fields: `id`, `root_id`, `scope`, `project_id`, `status="deleted"`, and `deleted_at`; it never exposes content, payload, evidence, hashes, deletion reason, identity fields, or version numbers. Under the user-approved PRD §5.4 exception dated 2026-09-25, the store retains a private ordinal solely to preserve descending version order for tombstones created after this change; it is not returned by the API and expires with the tombstone. Existing tombstones lack that ordinal and remain deterministically ordered by deletion time and opaque ID until their 30-day expiry; their original version order cannot be recovered. Search matches only opaque tombstone IDs/root IDs, and kind filters return no tombstones. Required Chinese default notification text is exactly `已更新 N 条记忆` with N substituted as a base-10 count. Existing Web UI remains a projection of backend truth.

## Implementation Freedom

The agent may choose component decomposition, state-management details, responsive layout, iconography, and non-contractual copy. It should reuse existing panel, hooks, dialogs, error parsing, theme tokens, and accessibility patterns when they satisfy the requirements.

## Acceptance Criteria

- **AC1:** Search plus every kind/status/scope filter sends the correct API contract and renders only returned records.
- **AC2:** Detail/version view clearly distinguishes active, superseded, invalidated, and deleted-without-content states.
- **AC3:** A valid edit creates and displays a new user-edit version; invalid per-kind or over-limit input cannot be submitted.
- **AC4:** Single, kind, and all delete workflows require confirmation, render server-confirmed affected counts, and never display deleted content afterward.
- **AC5:** Extraction/recall toggles survive reload and roll back visually on a failed request.
- **AC6:** “Why recalled” displays backend-provided redacted factors and handles absent/degraded capability honestly.
- **AC7:** Automatic update shows only `已更新 N 条记忆` until expanded; expansion displays only records authorized by a fresh API response.
- **AC8:** Loading, empty, degraded, retryable error, 403/404, stale edit, and pagination boundaries are covered by component and browser tests.
- **AC9:** Full browser flows pass at both supported viewports with keyboard-only navigation and dialog focus assertions.
- **AC10:** TypeScript, Vitest, Oxlint, production build, and relevant Playwright suites pass without new warnings/errors attributable to this ticket.

## Dependencies

```text
blocked_by: #300 (MEM-V2-4)
blocks: #304 (MEM-V2-8)
parallelizable: with #302 and #303 once #300 is integrated
```

## Verification

- API parser/type tests using real backend response shapes, including strict content-free tombstones.
- Backend authorization tests for owner, cross-user, matching/mismatched project context, and expired tombstones.
- Component tests for filters, edit validation, versions, settings, notification disclosure, and failures.
- Playwright user flows at both viewport projects, including keyboard/focus behavior.
- Real browser smoke against the integrated backend for one create/edit/recall/delete lifecycle.
- Frontend typecheck, Vitest, Oxlint, production build, and full required Playwright gate before integration.

## Definition of Done

- The UI exposes every required governance behavior without maintaining durable truth.
- Accessibility and both viewport proofs are attached.
- No deleted/unauthorized content is visible in UI or browser artifacts.
- Review coverage, tracker, and PHASE_STATUS are updated after integration.
