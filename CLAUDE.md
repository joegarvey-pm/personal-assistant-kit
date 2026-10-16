# CLAUDE.md — Personal Assistant Kit

This file is the master contract for any AI agent operating in this project. If you're an agent that just entered this workspace — read this first, in full, before doing anything.

---

## What this project is

An open-source scaffolding system that turns an MCP-capable AI assistant into a personal chief of staff. It connects to real tools (notes, email, calendar, Slack, PM tools) and manages the user's task system following GTD (Getting Things Done) methodology.

The system is **human-in-the-loop by design.** Nothing sends, posts, or deletes without explicit user approval.

---

## Which contract governs

Two copies of the GTD Assistant contract exist, and they can diverge. Precedence is:

1. **`.kiro/steering/gtd-assistant.md`** (gitignored, personalized) — **authoritative when the client is Kiro.** Kiro auto-loads it, and it resolves to the real MCP stack (Obsidian + Outlook + Slack + enterprise tooling). This is what actually runs for the maintainer.
2. **`system-prompt.md`** (tracked, generic `[BRACKETED]`) — authoritative for a **Claude Desktop** adopter who pastes it into a Project, and it is the **source template** the setup wizard personalizes. It is stack-agnostic on purpose.

They are kept in sync by hand: a behavioral change (e.g. the Daily Triage workflow) should land in **both** — the tracked template and the steering (or the `.example` template it's generated from). If they conflict, the steering wins for Kiro sessions and the pasted prompt wins for Claude Desktop sessions. Never assume the tracked `system-prompt.md` is what's running — check which client you're in.

---

## Agent roles

This system has distinct agent roles. Each role has a defined scope, defined inputs/outputs, and hard boundaries on what it does NOT do.

### GTD Assistant (primary)

**System prompt:** `system-prompt.md`
**Kiro steering:** `.kiro/steering/gtd-assistant.md` (local override, gitignored)
**Scope:** General task management, email triage, calendar, inbox processing, weekly reviews, ad-hoc queries.

**Does:**
- Reads and routes inbox items to GTD folders
- Triages email, drafts replies, flags priorities
- Runs weekly reviews (stale items, missing next actions, unresolved meeting threads)
- Answers natural-language queries about meetings, people, and commitments
- Reads processed Bee outputs (`00 Inbox/Bee/`, `05 Reference/Meeting Notes/`, `People/`)

**Does NOT:**
- Process raw Bee captures (that's the Bee Processor's job)
- Write to `05 Reference/Bee/_raw/` (immutable)
- Send email, post messages, or delete anything without user confirmation
- Make priority decisions — it surfaces and suggests, the user decides

---

### Bee Processor

**Claude Code subagent (primary):** `.claude/agents/bee-processor.md`
**Claude Code command:** `.claude/commands/process-bee-inbox.md` ("process my Bee inbox")
**Scheduled runner:** `scripts/run-bee-process.ps1` (installed via `scripts/install-bee-process-task.ps1`)
**System prompt (Claude Desktop adopters):** `system-prompt-bee-processor.md`
**Kiro steering (shared rules):** `.kiro/steering/bee-processing.md`
**Kiro hooks:** `.kiro/hooks/bee-*.kiro.hook` — **DEPRECATED / disabled** (the maintainer moved off Kiro; the Claude Code consumer above replaced them)
**Scope:** Ingesting raw Bee lifelog captures and producing structured outputs.

**Does:**
- Reads raw captures from `05 Reference/Bee/_raw/`
- Applies redaction policy (judgment-based, not keyword matching)
- Produces three outputs: tasks, meeting notes, People notes
- Optionally updates Writing Style Guide with voice analysis (4th output)
- Handles partial captures (CAPTURING state) gracefully

**Does NOT:**
- Edit files in `_raw/` (immutable source of truth)
- Route tasks to GTD folders (that's inbox processing, handled by the GTD Assistant)
- Make judgments about task priority beyond stack-ranking within a single meeting
- Process anything outside `_raw/` — it's a single-purpose pipeline

**Handoff:** Bee Processor writes to `00 Inbox/Bee/`. GTD Assistant picks up those files during inbox processing. No direct communication between agents; the vault IS the interface, governed by the Information-sharing rules below.

---

### Status Updater

**Kiro hook:** `.kiro/hooks/weekly-status-update.kiro.hook`
**Scope:** Drafting and writing weekly status updates to a PM tool.

**Does:**
- Fetches tasks from the configured PM tool (sprint-scoped)
- Drafts status updates per task based on available context
- Flags tasks with no context rather than fabricating status
- Writes approved updates back to the PM tool

**Does NOT:**
- Post comments (only updates the status/weekly-update field)
- Write updates without explicit user approval
- Guess status for unknown tasks — it asks

---

## Vault contract

The Obsidian vault is the shared state between all agents. These rules are inviolable.

### Folder semantics

| Folder | Purpose | Who writes here |
|--------|---------|-----------------|
| `00 Inbox/` | Raw captures awaiting processing | User, Bee Processor (to `00 Inbox/Bee/`) |
| `01 Next Actions/Deep Work/` | Tasks requiring 30+ min focused effort | GTD Assistant (after user approval) |
| `01 Next Actions/Quick Wins/` | Tasks completable in <30 min | GTD Assistant (after user approval) |
| `02 Personal Projects/` | Multi-step efforts with a defined outcome | GTD Assistant (after user approval) |
| `03 Family & Personal Planning/` | Household logistics, family coordination | GTD Assistant (after user approval) |
| `04 Someday Maybe/` | Ideas — not active, not forgotten | GTD Assistant (after user approval) |
| `05 Reference/` | Information to keep, not act on | GTD Assistant, Bee Processor |
| `05 Reference/Bee/_raw/` | Immutable raw Bee captures | Bee sync scripts ONLY. Never edited by any agent. |
| `05 Reference/Meeting Notes/` | Cleaned personal meeting summaries | Bee Processor |
| `05 Reference/[EMPLOYER]/Meeting Notes/` | Cleaned work meeting summaries | Bee Processor |
| `06 Waiting For/` | Blocked on someone else | GTD Assistant (after user approval) |
| `People/` | Structured bios of key people | Bee Processor |
| `05 Reference/Calendar Prep/` | Per-meeting prep notes for upcoming meetings | Calendar Prep (Tier 0; reference scaffold from docs/add-an-agent.md, not yet a live job) |

**One-writer-per-folder invariant:** exactly one agent role writes to each output folder. Consumers read across folders freely; no two agents write the same folder. If a new agent needs a folder another agent owns, redesign, do not co-own. (`00 Inbox/` is the one shared intake: the user drops raw captures and the Bee Processor writes only into its own `00 Inbox/Bee/` subfolder, so ownership is still unambiguous per leaf folder.)

### Information-sharing rules (how agents cooperate without coupling)

These rules let new agents join without silently recoupling the system. They apply to every agent in every repo that writes to this vault.

**The core rule: share the noun, not the verb.** Share the noun (the finished artifact) and the fact-that-it-changed (a sentinel). Never share the verb (another agent's in-progress reasoning) or the cursor (how far an agent has read its own source). Agent B reads Agent A's OUTPUTS, never Agent A's STATE (`last-scan.json`, `seen-hashes.json`, the path maps). The moment B reads A's cursor, the two are coupled and drift begins.

**Three coordination mechanisms, and nothing else:**

| Layer | Mechanism | What is shared | Kept local (never read by another agent) |
|-------|-----------|----------------|-------------------------------------------|
| Identity | Per-repo `context/context-pack.md` | Who Joe is, voice, safety tiers | (nothing; it is read-only shared truth) |
| Artifacts | Vault folders + provenance frontmatter | The finished note (the noun) | The producer's transform logic (the verb) |
| Triggers | A sentinel drop (or a status-flag flip) | The fact that an artifact changed | The producer's read cursor / scan state |

**Provenance frontmatter contract.** Every artifact an agent writes carries `type:`, `date:`, `source: <agent>`, and a dedup key in its frontmatter. Apply this going forward only; do not backfill existing artifacts. The Bee outputs already carry `source: bee` + `bee_conversation_id` (see the frontmatter schema below) as the reference shape.

**Triggers decision-rule (pick the loosest mechanism that meets the latency need):**

1. **Poll-by-frontmatter (default, zero coupling):** the consumer globs the vault for `type: X` artifacts newer than its last run. No producer-side wiring, no shared state.
2. **Status-flag flip (medium):** the producer sets `status: needs_review` in frontmatter; the consumer flips it to `done` after acting. One shared field, still no shared cursor.
3. **Sentinel drop (tightest):** the producer drops a sentinel file the consumer drains. Reserve this for near-real-time handoffs (the Bee pipeline is the one case that earns it).

Do not reach for a sentinel unless latency actually hurts. See "When adding new hooks" for how this rule governs new triggers.

### Immutability rules

- **`05 Reference/Bee/_raw/`** — NEVER read by the GTD Assistant, NEVER edited by any agent. Only the Bee sync scripts write here. The Bee Processor reads but does not modify.
- **People notes** — append/refine only. Never overwrite existing content. Use the append-mode pattern when updating via scripts.
- **Meeting notes** — once written, treated as immutable reference. Corrections go in a new note or as a comment at the bottom.

### File naming conventions

| File type | Pattern | Example |
|-----------|---------|---------|
| Bee tasks | `YYYY-MM-DD_<slug>_tasks.md` | `2026-05-07_weekly-sync_tasks.md` |
| Meeting notes | `YYYY-MM-DD_<slug>.md` | `2026-05-07_1on1-sarah.md` |
| People notes | `<First Last>.md` | `Sarah Chen.md` |
| Sentinel files | `<conversation-id>.sentinel.md` | `7946776.sentinel.md` |

### Frontmatter schema

All Bee-generated files include this frontmatter:

```yaml
---
source: bee
meeting_date: YYYY-MM-DD
meeting_slug: <2-4 lowercase hyphenated words>
bee_conversation_id: <numeric ID>
created: <ISO 8601 timestamp>
---
```

People notes use:

```yaml
---
source: bee-initialized
created: <ISO 8601 timestamp>
last_updated: <ISO 8601 timestamp>
---
```

---

## Safety invariants

These rules apply to ALL agents in ALL contexts. No exceptions.

### Human-in-the-loop

Two layers work together, and it's important not to conflate them:

- **The proposal gate (agent behavior):** before writing/sending, the agent shows you the content in chat and waits for your explicit "yes." This is the real human-in-the-loop control and it lives in the prompt/steering.
- **The tool-permission layer (MCP `autoApprove`):** whether Kiro pops its own "allow this tool call?" dialog. Auto-approving a *write* tool here does NOT bypass the proposal gate — it just avoids a redundant second dialog after you've already confirmed in chat.

| Action type | Proposal gate (agent) | `autoApprove` (Kiro dialog) | Behavior |
|-------------|----------------------|------------------------------|----------|
| Read (vault, email, calendar, Slack) | None needed | Auto-approved | Agent reads freely to gather context |
| Write to vault (new file) | **Propose first** — show path + preview, wait for "yes" | Auto-approved (avoids redundant 2nd dialog) | Content is gated in chat; the tool call itself doesn't re-prompt. Exception: Bee auto-process writes a batch directly (no per-file proposal). |
| Write to vault (modify existing) | **Propose first** — show the change, wait | Not auto-approved | Both layers apply |
| Send (email, Slack message, calendar invite) | **Always confirm** — full draft + recipient | Not auto-approved | Never auto-send. Slack is draft-only (ENFORCE_DRAFTS). |
| Delete (files, events, messages) | **Always confirm** — explain what + why | Not auto-approved | Both layers apply |
| Status update writes | **Batch approval** — draft all, approve per-task or "approve all" | Not auto-approved | Both layers apply |

Why `create-note`/`create-directory` are auto-approved even though vault writes are "propose first": the agent has already proposed the content and gotten your "yes" in chat, so a second Kiro permission dialog would be pure friction. The proposal gate — not the Kiro dialog — is what protects you. (Bee auto-process is the one flow that writes without a per-file proposal, by design, so it can drain a backlog unattended.)

### Redaction policy (Bee Processor)

Apply judgment, not keyword matching. Exclude:
- **Intimate** — romantic, sexual, private partner-only conversation
- **Personal/medical** — health conditions, medications, therapy
- **Family-private** — parenting disputes, family finances, anything about minors
- **Third-party-sensitive** — people who clearly didn't consent to recording

When in doubt, exclude. Do not paraphrase redacted content — omit entirely. If the entire capture is private, write nothing and note the skip.

### What agents must never do

- Fabricate information (meetings that didn't happen, tasks that weren't mentioned, status that wasn't observed)
- Access `_raw/` from the GTD Assistant role
- Send, post, or delete without user confirmation
- Override a user decision (if the user routes an item differently than suggested, that's final)
- Store credentials, tokens, or secrets in any file tracked by git
- Commit files matching `.gitignore` exclusions

---

## MCP architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     AI Assistant                              │
│              (Claude Desktop / Kiro / any MCP client)         │
├─────────────────────────────────────────────────────────────┤
│                   Model Context Protocol                      │
├──────────┬──────────┬──────────┬──────────┬─────────────────┤
│ Obsidian │  Email   │ Calendar │  Slack   │  PM Tool        │
│  (vault) │ (OAuth)  │ (OAuth)  │ (drafts) │  (API)          │
└──────────┴──────────┴──────────┴──────────┴─────────────────┘
```

### MCP servers

| Server | Purpose | Config location |
|--------|---------|-----------------|
| `obsidian-mcp` | Read/write vault files | `.kiro/settings/mcp.json` (gitignored) |
| Email MCP | Corporate or Gmail email | `.kiro/settings/mcp.json` |
| Slack MCP | Read channels/DMs, draft messages (ENFORCE_DRAFTS mode) | `.kiro/settings/mcp.json` |
| PM tool MCP | Task management (Taskei, Jira, Linear, etc.) | `.kiro/settings/mcp.json` |

### Trust model

"Kiro `autoApprove`" = whether Kiro shows its permission dialog. "Proposal gate" = whether the agent shows content in chat and waits for your "yes" (see Human-in-the-loop above). A write can be auto-approved at the Kiro layer while still being gated by the proposal — the two are independent.

| Operation | Kiro `autoApprove` | Proposal gate (agent) |
|-----------|--------------------|-----------------------|
| List/search vault | Yes | None needed |
| Read vault file | Yes | None needed |
| Create vault note/dir | Yes | Propose first (except Bee auto-process, which writes a batch directly) |
| Read email/inbox | Yes | None needed |
| Read calendar | Yes | None needed |
| Send email / book room | No | Always confirm |
| Read Slack (unread, DMs, messages) | Yes | None needed |
| Post/reply Slack | No | Always — draft-only (ENFORCE_DRAFTS) |
| Read tasks | Yes | None needed |
| Update task status | No | After batch approval |

---

## Bee processing on Claude Code (primary)

The Bee processing half runs on **Claude Code** now, not Kiro. The PowerShell sync (Layer 1
scheduled task + Layer 2 watcher) writes sentinels into `.kiro/bee-inbox/`; a Claude Code
consumer drains them. On macOS, `scripts/mac/` replaces the PowerShell scripts with two launchd
jobs (see `scripts/mac/README.md`); the sentinel contract and the consumer are the same.

| Component | Trigger | What it does |
|-----------|---------|-------------|
| `.claude/agents/bee-processor.md` | invoked by the command/runner | The subagent that does the work: for each pending sentinel — idempotency guard → completeness gate (skip partial/sparse) → redact → write the outputs to the vault (meeting note last) → write a done marker in `.kiro/bee-inbox/_done/` → delete the sentinel (on macOS the runner deletes it). Tools scoped to Read/Write/Edit/Glob/Grep/Bash; no send/ticket/email. |
| `.claude/commands/process-bee-inbox.md` | `/process-bee-inbox` (manual) | The reliable manual path. Drains ALL pending sentinels in one pass. |
| `scripts/run-bee-process.ps1` | scheduled task `BeeProcess30Min` | Headless runner: `claude -p '/process-bee-inbox'` on a cadence (every 30 min by default), cheap early-out when no sentinels pend. Replaces the retired Kiro auto-hook. |
| `scripts/mac/run-bee-process.sh` | launchd job `com.personal-assistant-kit.bee-process` (started by the sync when it writes sentinels; every 30 min as a fallback) | macOS headless runner with the same contract, plus an optional preflight command and a watchdog. Claude runs in `dontAsk` mode: it may read inside the repo and the vault, run `ls`, `wc` and `date` there, and write only Markdown directly inside the output folders and `.kiro/bee-inbox/_done/`. The agent deletes nothing there: the runner removes each sentinel whose done marker is newer than it. `scripts/mac/bee_sync.py` is the matching sync job. |

Output folders are read from `.claude/bee-paths.local.json` (gitignored, personalized;
`.claude/bee-paths.example.json` is the tracked template). The vault root is derived from
each sentinel's `raw_path`, so no absolute vault path lives in a tracked file.

### Sentinel-based processing flow

```
bee sync (scheduled/watcher) → raw capture to vault → sentinel to .kiro/bee-inbox/
    → BeeProcess30Min runs run-bee-process.ps1 → claude -p '/process-bee-inbox'
      — OR — you run /process-bee-inbox manually
    → bee-processor subagent, for each pending sentinel:
        idempotency guard (skip if bee_conversation_id already in vault)
        completeness gate (skip if state still CAPTURING / sparse; leave its sentinel)
        redact, generate slug, draft outputs
        write the outputs directly to the vault, meeting note last (Write/Edit reach outside the repo)
        write the done marker .kiro/bee-inbox/_done/<id>.md
        delete the sentinel (on macOS the runner does it, for each sentinel older than its marker)
    → summarize
```

Unlike the old Kiro/MCP path, Claude Code writes directly to the vault, so there is no
`_output/` staging or `apply-bee-outputs` last-mile step. The close-the-loop guarantee is
the same: a sentinel is deleted only after its outputs land and its done marker is written.

### The event seam is single-consumer today (conditional generalization)

The event seam is `.kiro/bee-inbox/*.sentinel.md` because Bee is the only event-driven
consumer right now. **When a second event-driven consumer is added**, generalize the seam to
`.agent-inbox/<consumer>/*.sentinel.md` (one subfolder per consumer) and update the four
sentinel-dir defaults at that time: `scripts/bee-sync-scheduled.ps1` (`$SentinelDir`),
`scripts/bee-stream-watcher.ps1` (`$SentinelDir`), `scripts/run-bee-process.ps1`
(`$sentinelDir`), and `scripts/install-bee-sync-task.ps1`. The two prompt files,
`.claude/agents/bee-processor.md` and `.claude/commands/process-bee-inbox.md`, name the inbox and
its `_done/` marker folder too; update them in the same change. Until then, do NOT move it;
moving it now would break the running `BeeProcess30Min` task for zero benefit. On macOS the
seam is `sentinel_dir` in `~/.config/bee-sync/config.json` (written by
`scripts/mac/bee-launchd.sh install`) plus the same default in `scripts/mac/run-bee-process.sh`;
update both when the seam moves.

At the same time the seam is generalized, extract the copy-pasted sentinel-writing heredoc
(duplicated in `bee-sync-scheduled.ps1` and `bee-stream-watcher.ps1`, both still carrying the
stale instruction line "Process per the `bee-processing` steering rules") into a shared
`bee-lib.ps1` helper and drop that stale line. This is the future step, not a change to make
now. Each of the four sites carries a `# CONDITIONAL` comment pointing back here.

### Retired Kiro hooks

`.kiro/hooks/bee-sentinel-auto-process.kiro.hook` and `bee-process-inbox.kiro.hook` are
**deprecated and disabled** (`enabled: false`). They no longer fire — the maintainer moved
off Kiro. They are kept as a reference for anyone still running this pipeline under Kiro.
The `weekly-status-update` hook is unrelated to Bee and unaffected.

---

## File map

```
personal-assistant-kit/
├── CLAUDE.md                          ← You are here
├── README.md                          ← Public-facing docs
├── system-prompt.md                   ← GTD Assistant system prompt (generic, [BRACKETED])
├── system-prompt-bee-processor.md     ← Bee Processor system prompt (Claude Desktop adopters)
├── claude_desktop_config.example.json ← Basic MCP config example
├── claude_desktop_config.advanced.example.json
├── .claude/                           ← Claude Code runtime (primary Bee consumer)
│   ├── agents/
│   │   └── bee-processor.md           ← Bee Processor subagent (replaces the Kiro hooks)
│   ├── commands/
│   │   └── process-bee-inbox.md       ← "/process-bee-inbox" manual drain
│   ├── bee-paths.example.json         ← tracked path-map template ([BRACKETED])
│   └── bee-paths.local.json           ← gitignored, personalized vault path map
├── .kiro/
│   ├── bee-inbox/                     ← Sentinel processing state (gitignored contents)
│   │   └── .gitkeep
│   ├── hooks/
│   │   ├── bee-sentinel-auto-process.kiro.hook  ← DEPRECATED (disabled; moved to Claude Code)
│   │   ├── bee-process-inbox.kiro.hook          ← DEPRECATED (disabled; moved to Claude Code)
│   │   └── weekly-status-update.kiro.hook
│   ├── settings/
│   │   └── mcp.example.json           ← MCP config template (Obsidian + Outlook + Slack)
│   ├── steering/
│   │   ├── bee-processing.md          ← Bee processing rules (genericized)
│   │   └── gtd-assistant.example.md   ← GTD steering template (generated into gtd-assistant.md)
│   └── specs/
│       └── operational-framework-example/  ← Template for non-code Kiro specs
├── scripts/
│   ├── bee-lib.ps1                    ← Shared completeness gate (Test-BeeCaptureReady)
│   ├── bee-sync-scheduled.ps1         ← Scheduled Bee sync (every 15 min, Layer 1)
│   ├── bee-stream-watcher.ps1         ← Real-time Bee event watcher (Layer 2)
│   ├── bee-sync-silent.vbs            ← Silent launcher for the sync task
│   ├── install-bee-sync-task.ps1      ← Installs the sync scheduled task
│   ├── install-bee-watcher-autostart.ps1
│   ├── run-bee-process.ps1            ← Headless Claude Code consumer runner
│   ├── bee-process-silent.vbs        ← Silent launcher for the process task
│   ├── install-bee-process-task.ps1  ← Installs the BeeProcess30Min task
│   ├── apply-bee-outputs.template.ps1 ← Legacy Kiro/MCP vault write-back (unused by Claude Code)
│   └── mac/                           ← macOS port on launchd: bee_sync.py, run-bee-process.sh, bee-launchd.sh, README.md
├── docs/
│   ├── bee-setup.md
│   ├── enterprise-mcp-patterns.md
│   ├── obsidian-setup.md
│   ├── obsidian-sync-options.md
│   ├── gmail-calendar-github-setup.md
│   ├── google-drive-setup.md
│   ├── advanced-connectors.md
│   └── customizing-your-system-prompt.md
├── SETUP-MAC.md
├── SETUP-WINDOWS.md
└── TROUBLESHOOTING.md
```

---

## Development guidelines

### When modifying system prompts

- Keep `[BRACKETED]` placeholders for user-specific values
- Don't add employer-specific, person-specific, or tool-specific content to tracked files
- Land behavioral changes in BOTH the tracked `system-prompt.md` and the steering template (`.kiro/steering/gtd-assistant.example.md`) — see "Which contract governs"
- Optionally check changes against the offline eval suite (`evals/`) — note it's not wired into CI yet
- The system prompt is the behavioral contract; changes to it change agent behavior

### When adding new hooks

- Choose the trigger mechanism per the triggers decision-rule in "Information-sharing rules" above: poll-by-frontmatter first, then a status-flag flip, and a sentinel drop only when latency actually hurts.
- Define the trigger clearly (fileCreated, userTriggered, scheduled)
- Document what the hook reads, what it writes, and what it deletes
- Respect the vault contract and the one-writer-per-folder invariant; don't write to folders outside the hook's designated scope, and don't write to a folder another agent already owns
- Add a corresponding entry to this CLAUDE.md's hooks table

### When adding new MCP servers

- Document the auth pattern in `docs/enterprise-mcp-patterns.md`
- Define which operations are auto-approved vs. require confirmation
- Add to the trust model table above
- Add the server to the file map

### Commit conventions

- Commit messages: imperative mood, lead with what changes (not "Updated X")
- Don't commit `.kiro/settings/mcp.json`, vault paths, credentials, or local-only content
- Check `.gitignore` before staging — sensitive content should never reach git history

---

## Runtime vs. offline tooling

Be precise about what actually runs *inside the agent* versus what is *offline developer tooling* you run yourself from a terminal. The agent is prompt-driven over MCP — it cannot `import` TypeScript modules mid-session, so the TS subsystems below do **not** execute during a live agent turn and do **not** enforce or observe anything the agent does in real time.

| Component | What it is | Runs inside the agent? |
|-----------|-----------|------------------------|
| `system-prompt.md`, `.kiro/steering/*.md` | The behavioral contract the model reads | **Yes** — this is the live runtime |
| `.claude/agents/bee-processor.md`, `.claude/commands/process-bee-inbox.md` | Claude Code subagent + command — the live Bee consumer | **Yes** — when Claude Code is the client |
| `.kiro/hooks/*.kiro.hook` | Kiro-triggered agent prompts (the Bee ones are deprecated/disabled) | Only if Kiro is the client; the Bee hooks no longer fire |
| Bee sync + process `scripts/*.ps1`, `scripts/mac/*` | Windows Task Scheduler / login automation; launchd on macOS | **Yes** — but as OS processes, not inside the agent |
| `validators/` | TS CLI: schema / redaction / routing / idempotency checks | **No** — offline CLI you run against the vault; nothing calls it at agent runtime |
| `observability/` | TS logging / tracing / metering / drift | **No** — only the offline eval runner imports it; it does not observe live agent turns |
| `evals/` | TS harness calling the Claude API with fixtures | **No** — offline test suite; runs in CI against the shipped prompts, not during an agent turn |

The TS subsystems are useful **offline** (run them in CI or by hand to check the vault and prompts). They are not runtime middleware, and nothing in the agent loop invokes them. Don't describe them as if they gate or instrument live behavior.

---

## What's built

| Area | Status | What it is | Where it runs |
|------|--------|-----------|---------------|
| CLAUDE.md + agent architecture | Built | This file — the system contract | Runtime (read by the agent) |
| Daily Triage + GTD workflows | Built | `system-prompt.md` + `.kiro/steering/gtd-assistant.md` | Runtime |
| Bee pipeline | Built | sync scripts (Layer 1 + 2) + processing rules + Claude Code consumer (`bee-processor` subagent, `/process-bee-inbox`, `run-bee-process.ps1`); macOS port in `scripts/mac/`; Kiro hooks deprecated | OS automation + Claude Code runtime |
| Evals | Built (offline, CI-gated) | `evals/` — 5 suites (inbox, Bee, review, status, confirm-before-write) against the shipped prompts/hooks | Offline CLI + GitHub Actions (`.github/workflows/evals.yml`) |
| Observability | Built (offline) | `observability/` — logs, traces, metering, drift | Offline; imported only by the eval runner |
| Validators | Built (offline) | `validators/` — schema, redaction, routing, idempotency, continuation | Offline CLI; **not** invoked at agent runtime |
| Bundled infrastructure | Built | `.devcontainer/`, `sandbox/`, `scripts/setup.mjs` | Dev-time |
| Subagent routing | Not built | Deferred — revisit if evals show cost/latency bottlenecks | — |

> Honesty note: the offline subsystems were previously labeled "Done / Auto-integrated," implying they enforce invariants and observe cost/quality on the live agent. They do not — see "Runtime vs. offline tooling" above. The evals now run in CI against the shipped prompts/hooks (`.github/workflows/evals.yml`), including a real tool-use gate; validators as a pre-commit/scheduled vault check remain a manual step.
