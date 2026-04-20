from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path
from typing import Optional

import click

from . import llm as llm_module
from .hooks import run_hook
from .models import Alternative, Confidence, Outcome, Reversible, Scope, Status
from .reconciler import get_runtime_deps, reconcile
from .report import generate_report
from .session import EventLogger, SessionState, get_current_session_id
from .settings import add_adr_hooks, check_hooks_present, remove_adr_hooks
from .store import DecisionStore, create_observed


def _find_project_root() -> Path:
    path = Path.cwd()
    for parent in [path] + list(path.parents):
        if (parent / ".adr-agent").exists():
            return parent
    return path


def _require_initialized(project_root: Path) -> None:
    if not (project_root / ".adr-agent").exists():
        raise click.ClickException(
            "adr-agent is not initialized in this repository. Run `adr-agent init` first."
        )


def _make_store(project_root: Path) -> DecisionStore:
    return DecisionStore(project_root / ".adr-agent" / "decisions")


def _sessions_dir(project_root: Path) -> Path:
    return project_root / ".adr-agent" / "sessions"


def _get_logger(project_root: Path) -> Optional[EventLogger]:
    sessions_dir = _sessions_dir(project_root)
    session_id = get_current_session_id(sessions_dir)
    if session_id:
        return EventLogger(sessions_dir, session_id)
    return None


_PRIVACY_NOTICE = """\
adr-agent records architectural decisions for use by AI agents.

Before initializing, please note:

1. Decision files are committed to git and become part of your
   repository's permanent history. Rationale and alternatives will
   be visible to everyone with repo access. Treat them with the
   same sensitivity as source code.

2. Session logs are stored locally under .adr-agent/sessions/ and
   are gitignored by default. They contain command metadata, not
   decision content.

3. adr-agent does not transmit any data externally. No telemetry,
   no central collection.

4. The aggregate pattern of decisions can reveal information even
   when individual decisions are innocuous.
"""

_FIRST_RUN_MARKER = Path.home() / ".adr-agent-initialized"


@click.group()
def main() -> None:
    """adr-agent — per-repository architectural memory for AI agents."""


# ── init ──────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--yes", "-y", is_flag=True, help="Skip privacy confirmation prompt.")
def init(yes: bool) -> None:
    """Initialize adr-agent in the current repository."""
    project_root = Path.cwd()

    # Privacy notice on first run
    if not _FIRST_RUN_MARKER.exists():
        click.echo(_PRIVACY_NOTICE)
        if not yes:
            if not click.confirm("Proceed with init?", default=False):
                click.echo("Aborted.")
                return
        _FIRST_RUN_MARKER.parent.mkdir(parents=True, exist_ok=True)
        _FIRST_RUN_MARKER.touch()

    adr_dir = project_root / ".adr-agent"
    adr_dir.mkdir(exist_ok=True)
    (adr_dir / "decisions").mkdir(exist_ok=True)
    (adr_dir / "sessions").mkdir(exist_ok=True)

    # Gitignore sessions/
    gitignore = project_root / ".gitignore"
    gitignore_entry = ".adr-agent/sessions/"
    if gitignore.exists():
        content = gitignore.read_text()
        if gitignore_entry not in content:
            gitignore.write_text(content.rstrip() + f"\n{gitignore_entry}\n")
    else:
        gitignore.write_text(f"{gitignore_entry}\n")

    # Seed from pyproject.toml
    pyproject = project_root / "pyproject.toml"
    store = _make_store(project_root)
    seeded = []
    if pyproject.exists():
        from .models import ObservedVia
        seeded = reconcile(pyproject, store, observed_via=ObservedVia.SEED)

    # Configure hooks
    add_adr_hooks(project_root)

    click.echo("Initialized adr-agent.")
    if seeded:
        click.echo(f"Seeded {len(seeded)} observed entr{'y' if len(seeded)==1 else 'ies'} from pyproject.toml:")
        for pkg in seeded:
            click.echo(f"  {pkg}")
    click.echo("Hooks configured in .claude/settings.json.")


# ── show ──────────────────────────────────────────────────────────────────────

@main.command()
@click.argument("adr_id")
def show(adr_id: str) -> None:
    """Display a full decision record."""
    project_root = _find_project_root()
    _require_initialized(project_root)
    store = _make_store(project_root)
    decision = store.get(adr_id)
    if decision is None:
        raise click.ClickException(f"Decision {adr_id} not found.")

    logger = _get_logger(project_root)
    if logger:
        logger.log_voluntary("show", [decision.id])

    lines = [f"# {decision.id}: {decision.title}"]
    lines.append(f"Status: {decision.status.value}  |  Confidence: {decision.confidence.value}  |  Created: {decision.created}")
    if decision.scope.tags:
        lines.append(f"Tags: {', '.join(decision.scope.tags)}")
    if decision.scope.paths:
        lines.append(f"Paths: {', '.join(decision.scope.paths)}")
    if decision.supersedes:
        lines.append(f"Supersedes: {', '.join(decision.supersedes)}")
    if decision.superseded_by:
        lines.append(f"Superseded by: {', '.join(decision.superseded_by)}")
    if decision.constraints_depended_on:
        lines.append(f"Constraints: {', '.join(decision.constraints_depended_on)}")
    if decision.observed_via:
        lines.append(f"Observed via: {decision.observed_via.value}")

    if decision.alternatives:
        lines.append("\nAlternatives:")
        for alt in decision.alternatives:
            rev = f"reversible: {alt.reversible.value}"
            constraint = f", constraint: {alt.constraint}" if alt.constraint else ""
            lines.append(f"  [{alt.outcome.value}] {alt.name}")
            lines.append(f"    {alt.reason} ({rev}{constraint})")

    if decision.context_text:
        lines.append(f"\n## Context\n{decision.context_text}")
    if decision.decision_text:
        lines.append(f"\n## Decision\n{decision.decision_text}")
    if decision.consequences_text:
        lines.append(f"\n## Consequences\n{decision.consequences_text}")

    click.echo("\n".join(lines))

    if decision.status == Status.OBSERVED:
        click.echo(
            f"\n[Observed entry] Run `adr-agent promote {decision.id}` to capture rationale if you have context."
        )


# ── considered ────────────────────────────────────────────────────────────────

@main.command()
@click.argument("topic")
def considered(topic: str) -> None:
    """Show all decisions where TOPIC was evaluated as an alternative."""
    project_root = _find_project_root()
    _require_initialized(project_root)
    store = _make_store(project_root)

    logger = _get_logger(project_root)
    if logger:
        logger.log_voluntary("considered", [topic])

    results = store.search_alternatives(topic)
    if not results:
        click.echo(f"No decisions found where '{topic}' was considered.")
        return

    by_outcome: dict[str, list] = {"chosen": [], "not-chosen": [], "rejected": []}
    for decision, alts in results:
        for alt in alts:
            by_outcome[alt.outcome.value].append((decision, alt))

    labels = {"chosen": "CHOSEN", "not-chosen": "NOT-CHOSEN", "rejected": "REJECTED"}
    for outcome, label in labels.items():
        entries = by_outcome[outcome]
        if not entries:
            continue
        click.echo(label)
        for decision, alt in entries:
            sup = f" superseded by {decision.superseded_by[0]}" if decision.superseded_by else ""
            click.echo(f"  {decision.id} ({decision.created}) [{alt.name}] for {_infer_purpose(decision)}{sup}")
            click.echo(f'    "{alt.reason}"')
            click.echo(f"    reversible: {alt.reversible.value}")


def _infer_purpose(decision) -> str:
    title = decision.title.lower()
    if title.startswith("use "):
        return title[4:]
    if title.startswith("uses "):
        return title[5:]
    return title[:40]


# ── history ───────────────────────────────────────────────────────────────────

@main.command()
@click.argument("path_or_tag")
def history(path_or_tag: str) -> None:
    """Show all decisions that have governed a path or tag, chronologically."""
    project_root = _find_project_root()
    _require_initialized(project_root)
    store = _make_store(project_root)

    logger = _get_logger(project_root)
    if logger:
        logger.log_voluntary("history", [path_or_tag])

    decisions = store.history(path_or_tag)
    if not decisions:
        click.echo(f"No decisions found for '{path_or_tag}'.")
        return

    for d in decisions:
        sup_note = f" → superseded by {d.superseded_by[0]}" if d.superseded_by else ""
        click.echo(f"{d.id} ({d.created})  [{d.status.value}]{sup_note}")
        click.echo(f"  {d.title}")


# ── check-constraint ──────────────────────────────────────────────────────────

@main.command("check-constraint")
@click.argument("tag")
def check_constraint(tag: str) -> None:
    """Find all decisions and alternatives that depend on a constraint tag."""
    project_root = _find_project_root()
    _require_initialized(project_root)
    store = _make_store(project_root)

    logger = _get_logger(project_root)
    if logger:
        logger.log_voluntary("check-constraint", [tag])

    results = store.check_constraint(tag)
    if not results:
        click.echo(f"No decisions reference constraint '{tag}'.")
        return

    for decision, alt_matches in results:
        constraint_in_decision = tag.lower() in [c.lower() for c in decision.constraints_depended_on]
        if constraint_in_decision:
            click.echo(f"{decision.id}: {decision.title}")
            click.echo(f"  Constraint '{tag}' is depended upon by this decision.")
        for alt in alt_matches:
            click.echo(f"  Alternative '{alt.name}' was {alt.outcome.value} due to constraint '{tag}'.")
            click.echo(f"  Reason: {alt.reason}")


# ── propose ───────────────────────────────────────────────────────────────────

@main.command()
@click.option("--dependency", default=None, help="Dependency change that triggered this proposal.")
@click.option("--relevant-adrs", default=None, help="Comma-separated ADR IDs relevant to this proposal.")
@click.option("--path", "scope_path", default=None, help="File path scope hint.")
def propose(dependency: Optional[str], relevant_adrs: Optional[str], scope_path: Optional[str]) -> None:
    """Interactively record a new architectural decision."""
    project_root = _find_project_root()
    _require_initialized(project_root)
    store = _make_store(project_root)

    # Pre-fill defaults from triggered context
    default_title = f"Add {dependency}" if dependency else ""
    default_scope_tags = dependency or ""
    default_scope_paths = scope_path or ""
    default_supersedes = ""

    relevant_decisions = []
    if relevant_adrs:
        for adr_id in relevant_adrs.split(","):
            d = store.get(adr_id.strip())
            if d:
                relevant_decisions.append(d)
                click.echo(f"Relevant decision: {d.id}: {d.title}")

    title = click.prompt("Decision title (one sentence)", default=default_title or "")
    rationale = click.prompt("Rationale (what context led to this choice)")
    confidence_raw = click.prompt(
        "Confidence",
        type=click.Choice(["low", "medium", "high"]),
        default="medium",
    )
    confidence = Confidence(confidence_raw)

    tags_raw = click.prompt("Scope tags (comma-separated, or empty)", default=default_scope_tags)
    paths_raw = click.prompt("Scope paths (comma-separated, or empty)", default=default_scope_paths)
    constraints_raw = click.prompt("Constraints depended on (comma-separated, or empty)", default="")
    supersedes_raw = click.prompt(
        "Supersedes (ADR IDs, comma-separated, or empty)", default=default_supersedes
    )

    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    paths = [p.strip() for p in paths_raw.split(",") if p.strip()]
    constraints = [c.strip() for c in constraints_raw.split(",") if c.strip()]
    supersedes = [s.strip() for s in supersedes_raw.split(",") if s.strip()]

    alternatives: list[Alternative] = []
    while click.confirm("Add an alternative?", default=False):
        alt_name = click.prompt("Alternative name")
        alt_outcome_raw = click.prompt(
            "Outcome",
            type=click.Choice(["chosen", "not-chosen", "rejected"]),
        )
        alt_reason = click.prompt("Reason")
        alt_reversible_raw = click.prompt(
            "Reversible",
            type=click.Choice(["cheap", "costly", "no"]),
        )
        alt_constraint = click.prompt("Constraint (optional, or empty)", default="") or None
        alternatives.append(
            Alternative(
                name=alt_name,
                outcome=Outcome(alt_outcome_raw),
                reason=alt_reason,
                reversible=Reversible(alt_reversible_raw),
                constraint=alt_constraint,
            )
        )

    # Generate prose body via LLM
    alt_summary = "; ".join(f"{a.name} ({a.outcome.value})" for a in alternatives) if alternatives else ""
    client = llm_module.get_client()
    context_text, decision_text, consequences_text = client.generate_adr_body(
        title=title,
        rationale=rationale,
        alternatives_summary=alt_summary,
        constraints=constraints,
        supersedes=supersedes,
    )

    # Update superseded decisions
    for sup_id in supersedes:
        sup_decision = store.get(sup_id)
        if sup_decision:
            # Will be updated by saving later; track forward link
            pass

    adr_id = store.next_id()
    from .models import Decision
    decision = Decision(
        id=adr_id,
        title=title,
        status=Status.ACCEPTED,
        created=datetime.date.today(),
        confidence=confidence,
        scope=Scope(tags=tags, paths=paths),
        alternatives=alternatives,
        supersedes=supersedes,
        constraints_depended_on=constraints,
        context_text=context_text,
        decision_text=decision_text,
        consequences_text=consequences_text,
    )
    path = store.save(decision)

    # Update superseded_by on parent decisions
    for sup_id in supersedes:
        sup_decision = store.get(sup_id)
        if sup_decision:
            if adr_id not in sup_decision.superseded_by:
                sup_decision.superseded_by.append(adr_id)
            sup_decision.status = Status.SUPERSEDED
            store.save(sup_decision)

    # Log and update session state
    logger = _get_logger(project_root)
    if logger:
        logger.log_voluntary("propose", [adr_id])

    sessions_dir = _sessions_dir(project_root)
    session_id = get_current_session_id(sessions_dir)
    if session_id:
        state = SessionState(sessions_dir, session_id)
        state.record_propose_called([dependency] if dependency else [])

    click.echo(f"\nWritten: {path}")


# ── promote ───────────────────────────────────────────────────────────────────

@main.command()
@click.argument("adr_id")
@click.option("--context", "context_text", default=None, help="Context explaining the original choice.")
def promote(adr_id: str, context_text: Optional[str]) -> None:
    """Promote an observed entry to accepted by capturing rationale."""
    project_root = _find_project_root()
    _require_initialized(project_root)
    store = _make_store(project_root)

    decision = store.get(adr_id)
    if decision is None:
        raise click.ClickException(f"Decision {adr_id} not found.")
    if decision.status != Status.OBSERVED:
        raise click.ClickException(f"{adr_id} is not an observed entry (status: {decision.status.value}).")

    click.echo(f"Promoting {decision.id}: {decision.title}")

    if context_text is None:
        context_text = click.prompt(
            "Provide context for why this dependency was originally adopted "
            "(or describe what you know about it)"
        )

    confidence_raw = click.prompt(
        "Confidence",
        type=click.Choice(["low", "medium", "high"]),
        default="medium",
    )
    confidence = Confidence(confidence_raw)

    tags_raw = click.prompt("Scope tags (comma-separated, or empty)", default=",".join(decision.scope.tags))
    paths_raw = click.prompt("Scope paths (comma-separated, or empty)", default=",".join(decision.scope.paths))
    constraints_raw = click.prompt("Constraints depended on (comma-separated, or empty)", default="")

    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    paths = [p.strip() for p in paths_raw.split(",") if p.strip()]
    constraints = [c.strip() for c in constraints_raw.split(",") if c.strip()]

    alternatives: list[Alternative] = list(decision.alternatives)
    if click.confirm("Add alternatives?", default=False):
        while True:
            alt_name = click.prompt("Alternative name")
            alt_outcome_raw = click.prompt("Outcome", type=click.Choice(["chosen", "not-chosen", "rejected"]))
            alt_reason = click.prompt("Reason")
            alt_reversible_raw = click.prompt("Reversible", type=click.Choice(["cheap", "costly", "no"]))
            alt_constraint = click.prompt("Constraint (optional, or empty)", default="") or None
            alternatives.append(
                Alternative(
                    name=alt_name,
                    outcome=Outcome(alt_outcome_raw),
                    reason=alt_reason,
                    reversible=Reversible(alt_reversible_raw),
                    constraint=alt_constraint,
                )
            )
            if not click.confirm("Add another alternative?", default=False):
                break

    # Generate prose body via LLM
    client = llm_module.get_client()
    new_context, new_decision, new_consequences = client.generate_promotion_body(
        title=decision.title,
        context_provided=context_text,
        existing_context=decision.context_text,
    )

    decision.status = Status.ACCEPTED
    decision.confidence = confidence
    decision.scope = Scope(tags=tags, paths=paths)
    decision.constraints_depended_on = constraints
    decision.alternatives = alternatives
    decision.context_text = new_context
    decision.decision_text = new_decision
    decision.consequences_text = new_consequences
    # Preserve observed_via in frontmatter for reporting purposes

    path = store.save(decision)

    logger = _get_logger(project_root)
    if logger:
        logger.log_voluntary("promote", [decision.id])

    click.echo(f"\nPromoted {decision.id} to accepted. Written: {path}")


# ── report ────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--since", default=None, help="Filter events since this time (e.g. '2 weeks ago').")
def report(since: Optional[str]) -> None:
    """Display a summary of adr-agent activity and store integrity."""
    project_root = _find_project_root()
    _require_initialized(project_root)
    store = _make_store(project_root)
    sessions_dir = _sessions_dir(project_root)
    click.echo(generate_report(sessions_dir, store, since_str=since))


# ── doctor ────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--repair", is_flag=True, help="Repair missing or outdated hook configuration.")
def doctor(repair: bool) -> None:
    """Check hook configuration health."""
    project_root = _find_project_root()
    _require_initialized(project_root)

    status = check_hooks_present(project_root)
    all_ok = all(status.values())

    for event, present in status.items():
        mark = "✓" if present else "✗"
        click.echo(f"  {mark} {event} hook")

    if all_ok:
        click.echo("Hook configuration is healthy.")
    else:
        click.echo("Some hooks are missing or misconfigured.")
        if repair:
            add_adr_hooks(project_root)
            click.echo("Repaired hook configuration.")
        else:
            click.echo("Run `adr-agent doctor --repair` to fix.")


# ── uninstall ─────────────────────────────────────────────────────────────────

@main.command()
@click.option("--yes", "-y", is_flag=True)
def uninstall(yes: bool) -> None:
    """Remove adr-agent hook configuration from .claude/settings.json."""
    project_root = _find_project_root()
    _require_initialized(project_root)

    if not yes:
        if not click.confirm("Remove adr-agent hooks from .claude/settings.json?", default=False):
            click.echo("Aborted.")
            return

    remove_adr_hooks(project_root)
    click.echo("adr-agent hooks removed from .claude/settings.json.")
    click.echo("The .adr-agent/ directory and decision files are preserved.")


# ── privacy ───────────────────────────────────────────────────────────────────

@main.command()
def privacy() -> None:
    """Display the privacy notice."""
    click.echo(_PRIVACY_NOTICE)


# ── hook subcommands ──────────────────────────────────────────────────────────

@main.command("session-start", hidden=True)
def session_start() -> None:
    """Hook: runs at session start."""
    run_hook("session-start")


@main.command("pre-tool-use", hidden=True)
def pre_tool_use() -> None:
    """Hook: runs before tool use."""
    run_hook("pre-tool-use")


@main.command("post-tool-use", hidden=True)
def post_tool_use() -> None:
    """Hook: runs after tool use."""
    run_hook("post-tool-use")


@main.command("session-end", hidden=True)
def session_end() -> None:
    """Hook: runs at session end."""
    run_hook("session-end")
