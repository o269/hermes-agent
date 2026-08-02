#!/usr/bin/env python3
"""Fail-closed assignment policy for the fleet authority lane.

This module is deliberately small and dependency-free so both the broker bridge
and its installer can use the same policy before any board mutation occurs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

AUTHORITY_PROFILE = "fable"

# Title-marker classification is deliberately two-phase: first isolate bounded
# bracket contents, then search each content block for complete marker tokens.
# This keeps token order irrelevant (``[LAND+FIX]`` and ``[FIX+LAND]`` are both
# mixed) without treating prose outside brackets as an authority declaration.
_BRACKET_CONTENT = re.compile(r"\[([^\]]*)\]")
_AUTHORITY_TITLE_TOKEN = re.compile(
    r"(?<![A-Z0-9])(?:LAND|APPLY|OPERATOR|ACCEPT|ACCEPTANCE|"
    r"APPROVAL|APPROVE|AUTHORITY|INSTALL|DEPLOY|MERGE|CUTOVER|RELEASE|"
    r"SUPERSESSION|RECOVERY|DECISION)(?![A-Z0-9])",
    re.IGNORECASE,
)
_EXECUTOR_TITLE_TOKEN = re.compile(
    r"(?<![A-Z0-9])(?:AUTHOR|FIX|REVIEW|EXEC|IMPLEMENT|BUILD|TEST|VERIFY|"
    r"AUDIT|REWORK|REBIND|MIGRAT(?:E|ION))(?![A-Z0-9])",
    re.IGNORECASE,
)

# The broader prose shape also checks title + body so an authority-less neutral
# title cannot hide an implementation deliverable in the opening post.
_EXECUTOR_SHAPE = re.compile(
    r"\b(?:author(?:ing)?|implement(?:ation)?|"
    r"fix|review|rework|rebind|build|test|verify|audit|migrat(?:e|ion)|"
    r"source[- ]?code|pull request|PR\s*#?\d+)\b",
    re.IGNORECASE,
)


class AuthorityLaneError(ValueError):
    """Raised before a forbidden executor-to-authority assignment is written."""


@dataclass(frozen=True)
class CardClassification:
    """Canonical title/body classification used before custody decisions."""

    executor: bool
    executor_marker: bool
    authority: bool

    @property
    def mixed(self) -> bool:
        """Return whether executor-shaped work and authority occur together."""
        return self.executor and self.authority

    @property
    def pure_authority(self) -> bool:
        """Return whether authority is explicit without executor-shaped work."""
        return self.authority and not self.executor


def normalize_assignee(value: Optional[str]) -> Optional[str]:
    """Return a canonical profile label while preserving an omitted assignee."""
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def _classify_title_markers(title: str) -> tuple[bool, bool]:
    """Return ``(executor, authority)`` for complete tokens inside brackets."""
    executor = False
    authority = False
    for match in _BRACKET_CONTENT.finditer(title or ""):
        content = match.group(1)
        executor = executor or bool(_EXECUTOR_TITLE_TOKEN.search(content))
        authority = authority or bool(_AUTHORITY_TITLE_TOKEN.search(content))
        if executor and authority:
            break
    return executor, authority


def classify_card(title: str, body: Optional[str] = None) -> CardClassification:
    """Classify executable and authority markers before choosing custody.

    The two dimensions are intentionally retained instead of collapsing mixed
    cards into authority work.  Callers can therefore apply the fleet's
    executable-marker-wins rule and fail closed when an automatic lifecycle
    transition would otherwise hide an ambiguous mixed card.
    """
    executor_marker, authority = _classify_title_markers(title)
    return CardClassification(
        executor=executor_marker
        or bool(_EXECUTOR_SHAPE.search(f"{title or ''}\n{body or ''}")),
        executor_marker=executor_marker,
        authority=authority,
    )


def is_explicit_authority_card(title: str, body: Optional[str] = None) -> bool:
    """Return whether title/body are explicit, unambiguously authority-only work."""
    return classify_card(title, body).pure_authority


def is_executor_shaped(title: str, body: Optional[str] = None) -> bool:
    """Return whether the card asks for implementation/review executor work."""
    return classify_card(title, body).executor


def resolve_transition_assignee(
    current_assignee: Optional[str],
    requested_assignee: Optional[str],
    *,
    title: Optional[str] = None,
    body: Optional[str] = None,
    operation: str = "transition",
    reject_implicit_mixed: bool = False,
) -> Optional[str]:
    """Resolve transition custody without hiding a mixed review card.

    Omitted assignees normally preserve the current executor.  Review-state
    callers set ``reject_implicit_mixed`` so authority-tagged executor work in
    either title or body cannot silently cross the lifecycle boundary without
    being split or explicitly routed back to an executor.
    """
    requested = normalize_assignee(requested_assignee)
    if requested is not None:
        return requested
    if reject_implicit_mixed:
        classification = classify_card(title or "", body)
        if classification.mixed:
            raise AuthorityLaneError(
                f"{operation} rejected: mixed executor/authority work cannot "
                "use an implicit assignee; keep the executor card explicit and "
                "create a distinct authority card"
            )
    return normalize_assignee(current_assignee)


def validate_assignment(
    *,
    title: str,
    body: Optional[str],
    assignee: Optional[str],
    operation: str,
) -> Optional[str]:
    """Validate and return the normalized assignee.

    No executor-shaped work may target the Fable authority lane, even when an
    authority marker is also present.  For non-executor cards, Fable custody
    additionally requires an explicit authority action tag such as ``[LAND]``,
    ``[APPLY]``, ``[OPERATOR-GATE]``, or ``[ACCEPTANCE]``.
    """
    target = normalize_assignee(assignee)
    classification = classify_card(title, body)
    if target == AUTHORITY_PROFILE and not classification.pure_authority:
        work_kind = "executor-shaped work" if classification.executor else (
            "work without an explicit authority action"
        )
        raise AuthorityLaneError(
            f"{operation} rejected: {work_kind} cannot target "
            f"authority profile {AUTHORITY_PROFILE!r}; keep the implementation/"
            "review card on its executor and create a distinct explicit "
            "[LAND]/[APPLY]/[OPERATOR-GATE]/[ACCEPTANCE] authority card"
        )
    return target
