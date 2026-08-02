#!/usr/bin/env python3
"""Fail-closed assignment policy for the fleet authority lane.

This module is deliberately small and dependency-free so both the broker bridge
and its installer can use the same policy before any board mutation occurs.
"""
from __future__ import annotations

import re
from typing import Optional

AUTHORITY_PROFILE = "fable"

# Authority custody must be explicit in a bracketed title marker.  A bare
# ``[FABLE]`` identity marker is not enough: the action itself must be named.
_AUTHORITY_TITLE_TAG = re.compile(
    r"\[[^\]]*(?<![A-Z0-9])(?:LAND|APPLY|OPERATOR|ACCEPT|ACCEPTANCE|"
    r"APPROVAL|APPROVE|AUTHORITY|INSTALL|DEPLOY|MERGE|CUTOVER|RELEASE|"
    r"SUPERSESSION|RECOVERY|DECISION)(?![A-Z0-9])[^\]]*\]",
    re.IGNORECASE,
)

# These markers describe work an executor/reviewer performs.  They are checked
# across title + body because a neutral-looking title must not hide an
# implementation deliverable in the opening post.
_EXECUTOR_SHAPE = re.compile(
    r"(?:\[(?:AUTHOR|FIX|REVIEW|EXEC|IMPLEMENT|BUILD|TEST|VERIFY|AUDIT|"
    r"REWORK|REBIND|MIGRAT(?:E|ION))[^\]]*\]|\b(?:author(?:ing)?|implement(?:ation)?|"
    r"fix|review|rework|rebind|build|test|verify|audit|migrat(?:e|ion)|"
    r"source[- ]?code|pull request|\bPR\s*#?\d+)\b)",
    re.IGNORECASE,
)


class AuthorityLaneError(ValueError):
    """Raised before a forbidden executor-to-authority assignment is written."""


def normalize_assignee(value: Optional[str]) -> Optional[str]:
    """Return a canonical profile label while preserving an omitted assignee."""
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def is_explicit_authority_card(title: str) -> bool:
    """Return whether the title names an allowed authority action explicitly."""
    return bool(_AUTHORITY_TITLE_TAG.search(title or ""))


def is_executor_shaped(title: str, body: Optional[str] = None) -> bool:
    """Return whether the card asks for implementation/review executor work."""
    return bool(_EXECUTOR_SHAPE.search(f"{title or ''}\n{body or ''}"))


def resolve_transition_assignee(
    current_assignee: Optional[str],
    requested_assignee: Optional[str],
) -> Optional[str]:
    """Preserve current executor custody when ``--assignee`` is omitted."""
    requested = normalize_assignee(requested_assignee)
    if requested is not None:
        return requested
    return normalize_assignee(current_assignee)


def validate_assignment(
    *,
    title: str,
    body: Optional[str],
    assignee: Optional[str],
    operation: str,
) -> Optional[str]:
    """Validate and return the normalized assignee.

    No work may target the Fable authority lane unless the title carries an
    explicit authority action tag such as ``[LAND]``, ``[APPLY]``,
    ``[OPERATOR-GATE]``, or ``[ACCEPTANCE]``.  This allow-list removes
    classifier false negatives; executor classification is retained so the
    incident-shaped rejection is unambiguous.  Authority tags win when a land
    gate legitimately references review evidence.
    """
    target = normalize_assignee(assignee)
    if target == AUTHORITY_PROFILE and not is_explicit_authority_card(title):
        work_kind = (
            "executor-shaped work"
            if is_executor_shaped(title, body)
            else "work without an explicit authority action"
        )
        raise AuthorityLaneError(
            f"{operation} rejected: {work_kind} cannot target "
            f"authority profile {AUTHORITY_PROFILE!r}; keep the implementation/"
            "review card on its executor and create a distinct explicit "
            "[LAND]/[APPLY]/[OPERATOR-GATE]/[ACCEPTANCE] authority card"
        )
    return target
