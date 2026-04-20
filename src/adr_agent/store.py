from __future__ import annotations

import datetime
from pathlib import Path
from typing import Optional

import frontmatter

from .models import Decision, ObservedVia, Scope, Status


class DecisionStore:
    def __init__(self, decisions_dir: Path):
        self.decisions_dir = decisions_dir

    def load_all(self) -> list[Decision]:
        if not self.decisions_dir.exists():
            return []
        decisions = []
        for path in sorted(self.decisions_dir.glob("ADR-*.md")):
            try:
                decisions.append(self._read(path))
            except Exception:
                pass
        return decisions

    def get(self, adr_id: str) -> Optional[Decision]:
        adr_id = adr_id.upper()
        if not adr_id.startswith("ADR-"):
            adr_id = f"ADR-{adr_id}"
        for path in self.decisions_dir.glob(f"{adr_id}-*.md"):
            return self._read(path)
        return None

    def save(self, decision: Decision) -> Path:
        self.decisions_dir.mkdir(parents=True, exist_ok=True)
        path = self.decisions_dir / decision.filename
        body = _build_body(decision)
        post = frontmatter.Post(body, **decision.to_frontmatter())
        path.write_text(frontmatter.dumps(post))
        return path

    def next_id(self) -> str:
        existing = self.load_all()
        if not existing:
            return "ADR-0001"
        max_num = max(d.num for d in existing)
        return f"ADR-{max_num + 1:04d}"

    def find_covering(self, package_name: str) -> list[Decision]:
        """Find decisions that cover a given package (by title or scope tags)."""
        name_lower = package_name.lower()
        results = []
        for d in self.load_all():
            if d.status in (Status.SUPERSEDED, Status.REJECTED):
                continue
            if name_lower in d.title.lower():
                results.append(d)
                continue
            if name_lower in [t.lower() for t in d.scope.tags]:
                results.append(d)
        return results

    def search_alternatives(self, topic: str) -> list[tuple[Decision, list]]:
        """Return decisions that mention topic as an alternative name."""
        topic_lower = topic.lower()
        results = []
        for d in self.load_all():
            matches = [a for a in d.alternatives if topic_lower in a.name.lower()]
            if matches:
                results.append((d, matches))
        return results

    def history(self, path_or_tag: str) -> list[Decision]:
        """All decisions (including superseded) covering a path or tag."""
        needle = path_or_tag.lower()
        results = []
        for d in self.load_all():
            if needle in [t.lower() for t in d.scope.tags]:
                results.append(d)
                continue
            if any(needle in p.lower() for p in d.scope.paths):
                results.append(d)
        return sorted(results, key=lambda d: d.created)

    def check_constraint(self, tag: str) -> list[tuple[Decision, list]]:
        """Return decisions and alternatives referencing a constraint tag."""
        tag_lower = tag.lower()
        results = []
        for d in self.load_all():
            decision_matches = tag_lower in [c.lower() for c in d.constraints_depended_on]
            alt_matches = [a for a in d.alternatives if a.constraint and tag_lower == a.constraint.lower()]
            if decision_matches or alt_matches:
                results.append((d, alt_matches))
        return results

    def _read(self, path: Path) -> Decision:
        post = frontmatter.load(str(path))
        return Decision.from_frontmatter(dict(post.metadata), post.content)


def _build_body(decision: Decision) -> str:
    parts = []
    if decision.context_text:
        parts.append(f"## Context\n{decision.context_text}")
    if decision.decision_text:
        parts.append(f"## Decision\n{decision.decision_text}")
    if decision.consequences_text:
        parts.append(f"## Consequences\n{decision.consequences_text}")
    return "\n\n".join(parts) + "\n" if parts else ""


def create_observed(
    package_name: str,
    store: DecisionStore,
    observed_via: ObservedVia,
    created: Optional[datetime.date] = None,
) -> Decision:
    from .models import Confidence
    decision = Decision(
        id=store.next_id(),
        title=f"Uses {package_name}",
        status=Status.OBSERVED,
        created=created or datetime.date.today(),
        confidence=Confidence.MEDIUM,
        scope=Scope(tags=[package_name]),
        observed_via=observed_via,
    )
    store.save(decision)
    return decision
