# Threat model: intelligence-agent

For the Anthropic OSS Scanner enrollment.
Honest framing first: **this is a personal-use open-source project, not
critical infrastructure.** It is a lightweight agent harness the maintainer
runs for himself. Severity ratings below reflect impact on a
single-user, self-hosted deployment — not on internet-scale infrastructure.

## What the project does

`intelligence-agent` is a general-purpose lightweight agent harness
(Python + asyncio, own main loop, not bound to LangGraph) focused on
observability (every run fully traceable), recoverability
(Resume/Replay/Fork/Checkpoint) and plugin extension. It ships a dark
three-pane web workbench ("Agent Harness Inspector") served from the same
process: HTTP API + Server-Sent Events + WebSocket.

The agent executes tools on behalf of the user, including **shell commands,
file read/write, git operations, and Docker-sandboxed code execution**.

## Components that matter (in scope)

1. **Docker sandbox** (`src/agent_harness/sandbox/`) — runs
   agent-generated shell commands and code. A sandbox escape means host
   command execution as the host user. This is the highest-value target.
2. **Web/API surface** (`src/agent_harness/web/`, `web/`) — HTTP API, SSE
   streams, WebSocket endpoints. An unauthenticated remote caller who can
   reach these can drive the agent's tools, i.e. run shell commands.
   (A past unauthenticated `/api/ws` issue was fixed; the area stays
   high-value.)
3. **Tool permission / approval path** (`src/agent_harness/tooling/`) —
   Contract → Registry → Validation → Permission → Scheduler → ToolExecutor.
   Bypasses here turn "needs approval" tools into silent execution.
4. **Session write path** (session service, JSONL event log, checkpoints) —
   tampering breaks the "single source of truth" the UI and resume logic
   rely on.
5. **MCP / skills / plugins** — third-party code and remote side effects
   loaded into the runtime.

## Where untrusted input enters

- **Task prompts**: user (or pasted third-party) text → LLM → tool calls.
  Prompt injection that steers tool use is an inherent LLM-agent risk; rate
  it by the *impact of the resulting tool action*, not as its own class.
- **Web Inspector inputs**: anything typed into the workbench → HTTP/SSE/WS.
- **MCP server configs, skill definitions, plugin packages**: third-party
  content with code or remote side effects.
- **Files under the workspace**: the agent reads/writes them; malicious
  content (e.g. instruction-like text in a repo file) is untrusted input.
- **Sandbox stdin/stdout**: agent-generated commands and their output.

## Severity guidance (how we rate reports)

- **Critical**: sandbox escape or host RCE; unauthenticated remote access
  to the web/API surface (equivalent to remote shell); authentication
  bypass on the API; exfiltration of credentials/tokens from the host.
- **High**: path traversal allowing file read/write outside the workspace;
  permission/approval bypass that silently executes gated tools; session
  store tampering that survives restart; XSS only if it leads to tool
  execution (plain self-XSS is medium — single-user deployment).
- **Medium**: stored/reflected XSS in the Inspector UI; information
  disclosure of session contents to a local attacker; missing security
  headers with a demonstrated consequence.
- **Low / informational**: hardening suggestions without a demonstrated
  impact (e.g. "add header X" with no exploit path).

Prompt-injection-driven tool misuse: rate by impact per the above scale.
We do not treat "the model can be talked into something" alone as a
vulnerability without a concrete tool/permission bypass.

## Out of scope

- Vulnerabilities *inside* third-party dependencies themselves — report the
  dependency and version; the fix on our side is a bump.
- The operator's own machine, physical access, or the operator being
  socially engineered.
- Model-provider-side issues (prompt handling by the upstream LLM API).

## How we like reports and patches

- One finding per report, with a reproducer we can run.
- Patches should follow the repo's existing structure and naming; minimal
  diffs preferred (see `AGENTS.md` §9 Surgical Changes).
- Reports go to the `primary_contact` in `project.yaml`.
