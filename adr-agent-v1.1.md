# adr-agent — v1.1 Design Document

## v1.1 Changes

This section summarises what changed from v1. The rest of the document reflects the updated design throughout; this section exists to make the delta legible without a line-by-line diff.

### 1. New `plan` command (replaces `considered`)

The `considered` command answered a narrow question: "has this specific technology appeared as an alternative before?" The `plan` command answers the broader question an agent actually asks at the start of a task: "given what I am about to do, what does the store already know that is relevant?"

`adr-agent plan "<prompt>"` accepts a free-text description of the agent's intended work and returns a structured context brief covering accepted decisions that apply, observed entries that may be affected, alternatives that were evaluated and rejected (and why), and constraints that are currently active. This replaces the need to issue multiple `show` and `considered` calls before beginning a non-trivial task.

To support efficient `plan` execution, the data layer gains an inverted index (`.adr-agent/index.json`) that maps terms extracted from decision content — titles, tags, dependency names, alternative names, constraint tags, and prose keywords — to the decision IDs that contain them. The index is updated on every write operation and used by `plan` for sub-second lookup across the full store.

### 2. `considered` command removed

The `considered` command is subsumed by `plan`. Its output — decisions where a topic appears as an alternative, grouped by outcome — is now a named section within `plan` output. The `considered` command no longer exists as a standalone CLI entry point.

The session-start brief hint that previously listed `adr-agent considered <topic>` now lists `adr-agent plan "<prompt>"` instead.

The logging schema drops `considered` from the voluntary-action table and adds `plan` in its place.

### 3. First-Run Audit prompt for existing codebases

When `adr-agent init` runs against a repository that already has runtime dependencies, the tool now ends its output with an explicit suggestion for the agent to run a structured backfill audit. The audit prompt instructs the agent to examine the seeded observed entries, research why each central dependency was likely chosen, and call `adr-agent promote <id>` with real rationale and alternatives where evidence exists. This converts an otherwise-opaque set of observed entries into accepted decisions from the first session, rather than leaving them as a long-lived debt for the promotion pull mechanisms to work down gradually.

---

## Purpose

adr-agent is a per-repository system that provides AI agents with durable
architectural memory across sessions. It closes a specific gap: agents make
architectural choices every session but have no reliable way to know what
has already been decided, what has already been considered and rejected, or
why. Without such a system, agents waste tokens rediscovering settled
questions, relitigate choices that were made deliberately, and drift from
the architectural direction that humans and prior agents established.

This document describes the complete v1.1 feature set. It is scoped to what
can be built, shipped, and used in real projects to test the core
hypothesis: that agents benefit meaningfully from structured architectural
memory when the system makes recording reliable and retrieval cheap.

This document is written from the perspective of the primary consumer: an
AI agent operating in the codebase. Human developers are secondary
beneficiaries — they see less rework, fewer architectural regressions in
code review, and a growing record of why the system is shaped the way it is.

## Agent Requirements

### R1. Session orientation without cost

An agent begins each session with no memory of prior sessions. To act
coherently, it must know what architectural decisions are currently in
force. This knowledge must arrive in the agent's context cheaply — compact
enough not to dominate the context window, but sufficient for the agent to
recognize when a task intersects with a prior decision and should be
investigated further.

### R2. On-demand access to full rationale

When the agent recognizes that a decision applies to its current task, it
must be able to retrieve the full reasoning behind that decision: the
context that prompted it, the alternatives considered, and the constraints
that shaped the outcome.

### R3. Visibility into negative space

Agents must know not only what was chosen but what was considered and not
chosen, and why. The system must distinguish between **rejected**
alternatives (ruled out for substantive reasons that may still apply) and
**not-chosen** alternatives (viable options that lost on balance but remain
available).

### R4. Constraint tracking

Many decisions rest on constraints that can expire — team size, traffic
volume, regulatory posture, existing infrastructure. The system must make
the link between decisions and constraints explicit and queryable.

### R5. Structured write path

When an agent makes a non-trivial architectural choice, it must have a
low-friction way to record that choice with enough structure that future
agents can consume it.

### R6. Trigger awareness

Agents cannot be relied upon to remember to record decisions. The system
must intercept the agent at points where a decision is implicit in the
action being taken.

### R7. Non-blocking operation

The system must inform but not prevent. The agent, given full context, is
trusted to make the final call. Hard blocks create workflow friction that
either stops the agent or trains it to route around the tool.

### R8. Preservation of history and integrity

Superseded decisions must remain visible and queryable, with links to their
replacements. Rejected proposals must be preserved distinctly from
superseded decisions. Critically, the store must remain a faithful
representation of the codebase's architectural surface — the system must
not allow gaps to accumulate silently between what the codebase contains
and what the store records.

## System Overview

adr-agent is a Python package installed as a development dependency of the
target repository. It ships a command-line entry point (`adr-agent`) and,
via Claude Code's hook system, integrates into the agent's session
lifecycle so that orientation, triggered prompts, and store reconciliation
happen automatically.

All persistent state lives under `.adr-agent/` at the repository root as
plain markdown files with YAML frontmatter, checked into version control.
Every clone of the repository has identical decision history. Every session
on any clone sees the same state.

The agent interacts with adr-agent through:

- Automatic context injection at session start (via SessionStart hook)
- Automatic prompts at architectural inflection points (via PreToolUse and
  PostToolUse hooks)
- Explicit queries the agent may issue at any time (CLI commands)
- An explicit write path for recording decisions (CLI commands)

## Data Model

### Decision files

Each decision is a single markdown file under `.adr-agent/decisions/`,
named `ADR-NNNN-slug.md`, with structured frontmatter and a prose body.

```yaml
---
id: ADR-0047
title: Use Redis for session storage
status: accepted # accepted | observed | superseded | rejected
created: 2026-03-02
confidence: medium # low | medium | high
scope:
tags: [session, auth]
paths: ["src/auth/sessions/**"]
supersedes: [ADR-0019]
superseded_by: []
constraints_depended_on: [mobile-latency-sla]
alternatives:
- name: Postgres-backed sessions
  outcome: not-chosen
  reason: "Meets correctness requirements but adds query load to primary DB;
  Redis already present"
  reversible: cheap
- name: In-memory sessions with sticky routing
  outcome: rejected
  reason: "Incompatible with blue/green deploys required by ADR-0032"
  reversible: no
  constraint: blue-green-deploys
---

## Context
The mobile team requires sub-100ms session lookup at p99.
ADR-0019 previously specified Postgres for all persistence...

## Decision
Sessions are stored in Redis...

## Consequences
...
```

### Status semantics

- **accepted** — currently in force, with recorded rationale. Included in
  the brief.
- **observed** — currently in force, but rationale was not captured.
  Created either at initialization (from existing dependencies) or by
  automatic reconciliation when a dependency change happens without a propose
  call. Included in the brief, segregated from accepted entries. Eligible for
  promotion when context becomes available.
- **superseded** — was in force, replaced by another decision. Excluded
  from the brief; retrievable by id or history query.
- **rejected** — proposed but never accepted. Preserved as a record of what
  was considered and declined.

Observed entries additionally carry an `observed_via` field recording how
they were created: `seed` (created at `init`), `reconciliation`
(auto-created when a dependency appeared without a propose call), or
`manual` (rare; reserved for direct user creation). This distinction
matters for the integrity report.

### Alternative semantics

Each alternative within a decision carries:

- **outcome** — `chosen` (the decision itself), `not-chosen` (viable but
  unpicked), `rejected` (ruled out for a substantive reason).
- **reason** — plain text rationale.
- **reversible** — `cheap` | `costly` | `no` — the cost of revisiting later.
- **constraint** *(optional)* — a tag identifying what made the alternative
  unavailable. If the constraint expires, the alternative becomes revisitable.

### Constraints

Constraints are tags rather than standalone records. A decision or
alternative references a constraint by name; the agent can query
`adr-agent check-constraint <tag>` to find every reference. This enables
the workflow: a constraint changes → agent queries for dependents → agent
evaluates whether any decisions should be reconsidered.

### The decision index

To support efficient `plan` execution across large stores, adr-agent
maintains an inverted index at `.adr-agent/index.json`. The index maps
individual terms to the set of decision IDs that contain them, enabling
sub-second lookup without reading every decision file.

**Indexed terms** are extracted from each decision at write time and include:

- The decision title (tokenised into individual words)
- All `tags` values
- All dependency names present in the title or alternatives
- All `constraints_depended_on` values
- All alternative `name` values
- All alternative `constraint` values
- Keywords extracted from the prose `## Context` and `## Decision` sections
  (stop-words removed, stemming applied)

The index is a plain JSON object of the form:

```json
{
  "redis":      ["ADR-0003", "ADR-0019", "ADR-0047"],
  "session":    ["ADR-0047"],
  "auth":       ["ADR-0047"],
  "blue-green": ["ADR-0032", "ADR-0047"],
  ...
}
```

**Index lifecycle:**

- Created (or rebuilt in full) by `adr-agent init` and `adr-agent rebuild-index`.
- Updated incrementally on every `propose`, `promote`, or direct decision
  file edit detected at reconciliation time.
- Read by `plan` to resolve query terms to candidate decision IDs before
  fetching and ranking the matching files.
- Committed to version control alongside decision files. Every clone has an
  identical, current index.

The index file is the only derived artifact that adr-agent commits. It is
small (one short line per term across the full store), deterministic (the
same decision content always produces the same index entries), and
reconstructible at any time via `adr-agent rebuild-index`.

## Lifecycle of an Observed Entry

Observed entries are central enough to the design to warrant their own
narrative. They handle the gap between what the codebase actually depends
on and what the store has rationale for.

An observed entry comes into being one of three ways:

1. **At `init`**, every direct runtime dependency in `pyproject.toml`
   becomes an observed entry. This gives a brand-new adr-agent installation
   immediate substance instead of starting empty.
2. **At reconciliation**, if a dependency exists in `pyproject.toml` but
   has no covering decision, the tool creates an observed entry automatically.
   Reconciliation runs at session start and after every `pyproject.toml` edit,
   so the store is never out of sync with the codebase by more than one
   operation.
3. **Manually**, if a user or agent explicitly chooses to mark something as
   observed without rationale.

Once created, an observed entry waits to be promoted. The system actively
pulls on it through two mechanisms:

- **Edit-time prompts** — when the agent edits a Python file that imports
  an observed dependency, the PreToolUse hook surfaces the relevant observed
  entry and invites the agent to promote it if context is available.
- **Query-time prompts** — when the agent runs `adr-agent show <id>` for an
  observed entry, the response includes an offer to promote.

Both prompts are explicit that silence is acceptable: if the agent has no
context, no promotion is required. The goal is to capture rationale when it
exists, not to coerce confabulation when it doesn't. An entry that stays
observed is honest; an entry that becomes accepted with fabricated
rationale is harmful.

Promotion converts an observed entry to accepted by walking the agent
through the same structured fields as `propose`. The original
`observed_via` field is preserved so the report can distinguish promoted
entries from natively-accepted ones.

## Agent Interface

### Session start (automatic)

On `SessionStart`, adr-agent does two things in order:

1. **Reconciles the store** with `pyproject.toml`, creating observed
   entries for any uncovered dependencies.
2. **Injects the brief** into the agent's context, with accepted and
   observed entries clearly separated:

```
# Architecture decisions (4 accepted, 14 observed)

ACCEPTED
ADR-0007 Pytest, not unittest
ADR-0024 HTTP handlers return Result[T, ApiError], never raise
ADR-0032 Blue/green deploys; no sticky routing
ADR-0047 Redis for session storage

OBSERVED (no rationale captured)
ADR-0001 Uses Postgres
ADR-0002 Uses FastAPI
ADR-0003 Uses SQLAlchemy
ADR-0015 Uses httpx [new: added by reconciliation]
...

Run `adr-agent show <id>` for full rationale and alternatives.
Run `adr-agent plan "<prompt>"` to get relevant context before starting a task.
Run `adr-agent propose` to record a new decision or supersession.
Run `adr-agent promote <id>` to capture rationale for an observed entry.
```

This is the full agent onboarding. No other setup is required of the agent.

### Query commands

**`adr-agent show <id>`** — returns the full decision, including
frontmatter and prose body. For observed entries, the response includes a
soft prompt offering to promote.

**`adr-agent plan "<prompt>"`** — accepts a free-text description of the
agent's intended work and returns a structured context brief assembled from
the store. This is the primary pre-task query command and replaces the
narrower `considered` command from v1.

The command tokenises the prompt, resolves terms against the inverted index
to identify candidate decisions, fetches and ranks the matching files, and
returns a four-section response:

```
$ adr-agent plan "add a background job queue for sending emails"

RELEVANT DECISIONS
ADR-0047 (accepted) Redis for session storage
  Redis is already present in the stack.
ADR-0052 (accepted) Postgres LISTEN/NOTIFY for job queue
  Chosen over Redis queue when volume was low; see CONSIDERED below.
ADR-0032 (accepted) Blue/green deploys; no sticky routing
  Any queue worker must be stateless to satisfy this constraint.

OBSERVED ENTRIES THAT MAY BE AFFECTED
ADR-0001 Uses Postgres (no rationale captured)
ADR-0003 Uses SQLAlchemy (no rationale captured)

WHAT HAS BEEN CONSIDERED
Redis as job queue
  NOT-CHOSEN in ADR-0052 (2026-04-11): "Queue volume is low; Postgres
  LISTEN/NOTIFY sufficient" — reversible: cheap
  If volume has grown, this alternative is worth revisiting.

Celery
  REJECTED in ADR-0052 (2026-04-11): "Adds a broker dependency; overkill
  at current scale" — reversible: costly

ACTIVE CONSTRAINTS RELEVANT TO THIS TASK
blue-green-deploys (referenced by ADR-0032, ADR-0047)
  Any worker process must be stateless and not use sticky routing.

Run `adr-agent show <id>` for full rationale on any entry above.
Run `adr-agent propose` when you are ready to record your decision.
```

The "WHAT HAS BEEN CONSIDERED" section directly subsumes the output that
`considered` previously provided, now surfaced automatically by relevance
rather than requiring the agent to know the right topic term to query.

**`adr-agent history <path|tag>`** — returns the chain of decisions that
have ever governed a path or tag, in chronological order, including
superseded ones.

**`adr-agent check-constraint <tag>`** — returns every decision and
alternative that depends on a named constraint.

### Write commands

**`adr-agent propose`** — initiates recording of a new decision or
supersession. The command prompts the agent for the structured fields
required by the schema:

```
Decision (one sentence):
Rationale (what context led to this choice):
[Prompt note: if prior decisions informed this choice, reference
them by ID in your rationale, e.g. "ADR-0019 rejected Redis for
low-volume reasons; volume has since grown."]
Alternatives considered:
  Name:
  Outcome: [chosen | not-chosen | rejected]
  Reason:
  Reversible: [cheap | costly | no]
  Constraint (optional):
  (repeat or enter 'done')
Constraints this decision depends on:
Supersedes (ADR ids, if any):
Confidence: [low | medium | high]
Scope (tags and/or paths):
```

The rationale prompt softly encourages reference to prior decisions in
prose. Decisions that cite prior decisions are more useful for future
readers, but no specific structure is required.

When `propose` is invoked in the context of a triggered prompt, the tool
pre-fills every field it can derive: the dependency being changed, the
existing decisions surfaced in the trigger, the file path, the implied
scope. The agent's task becomes "review and complete" rather than
"construct from scratch." Lower friction at the moment of recording
produces measurably higher compliance.

The tool writes the resulting decision file with `status: accepted` — the
system trusts the agent; there is no separate approval step. The index is
updated immediately after the file is written.

**`adr-agent promote <id>`** — converts an observed entry to accepted by
walking the agent through the same structured fields as `propose`. The
agent should call this when it has fresh context that explains why the
observed choice was originally made. The index is updated immediately after
promotion completes.

**`adr-agent rebuild-index`** — reconstructs `index.json` from scratch by
re-reading every decision file. Useful after manual file edits or a
corrupted index. Idempotent and safe to run at any time.

## Triggered Prompts

The system intercepts the agent at four lifecycle moments. Together they
maintain store integrity (the store remains a faithful picture of the
codebase) and maximize rationale capture (decisions are recorded when
context is freshest).

### Dependency edits — write path (`PreToolUse` on `pyproject.toml`)

When the agent proposes editing `pyproject.toml` in a way that changes
runtime dependencies (add, remove, or major version bump), adr-agent
injects:

```
You are modifying runtime dependencies:
+ redis>=5.0

Relevant existing decisions:
ADR-0019: Postgres for primary store (superseded by ADR-0047)
ADR-0047: Redis for session storage

If this change is covered by an existing decision, proceed.
If it represents a new or superseding decision, run `adr-agent propose`
first.
The propose flow will pre-fill the dependency name and the relevant
decisions shown above; you provide the rationale and alternatives.
```

The edit is not blocked. The agent decides whether to proceed, pause and
propose, or reconsider.

Triggers in v1.1 are limited to runtime dependency changes in
`pyproject.toml`. This is deliberately narrow: it is the cheapest reliable
signal of an architectural choice and produces very few false positives.

### Dependency edits — backstop (`PostToolUse` on `pyproject.toml`)

After a dependency edit completes, two things happen:

1. **Reconciliation runs.** Any newly-added dependency without a covering
   decision becomes an observed entry, ensuring the store reflects current
   reality before the next agent action. The index is updated to include
   the new entry.
2. **If the agent edited dependencies without a preceding propose call**, a
   reminder fires: "you modified dependencies without recording rationale;
   the affected entries are now observed. Run `adr-agent promote` if you
   can capture context now."

This pairs with the PreToolUse trigger to provide a second capture point
for rationale, and guarantees the store stays in sync regardless of whether
the agent recorded a decision.

### Code edits — observed-entry pull (`PreToolUse` on file edits)

When the agent edits a Python file (`Edit`, `Write`, or `MultiEdit`),
adr-agent extracts top-level imports and checks them against observed
entries. If the file imports any observed dependency, the hook injects:

```
You are editing code that imports `redis`, an observed dependency.

ADR-0003: Uses Redis (observed; no rationale captured)
Added: 2025-09-14 (during seeding)

If you have context for why Redis was originally adopted (from the
task description, code comments, or your conversation), consider
running `adr-agent promote ADR-0003` to capture it.

If you don't have context, no action is needed.
```

This fires at most once per observed entry per session, tracked via
session-scoped state. The "no action needed" line is deliberate: silence is
acceptable when context is genuinely absent. Without that explicit
permission, agents fabricate rationale to satisfy the prompt, which is
exactly the failure mode the tool exists to prevent.

### Session end — final capture opportunity (`SessionEnd`)

When the session terminates with any unresolved triggers (dependency
changes that fired a PreToolUse but never produced a propose call), the
SessionEnd hook injects a final prompt listing them and offering one last
chance to capture rationale before the context is lost.

## Multi-shot capture and integrity

Because adr-agent is a record-keeping tool, missed writes degrade the
integrity of the entire store — once agents and humans learn the store
doesn't reliably reflect current architecture, trust collapses. The system
maintains integrity through two complementary mechanisms:

**For new architectural decisions**, multi-shot capture across three points
(PreToolUse, PostToolUse, SessionEnd) gives the agent multiple
opportunities to record rationale at the moment of decision. The compound
probability of at least one capture succeeding is meaningfully higher than
any single prompt.

**For the underlying store-reality invariant**, automatic reconciliation
guarantees that every dependency in `pyproject.toml` is covered by some
decision (accepted or observed) at all times. Reconciliation runs at
session start and after every dependency edit, so the store is never out of
sync by more than one operation. If the agent skips propose, the dependency
still becomes an observed entry — rationale is lost, but the store remains
accurate.

These two mechanisms separate the integrity question (is the store
complete?) from the quality question (does each entry have rationale?).
Reconciliation guarantees integrity; multi-shot capture maximizes quality.
Together they produce a store that is reliably complete, with rationale
density that improves over time as agents promote observed entries when
context exists.

## Write Discipline

The agent should call `adr-agent propose` when:

- A triggered prompt indicates a decision is implicit in the current action.
- The agent is making a non-trivial architectural choice that another agent
  in the future might revisit — data store choice, concurrency model,
  external service integration, API style, error-handling convention.
- The agent is deliberately superseding a decision found in the brief or
  via query.

The agent should *not* call `adr-agent propose` for:

- Local implementation choices with no cross-module implications.
- Style or formatting choices (these belong in linter config, not decision
  records).
- Reversible experiments the agent expects to discard within the same
  session.

The agent should call `adr-agent promote <id>` when:

- A code-edit prompt surfaces an observed entry the agent has context for.
- The agent is working on code governed by an observed entry and the task
  description, comments, or recent conversation explain the original choice.
- The agent runs `show` on an observed entry and recognizes context that
  should be captured.

The agent should call `adr-agent plan "<prompt>"` when:

- Beginning any task that is likely to touch architectural decisions —
  adding a dependency, changing a data model, introducing a new service
  boundary, or modifying code in a path covered by existing decisions.
- The brief surfaced relevant decisions or observed entries and the agent
  wants to understand the full context before acting.

The brief's own content is the calibration signal: the granularity visible
in existing decisions is the granularity at which new decisions should be
recorded.

## Logging and Reporting

The system logs agent interactions so a human observer can assess whether
the tool is providing value. Logging is scoped to **voluntary** actions —
those the agent chose to take — because only voluntary actions carry signal
about usefulness. Required actions that fire automatically from hook
configuration occur at the same rate regardless of value delivered and are
excluded from the voluntary-action report.

### What is logged

Each voluntary invocation is recorded with a timestamp, session id, the
command name, and the target of the command:

- `adr-agent show <id>`
- `adr-agent plan "<prompt>"` (target: the prompt string, truncated to 200 chars)
- `adr-agent history <path|tag>`
- `adr-agent check-constraint <tag>`
- `adr-agent propose`
- `adr-agent promote <id>`

Trigger fires (PreToolUse, PostToolUse, SessionEnd, edit-time
observed-entry prompts) and reconciliation events are also logged, but as
separate event types not counted in the voluntary-action report. They are
recorded so that integrity metrics can be computed and so that causal
sequences can be reconstructed by future analyses.

`adr-agent rebuild-index` is logged as a maintenance event, separate from
both voluntary-action and trigger event tables.

Decision content is not logged — only identifiers and invocation metadata.
This keeps logs small, avoids leaking decision text into any aggregated
report, and makes logs safe to share across a team.

### Log storage

Session logs are written to `.adr-agent/sessions/<session-id>.jsonl` as
line-delimited JSON, one entry per invocation. The `sessions/` directory is
gitignored by default so that raw logs stay local. This avoids merge
conflicts on concurrent sessions and keeps the repository from bloating
with per-session artifacts.

### Log schema

Each event entry includes `timestamp`, `session_id`, `event_type`,
`command`, `targets` (a list of canonical identifiers the command operated
on), and a stable `event_id`. This structure supports the report command's
aggregations and provides enough information for ad-hoc analysis if a user
wants to query the logs directly.

### The report command

A human invokes `adr-agent report` to roll up session logs and store state
into a readable summary:

```
$ adr-agent report --since "2 weeks ago"

Retrieval (72 voluntary queries)
  show          41    most-viewed: ADR-0047, ADR-0024, ADR-0032
  plan          18    most-queried topics: job queue, session storage, async
  history        9
  check-constraint 4

Writes (13 records created)
  propose        6
  promote        7    observed → accepted

Integrity
  Reconciliation events: 3
    via session start:  1
    via post-edit hook: 2
  Promotion opportunities: 12 (edit-time prompts fired)
  Promotions resulting:   5  (42% of opportunities)

Observed entries: 14 total
  via seed:          11 (from initial adoption)
  via reconciliation:  3 (created when propose was skipped)
  via manual:          0
```

The **Integrity** section is the most important diagnostic. Reconciliation
count measures how often the system had to backstop a missed propose call —
each reconciliation event is a moment where rationale was available but not
captured. The promotion ratio measures how effectively the pull mechanisms
are converting observed entries into accepted ones when context exists. The
breakdown of observed entries by source tells the longer story: a healthy
adoption shows seed counts shrinking (as promotion happens) while
reconciliation counts stay low (as write discipline holds).

A high reconciliation count or a low promotion ratio signals that the store
is drifting away from being a useful record, even if voluntary retrieval
counts look fine.

## When Value Accrues

The primary value of adr-agent is cross-session. But single-session value
exists too, and it matters for adoption — users need some benefit from the
very first session.

### Benefits available in a single session

**Reasoning externalization.** When the agent calls `propose`, the
structured prompt forces articulation of alternatives, reversibility, and
constraints. This improves the reasoning itself regardless of whether any
future session reads the record.

**Preventing within-session drift.** Agents contradict themselves within
long sessions. A decision written in the first hour is visible to the agent
in the third hour via the brief and via `show`.

**Immediate architectural surface from initialization.** Initialization
seeds observed entries from existing dependencies, so the brief has
substantive content from the very first session. Even with zero accepted
decisions, the agent starts with an accurate mental model of what's
load-bearing.

**Trigger-driven reflection.** The dependency change trigger creates a
pause before an edit lands, surfacing any relevant existing decisions.

**Pre-task context via `plan`.** Before beginning any significant task,
the agent can issue a single `plan` call to retrieve all relevant accepted
decisions, observed entries that may be affected, previously-evaluated
alternatives, and active constraints. This replaces the need for multiple
`show` and ad-hoc queries and is available from the first session.

### Benefits that require multiple sessions

Preventing re-litigation of settled questions requires prior records to
litigate against. Surfacing prior rejections requires prior rejections to
exist. Architectural evolution via supersession requires decisions old
enough to be superseded. Protection against contributor turnover requires
history the new contributor didn't produce. These are the tool's headline
value propositions, and they all require accumulation.

### The J-curve

Honest expectation: modest single-session benefits for the first several
sessions, real cross-session benefits emerging as the decision count grows,
and full value only after the store contains enough material that most
architectural work touches something previously recorded. This is a
timescale of weeks of real use, not days.

The seeding-at-init flow partially backfills this cold-start problem by
giving the brief substantive content from day one. The First-Run Audit
(described below) accelerates this further: by converting seeded observed
entries into accepted ones immediately, a project with the audit complete
starts closer to the middle of the J-curve rather than the bottom.

Without either mechanism, the first several sessions would feel like pure
overhead, and users would likely uninstall before the curve turned.

## Privacy and Transparency

adr-agent records information that becomes part of a repository and its
local environment. The tool is explicit about this at first use so users
understand what they are opting into.

### What gets committed to the repository

Decision files and the decision index are committed to git. Rationale,
alternatives, constraints, and any prose the agent writes into a decision
become part of the repository's permanent history and are visible to
everyone with repo access:

- Rationale frequently contains context beyond pure technical reasoning:
  business considerations, security posture, team dynamics, vendor
  relationships. Treat decision content with the same sensitivity as source
  code and comments.
- Once committed, decisions are in git history. Editing or deleting a
  decision file in a later commit does not remove the original content from
  history.
- The aggregate pattern of recorded decisions may reveal information about
  a project even when individual decisions are innocuous — traffic scale,
  internal debates, strategic shifts.

### What gets stored locally

Session logs under `.adr-agent/sessions/` are stored on the user's machine
and gitignored by default. They contain command invocations, timestamps,
session ids, and target identifiers. They do not contain decision content,
agent reasoning, or conversation context.

Logs persist on disk until the user cleans them up; v1.1 does not impose
automatic retention limits.

### What leaves the machine

Nothing. adr-agent does not transmit data externally. There is no
telemetry, no crash reporting, no central collection, no corpus
contribution.

Future versions will not change this default. If the tool ever adds
features that send data externally, those features will be user-initiated
and off by default.

### The first-run notice

On first invocation of `adr-agent init` on a given machine, the tool
displays a one-time notice summarizing the above and asks for confirmation
before proceeding. Subsequent `init` runs in other repositories on the same
machine do not repeat the full notice. A `--yes` flag skips the prompt for
automated contexts; the notice content is always available via `adr-agent
privacy` and in the tool's documentation.

```
adr-agent records architectural decisions for use by AI agents.
Before initializing, please note:

1. Decision files and the decision index are committed to git and
   become part of your repository's permanent history. Rationale
   and alternatives will be visible to everyone with repo access.
   Treat them with the same sensitivity as source code.

2. Session logs are stored locally under .adr-agent/sessions/ and
   are gitignored by default. They contain command metadata, not
   decision content.

3. adr-agent does not transmit any data externally. No telemetry,
   no central collection.

4. The aggregate pattern of decisions can reveal information even
   when individual decisions are innocuous.

Proceed with init? [y/N]
```

## Human Developer Setup

adr-agent is added to a Python project as a development dependency:

```bash
uv add --dev adr-agent
# or: pip install adr-agent
```

Initialization is a single command run at the repository root:

```bash
adr-agent init
```

This creates:

- `.adr-agent/` — initial configuration and decisions folder. If
  `pyproject.toml` already lists runtime dependencies, observed entries are
  created for each (`observed_via: seed`).
- `.adr-agent/index.json` — the inverted index, populated from the seeded
  observed entries. Empty on a fresh repository; substantive from day one
  on any existing codebase.
- `.claude/settings.json` — populated with the required hooks:
  `SessionStart`, `PreToolUse` and `PostToolUse` on `pyproject.toml` edits,
  `PreToolUse` on file edits for observed-entry prompts, and `SessionEnd`
  for final-capture reminders.
- `.gitignore` entries for `.adr-agent/sessions/` so raw session logs stay
  local while decisions, the index, and configuration travel with the repo.

There is no separate `--seed` flag. Seeding is the default behavior because
empty stores have no value and existing dependencies are always
architecturally relevant.

Contributors cloning the repository run `uv sync` to install adr-agent into
the project environment; the hook configuration is already present in the
checked-in settings file. No further human intervention is required for
day-to-day operation.

### The First-Run Audit

When `adr-agent init` runs against a repository that already has runtime
dependencies, initialization ends with the following suggestion:

```
adr-agent has seeded N observed entries from your existing dependencies.
These entries reflect what the codebase uses but contain no rationale.

To backfill rationale for existing dependencies, ask your AI agent to
run the First-Run Audit:
```

The suggested prompt to give the agent:

> "I have just initialized adr-agent in this repository. Review the list
> of **OBSERVED** dependencies provided in the architecture brief.
>
> For each central dependency (e.g., the web framework, database client,
> or CLI library):
>
> 1. **Research** why it was likely chosen over common alternatives by
>    examining the code, imports, and documentation.
> 2. **Analyze** the pros and cons of this choice in the context of this
>    specific project.
> 3. **Execute** `adr-agent promote <id>` to convert these into **ACCEPTED**
>    entries. Include the rationale and at least one alternative considered
>    in the promotion flow.
>
> If you cannot find evidence for why a dependency was chosen, leave it as
> 'Observed' to maintain store integrity."

This prompt is shown once at `init` time and is always accessible via
`adr-agent first-run-audit`. It is not mandatory — a project may skip the
audit and rely on the gradual promotion pull mechanisms instead. But
running it immediately converts the seeded baseline into real accepted
decisions, accelerating the point at which the store delivers cross-session
value.

The audit explicitly instructs the agent not to fabricate rationale for
entries where evidence is absent. Leaving an entry observed is correct
behavior; promoting it with invented context is the primary failure mode
the tool is designed to prevent.

### Hook management is owned by the tool

The user never hand-edits `.claude/settings.json` for adr-agent. The tool
owns the full lifecycle of its own hook configuration, because it is the
only party that knows which events it needs to intercept and what commands
those hooks should invoke. Making this manual would create version drift
and silent installation failures.

Specifically, `init` handles:

- **Merging with existing hooks.** Projects that already have a
  `.claude/settings.json` with other hooks have adr-agent's entries added
  alongside them rather than overwriting the file.
- **Idempotence.** Re-running `init` does not duplicate hooks. The tool
  detects its own entries by command name and leaves them alone.
- **Drift detection.** When the tool version changes in a way that requires
  different hooks, `adr-agent doctor` reports the mismatch and offers to
  repair the configuration.
- **Clean removal.** `adr-agent uninstall` removes the tool's hook entries
  from `.claude/settings.json` without touching any other entries.

## Non-Goals

- **Scope-based enforcement.** The tool does not statically determine which
  files a decision governs and block edits that conflict. Scope tags and
  paths exist to aid retrieval, not to gate action.
- **Materialized alternative states.** The system does not store snapshots
  of the dependency graph or other "what-if" variants. Revisitability is
  captured through the `reversible` and `constraint` fields on alternatives.
- **Behavioral-change metrics.** The report surfaces activity counts and
  integrity metrics, not causal sequences between queries and outcomes.
- **Hard blocking.** No hook ever blocks an action. Every prompt is
  informational and skippable.
- **Human-facing polish.** The storage format is readable by humans for
  audit purposes but is not optimized for human browsing. There is no
  generated documentation site, no navigation UI, no search beyond the CLI.
- **Multi-language support.** v1.1 is Python-only. Dependency trigger
  detection and import-based observed-entry prompts assume `pyproject.toml`
  and Python imports.
- **Multi-client support.** v1.1 integrates with Claude Code only. The hook
  system provides automatic interception that MCP cannot replicate;
  integrity-critical behaviors require this.
- **Approval workflow.** The system does not require human approval of
  agent-authored decisions. Humans review at PR time via the normal code
  review process.
- **Cross-repository memory.** Decisions are per-repo. Organization-wide
  patterns require separate tooling.
- **Semantic search.** The `plan` command uses term-based index lookup, not
  embedding-based similarity. This keeps the index a plain JSON file with
  no external dependencies and makes its contents inspectable and
  reconstructible without a model.

## Success Criteria

The tool is working when:

1. The store remains complete: every dependency in `pyproject.toml` is
   covered by some decision (accepted or observed) at all times.
2. Agents consult existing decisions before proposing alternatives that
   have already been evaluated.
3. Dependency additions consistently produce recorded rationale rather than
   being backstopped by reconciliation.
4. Observed entries are progressively promoted as agents encounter relevant
   code with available context.
5. Superseded decisions retain links to their replacements, allowing agents
   to trace architectural evolution.
6. Agents issue `plan` before beginning non-trivial tasks, and the response
   demonstrably surfaces context they then use — visible in the causal
   sequence of a `plan` call followed by a `propose` or `promote` call in
   the same session.

The integrity section of `adr-agent report` is the primary diagnostic. Low
reconciliation counts mean write discipline is holding. A healthy promotion
ratio means pull mechanisms are surfacing context effectively. A shrinking
observed-via-seed count means the seeded baseline is being progressively
replaced with real rationale. A growing `plan` invocation count relative to
`show` invocations indicates agents are adopting the pre-task query
pattern.
