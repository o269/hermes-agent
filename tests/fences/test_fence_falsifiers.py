"""Must-fire falsifier harness: proves fence fixtures are not always-green.

WHY THIS EXISTS
---------------
A "fence" is a guard that refuses bad work (duplicate card re-mints, spawn
bursts, half-applied receipts). Every fence in this repo ships with tests, and
every fence PR *claims* in prose that "removing the fence turns the fixture
red". Nothing mechanically re-proved that claim, so a fixture could rot into
always-green — passing whether or not the fence still exists — and no CI signal
would notice. That is asserted state, not derived state.

This harness derives it. Each registered fence declares:

* an **ablation** — the narrowest possible removal of the fence mechanism,
  expressed as monkeypatches over the real symbols that implement it;
* **must-fire fixtures** — behaviours that exist *only because* the fence
  exists;
* **must-not-fire fixtures** — legitimate work the fence must never touch,
  i.e. the over-firing guard.

The driver then runs a 2x2 matrix per fixture and asserts all four cells:

                        fence intact        fence ablated
    must-fire           GREEN               RED (AssertionError)
    must-not-fire       GREEN               GREEN

The interesting cell is the top-right. A must-fire fixture that stays green
with its fence removed is **vacuous** and this harness fails loudly, naming the
fixture. The bottom-right cell is the mirror check: a "negative" fixture that
goes red under ablation was secretly testing the fence, not the legitimate
path, and is therefore mislabelled.

ADDING A FENCE
--------------
Append a :class:`FenceSpec` to :data:`FENCES`. Fixture callables take no
arguments and run against an isolated board provided by the ``board`` fixture;
they must fail via ``assert`` (an AssertionError is the RED signal — any other
exception is reported as a harness bug, because it means the fixture stopped
exercising the mechanism rather than detecting its absence).

Ablations name their targets as dotted paths. If a fence is renamed or moved,
``monkeypatch.setattr`` raises and ``test_ablation_targets_still_exist`` fails
with the stale path — the harness cannot silently point at nothing.

Seeded with the create-dedup fence merged as PR #75.
"""

from __future__ import annotations

import importlib
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import session_cold_archive as cold
from hermes_state import SessionDB

# ---------------------------------------------------------------------------
# Harness types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ablation:
    """A named, narrow removal of one fence mechanism."""

    describes: str
    targets: tuple[str, ...]
    apply: Callable[[pytest.MonkeyPatch], None]


@dataclass(frozen=True)
class FenceSpec:
    """One fence, its ablation, and its both-directions fixtures."""

    fence_id: str
    origin: str
    mechanism: str
    ablation: Ablation
    must_fire: Mapping[str, Callable[[], None]]
    must_not_fire: Mapping[str, Callable[[], None]]


def _resolve(dotted: str) -> object:
    """Resolve ``pkg.module.attr`` to the attribute object (raises if absent)."""
    module_path, _, attr = dotted.rpartition(".")
    module = importlib.import_module(module_path)
    return getattr(module, attr)


# ---------------------------------------------------------------------------
# Board fixture — one isolated kanban DB per matrix cell
# ---------------------------------------------------------------------------


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _dedup_events(conn) -> list[kb.Event]:
    rows = conn.execute(
        "SELECT task_id FROM task_events WHERE kind = 'create_deduplicated' "
        "ORDER BY id ASC"
    ).fetchall()
    seen: list[kb.Event] = []
    for row in rows:
        seen.extend(
            event
            for event in kb.list_events(conn, str(row["task_id"]))
            if event.kind == "create_deduplicated"
        )
    return seen


def _open_task_count(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status NOT IN ('done', 'archived')"
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# FENCE: kanban.create.dedup.title_scope  (PR #75)
#
# create_task() refuses to mint a second open card whose normalized title +
# scope (tenant, dependency-parent set, project/workspace identity) already
# exists on the board, even when the retry supplies a fresh idempotency key.
# ---------------------------------------------------------------------------


def _ablate_title_scope_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the duplicate lookup — create_task can no longer see a twin."""

    def _never_finds_a_duplicate(
        conn,
        *,
        title,
        tenant,
        parents,
        project_id,
        workspace_kind,
        workspace_path,
    ):
        scope = kb._normalize_task_scope(
            tenant=tenant,
            parents=parents,
            project_id=project_id,
            workspace_kind=workspace_kind,
            workspace_path=workspace_path,
        )
        return None, scope

    monkeypatch.setattr(
        "hermes_cli.kanban_db._find_open_title_scope_duplicate",
        _never_finds_a_duplicate,
    )


def _mf_equivalent_open_card_is_not_re_minted() -> None:
    """A fresh idempotency key must not buy a second copy of open work."""
    with kb.connect() as conn:
        first = kb.create_task(
            conn,
            title="  Fix  ＡPI   retry ",
            body="first body wins",
            created_by="drain-steward",
            tenant="tenant-a",
            idempotency_key="attempt-1",
        )
        repeated = kb.create_task(
            conn,
            title="fix api retry",
            body="re-mint body must lose",
            created_by="flame-drain",
            tenant="tenant-a",
            idempotency_key="fresh-attempt-2",
        )
        open_cards = _open_task_count(conn)
        surviving = kb.get_task(conn, first)

    assert repeated == first, (
        f"re-mint minted a new card {repeated!r} instead of returning {first!r}"
    )
    assert open_cards == 1, f"expected 1 open card, board holds {open_cards}"
    assert surviving is not None
    assert surviving.body == "first body wins"


def _mf_unicode_and_whitespace_variants_fold_to_one_card() -> None:
    """NFKC + whitespace-fold + case-fold are part of the identity, not cosmetics."""
    with kb.connect() as conn:
        first = kb.create_task(conn, title="Ship the   RELEASE", tenant="t")
        variant = kb.create_task(conn, title="ship the release", tenant="t")
        open_cards = _open_task_count(conn)

    assert variant == first, (
        f"whitespace/case variant minted {variant!r} instead of folding to {first!r}"
    )
    assert open_cards == 1, f"expected 1 open card, board holds {open_cards}"


def _mf_non_ready_open_card_still_blocks_re_mint() -> None:
    """"Open" means every status except done/archived — including blocked."""
    with kb.connect() as conn:
        parked = kb.create_task(
            conn,
            title="Repair the wedged lane",
            tenant="t",
            initial_status="blocked",
        )
        parked_task = kb.get_task(conn, parked)
        repeated = kb.create_task(conn, title="repair the wedged lane", tenant="t")
        open_cards = _open_task_count(conn)

    assert parked_task is not None and parked_task.status == "blocked"
    assert repeated == parked, (
        f"re-mint against a blocked card minted {repeated!r}, expected {parked!r}"
    )
    assert open_cards == 1, f"expected 1 open card, board holds {open_cards}"


def _mf_re_mint_blocked_after_dispatcher_rewrites_workspace_path() -> None:
    """The fence must survive the dispatcher mutating the card it is keyed on.

    ``dispatch_ready`` resolves a scratch task's workspace at claim time and
    persists ``<board-root>/workspaces/<task-id>`` back onto the row through
    ``set_workspace_path``. That value is keyed on the task's OWN id, so while
    ``workspace_path`` was part of the identity key the fence stopped matching
    the instant the original started running — inert against exactly the case
    it exists for. On the live fleet board 3,101 of 4,438 scratch rows carry
    such a rewritten path.
    """
    with kb.connect() as conn:
        original = kb.create_task(
            conn, title="Re-mint me while running", tenant="tenant-a"
        )
        # Pre-claim the fence already worked; assert that first so this fixture
        # cannot pass by the fence being broken in both directions.
        pre_claim = kb.create_task(
            conn, title="re-mint me while running", tenant="tenant-a"
        )
        assert pre_claim == original, (
            f"pre-claim re-mint minted {pre_claim!r} instead of {original!r}"
        )

        minted_path = kb.workspaces_root() / original
        kb.set_workspace_path(conn, original, str(minted_path))
        claimed = kb.get_task(conn, original)
        assert claimed is not None and claimed.workspace_path == str(minted_path)

        post_claim = kb.create_task(
            conn, title="re-mint me while running", tenant="tenant-a"
        )
        open_cards = _open_task_count(conn)

    assert post_claim == original, (
        f"post-claim re-mint minted {post_claim!r} instead of {original!r} — the "
        "fence went inert once the dispatcher rewrote workspace_path"
    )
    assert open_cards == 1, f"expected 1 open card, board holds {open_cards}"


def _mf_re_mint_blocked_after_worktree_path_rewrite() -> None:
    """Same defect, ``worktree`` flavour: the dispatcher persists
    ``<repo>/.worktrees/<task-id>``, equally per-task-unique."""
    with kb.connect() as conn:
        original = kb.create_task(
            conn, title="Worktree card", tenant="t", workspace_kind="worktree"
        )
        kb.set_workspace_path(conn, original, f"/srv/repo/.worktrees/{original}")
        repeated = kb.create_task(
            conn, title="worktree card", tenant="t", workspace_kind="worktree"
        )
        open_cards = _open_task_count(conn)

    assert repeated == original, (
        f"re-mint against a claimed worktree card minted {repeated!r}, "
        f"expected {original!r}"
    )
    assert open_cards == 1, f"expected 1 open card, board holds {open_cards}"


def _mnf_distinct_work_all_mints() -> None:
    """Different title, tenant, parent set, or workspace is different work.

    The workspace axis is deliberately KEPT — and strengthened — now that
    ``workspace_path`` carries identity only for ``workspace_kind == "dir"``.
    ``dir`` is the one kind whose path ``resolve_workspace`` hands back
    unchanged, so two ``dir`` cards pointed at two different checkouts really
    are different work. The original pair compared a ``dir`` card against a
    default-``scratch`` card, which the workspace *kind* alone already
    separates; ``two_dir_paths`` below makes the path itself load-bearing.
    """
    with kb.connect() as conn:
        parent_a = kb.create_task(conn, title="parent A")
        parent_b = kb.create_task(conn, title="parent B")
        base = kb.create_task(conn, title="Run release", tenant="tenant-a")
        other_title = kb.create_task(
            conn, title="Run release verification", tenant="tenant-a"
        )
        other_tenant = kb.create_task(conn, title="run release", tenant="tenant-b")
        under_parent_a = kb.create_task(
            conn, title="run release", tenant="tenant-a", parents=[parent_a]
        )
        under_parent_b = kb.create_task(
            conn, title="run release", tenant="tenant-a", parents=[parent_b]
        )
        other_workspace = kb.create_task(
            conn,
            title="run release",
            tenant="tenant-a",
            workspace_kind="dir",
            workspace_path="/srv/other-project",
        )
        second_dir_path = kb.create_task(
            conn,
            title="run release",
            tenant="tenant-a",
            workspace_kind="dir",
            workspace_path="/srv/third-project",
        )

    minted = {
        base,
        other_title,
        other_tenant,
        under_parent_a,
        under_parent_b,
        other_workspace,
        second_dir_path,
    }
    assert len(minted) == 7, f"legitimate work collapsed: {sorted(minted)}"


def _mnf_concurrent_swarms_keep_their_own_verifier_and_synthesizer() -> None:
    """The over-fire that a title-only, board-wide key would cause.

    ``create_swarm`` defaults ``verifier_title="Verify swarm outputs"`` and
    ``synthesizer_title="Synthesize swarm outputs"``, so two concurrent swarms
    carry byte-identical titles for those cards. Collapsing them would leave
    swarm 2 with no verifier and no synthesizer of its own — structurally
    ungated work, silently. The parent-set component of the key is what keeps
    them apart; this fixture exists so the key cannot be quietly regressed to
    title-only.
    """
    from hermes_cli import kanban_swarm as ks

    with kb.connect() as conn:
        first = ks.create_swarm(
            conn,
            goal="First swarm goal",
            workers=[ks.parse_worker_arg("alpha:do the alpha slice")],
            verifier_assignee="verifier",
            synthesizer_assignee="synthesizer",
        )
        second = ks.create_swarm(
            conn,
            goal="Second swarm goal",
            workers=[ks.parse_worker_arg("beta:do the beta slice")],
            verifier_assignee="verifier",
            synthesizer_assignee="synthesizer",
        )
        titles = {
            kb.get_task(conn, first.verifier_id).title,
            kb.get_task(conn, second.verifier_id).title,
        }

    assert first.verifier_id != second.verifier_id, (
        "two concurrent swarms collapsed their verifier cards — swarm 2 is "
        "left structurally ungated"
    )
    assert first.synthesizer_id != second.synthesizer_id, (
        "two concurrent swarms collapsed their synthesizer cards"
    )
    assert first.root_id != second.root_id
    # The titles really are identical: the parent scope, not the title, is
    # what keeps these cards apart.
    assert titles == {"Verify swarm outputs"}, titles


def _mnf_allow_open_duplicate_opt_out_still_mints() -> None:
    """A fence that can refuse card creation needs a reachable escape hatch.

    Surfaced as ``hermes kanban create --allow-duplicate``. It relaxes the
    inferred title+scope fence only — never the explicit ``idempotency_key``
    contract the caller opted into.
    """
    with kb.connect() as conn:
        first = kb.create_task(conn, title="Recurring sweep", tenant="t")
        forced = kb.create_task(
            conn, title="Recurring sweep", tenant="t", allow_open_duplicate=True
        )

        keyed = kb.create_task(conn, title="Keyed job", idempotency_key="k-1")
        keyed_retry = kb.create_task(
            conn,
            title="Keyed job",
            idempotency_key="k-1",
            allow_open_duplicate=True,
        )

    assert forced != first, "allow_open_duplicate must still mint a second card"
    assert keyed_retry == keyed, (
        "allow_open_duplicate must not bypass an explicit idempotency_key"
    )


def _mnf_control_tag_prefixes_stay_distinct() -> None:
    """``[REVIEW] X`` and ``[LAND] X`` are different stages of the same work."""
    with kb.connect() as conn:
        review = kb.create_task(conn, title="[REVIEW] Fix the signing route", tenant="t")
        land = kb.create_task(conn, title="[LAND] Fix the signing route", tenant="t")
        plain = kb.create_task(conn, title="Fix the signing route", tenant="t")

    assert len({review, land, plain}) == 3, (
        "control-tag prefixes must stay significant: "
        f"review={review!r} land={land!r} plain={plain!r}"
    )


def _mnf_closed_cards_do_not_block_recreation() -> None:
    """Recurring work must remain re-creatable once the last card closed."""
    with kb.connect() as conn:
        completed = kb.create_task(conn, title="rerunnable closure", tenant="t")
        assert kb.complete_task(conn, completed)
        after_done = kb.create_task(conn, title="  RERUNNABLE   CLOSURE ", tenant="t")

        shelved = kb.create_task(conn, title="abandoned sweep", tenant="t")
        assert kb.archive_task(conn, shelved)
        after_archive = kb.create_task(conn, title="abandoned sweep", tenant="t")

    assert after_done != completed, "a done card must not block recreation"
    assert after_archive != shelved, "an archived card must not block recreation"


def _mnf_distinct_idempotency_keys_still_mint_distinct_work() -> None:
    """The key path must not become a second, broader duplicate filter."""
    with kb.connect() as conn:
        first = kb.create_task(conn, title="ingest permits", idempotency_key="k-1")
        second = kb.create_task(conn, title="ingest licences", idempotency_key="k-2")

    assert first != second, "distinct keys + distinct titles must mint two cards"


# ---------------------------------------------------------------------------
# FENCE: kanban.create.dedup.receipt  (PR #75)
#
# Every suppressed re-mint appends a ``create_deduplicated`` task_event to the
# surviving card. Without it a fence that fires is indistinguishable from a
# fence that never ran — which is exactly how this fence sat at 0 observed
# firings while nobody could tell whether it was installed.
# ---------------------------------------------------------------------------


def _ablate_dedup_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Suppression still happens; the durable receipt stops being written."""

    def _records_nothing(conn, **kwargs):
        return None

    monkeypatch.setattr(
        "hermes_cli.kanban_db._record_create_dedup",
        _records_nothing,
    )


def _mf_title_scope_suppression_leaves_a_receipt() -> None:
    with kb.connect() as conn:
        first = kb.create_task(
            conn,
            title="Drain the review column",
            created_by="drain-steward",
            tenant="tenant-a",
            idempotency_key="attempt-1",
        )
        repeated = kb.create_task(
            conn,
            title="drain the review column",
            created_by="flame-drain",
            tenant="tenant-a",
            idempotency_key="fresh-attempt-2",
        )
        events = kb.list_events(conn, first)

    assert repeated == first
    receipts = [event for event in events if event.kind == "create_deduplicated"]
    assert receipts, (
        "suppressed re-mint left no create_deduplicated receipt on "
        f"{first!r}; card timeline kinds = {[e.kind for e in events]}"
    )
    payload = receipts[-1].payload
    assert payload is not None
    assert payload["existing_task_id"] == first
    assert payload["reason"] == "normalized_title_scope"
    assert payload["normalized_title"] == "drain the review column"
    assert payload["attempted_by"] == "flame-drain"
    assert payload["scope"]["tenant"] == "tenant-a"


def _mf_idempotency_key_suppression_leaves_a_receipt() -> None:
    with kb.connect() as conn:
        first = kb.create_task(
            conn,
            title="Rebuild the showroom catalog",
            created_by="data-lane",
            idempotency_key="catalog-run-7",
        )
        repeated = kb.create_task(
            conn,
            title="A wholly different title on the same key",
            created_by="retry-lane",
            idempotency_key="catalog-run-7",
        )
        events = kb.list_events(conn, first)

    assert repeated == first
    receipts = [event for event in events if event.kind == "create_deduplicated"]
    assert receipts, (
        "idempotency-key hit left no create_deduplicated receipt on "
        f"{first!r}; card timeline kinds = {[e.kind for e in events]}"
    )
    payload = receipts[-1].payload
    assert payload is not None
    assert payload["reason"] == "idempotency_key"
    assert payload["idempotency_key_supplied"] is True


def _mnf_legitimate_creates_emit_no_receipt() -> None:
    with kb.connect() as conn:
        kb.create_task(conn, title="first distinct card", tenant="t")
        kb.create_task(conn, title="second distinct card", tenant="t")
        kb.create_task(conn, title="third distinct card", tenant="t")
        receipts = _dedup_events(conn)

    assert receipts == [], (
        f"fence over-fired on distinct work: {[e.payload for e in receipts]}"
    )


def _mnf_distinct_scopes_emit_no_receipt() -> None:
    with kb.connect() as conn:
        kb.create_task(conn, title="same title", tenant="tenant-a")
        kb.create_task(conn, title="same title", tenant="tenant-b")
        kb.create_task(
            conn,
            title="same title",
            tenant="tenant-a",
            workspace_kind="dir",
            workspace_path="/srv/elsewhere",
        )
        receipts = _dedup_events(conn)

    assert receipts == [], (
        f"fence over-fired across scopes: {[e.payload for e in receipts]}"
    )


# ---------------------------------------------------------------------------
# FENCES: kanban.decompose.*  (PR #76)
# ---------------------------------------------------------------------------


def _decompose_graph(
    conn: sqlite3.Connection,
    task_id: str,
    children: list[dict],
    *,
    key: str,
) -> list[str] | None:
    return kb.decompose_triage_task(
        conn,
        task_id,
        root_assignee="orchestrator",
        children=children,
        idempotency_key=key,
        author="fence-falsifier",
    )


def _ablate_decomposition_fanout_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove only the >15 enforcement; leave the published cap intact."""
    monkeypatch.setattr(
        "hermes_cli.kanban_db._enforce_decomposition_fanout_cap",
        lambda _task_id, _children: None,
    )


def _mf_sixteen_child_graph_is_rejected_atomically() -> None:
    assert kb.MAX_DECOMPOSITION_CHILDREN == 15
    with kb.connect() as conn:
        root = kb.create_task(conn, title="oversized fence graph", triage=True)
        children = [
            {"title": f"oversized child {index}", "assignee": "worker"}
            for index in range(kb.MAX_DECOMPOSITION_CHILDREN + 1)
        ]
        before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        rejected = False
        rejection = ""
        try:
            _decompose_graph(conn, root, children, key="fence:fanout:oversized")
        except ValueError as exc:
            rejected = True
            rejection = str(exc)
        after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        root_task = kb.get_task(conn, root)

    assert kb.MAX_DECOMPOSITION_CHILDREN == 15, "ablation removed the constant"
    assert rejected, "a 16-child decomposition bypassed the hard cap"
    assert "exceeds hard cap 15" in rejection
    assert after == before, f"rejected graph minted {after - before} task rows"
    assert root_task is not None and root_task.status == "triage"


def _mnf_graph_at_fifteen_child_cap_decomposes() -> None:
    assert kb.MAX_DECOMPOSITION_CHILDREN == 15
    with kb.connect() as conn:
        root = kb.create_task(conn, title="at-cap fence graph", triage=True)
        children = [
            {"title": f"at-cap child {index}", "assignee": "worker"}
            for index in range(kb.MAX_DECOMPOSITION_CHILDREN)
        ]
        child_ids = _decompose_graph(conn, root, children, key="fence:fanout:at-cap")
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

    assert child_ids is not None
    assert len(child_ids) == kb.MAX_DECOMPOSITION_CHILDREN
    assert task_count == kb.MAX_DECOMPOSITION_CHILDREN + 1


def _ablate_live_parent_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "hermes_cli.kanban_db._live_parent_chain",
        lambda _conn, _task_id: [],
    )


def _mf_live_ancestor_blocks_decomposition_without_minting() -> None:
    with kb.connect() as conn:
        ancestor = kb.create_task(conn, title="live fence ancestor")
        parent = kb.create_task(
            conn,
            title="live fence parent",
            parents=[ancestor],
        )
        root = kb.create_task(
            conn,
            title="live-chain guarded root",
            parents=[parent],
            triage=True,
        )
        eligible = kb.list_decomposition_eligible_triage_ids(conn)
        before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        result = _decompose_graph(
            conn,
            root,
            [{"title": "must not be minted", "assignee": "worker"}],
            key="fence:live-parent:blocked",
        )
        after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

    assert root not in eligible, "a root below a live ancestor was listed as eligible"
    assert result is None, f"a root below a live ancestor decomposed into {result}"
    assert after == before, f"blocked decomposition minted {after - before} task rows"


def _mnf_terminal_ancestor_chain_still_decomposes() -> None:
    with kb.connect() as conn:
        ancestor = kb.create_task(conn, title="done fence ancestor")
        assert kb.complete_task(conn, ancestor)
        parent = kb.create_task(
            conn,
            title="archived fence parent",
            parents=[ancestor],
        )
        assert kb.archive_task(conn, parent)
        root = kb.create_task(
            conn,
            title="terminal-chain eligible root",
            parents=[parent],
            triage=True,
        )
        assert root in kb.list_decomposition_eligible_triage_ids(conn)
        child_ids = _decompose_graph(
            conn,
            root,
            [{"title": "legitimate child", "assignee": "worker"}],
            key="fence:live-parent:terminal-chain",
        )

    assert child_ids is not None and len(child_ids) == 1


def _ablate_decomposition_idempotency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "hermes_cli.kanban_db.decomposition_result_for_key",
        lambda _conn, _task_id, _key: None,
    )


def _mf_same_decomposition_key_returns_original_ids() -> None:
    key = "fence:idempotency:stable-retry"
    with kb.connect() as conn:
        root = kb.create_task(conn, title="retry-deterministic root", triage=True)
        first = _decompose_graph(
            conn,
            root,
            [
                {"title": "original child one", "assignee": "worker"},
                {"title": "original child two", "assignee": "worker"},
            ],
            key=key,
        )
        assert first is not None
        count_after_first = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        second = _decompose_graph(
            conn,
            root,
            [{"title": "replacement child", "assignee": "worker"}],
            key=key,
        )
        count_after_retry = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

    assert second == first, (
        f"same-key retry returned {second!r}, not original child ids {first!r}"
    )
    assert count_after_retry == count_after_first


def _mnf_different_key_on_eligible_root_decomposes() -> None:
    with kb.connect() as conn:
        first_root = kb.create_task(conn, title="first keyed root", triage=True)
        first = _decompose_graph(
            conn,
            first_root,
            [{"title": "first keyed child", "assignee": "worker"}],
            key="fence:idempotency:first",
        )
        assert first is not None

        eligible_root = kb.create_task(conn, title="different keyed root", triage=True)
        assert eligible_root in kb.list_decomposition_eligible_triage_ids(conn)
        second = _decompose_graph(
            conn,
            eligible_root,
            [{"title": "different keyed child", "assignee": "worker"}],
            key="fence:idempotency:different",
        )

    assert second is not None and len(second) == 1
    assert second != first


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def _board_snapshot(live_session_ids: tuple[str, ...]) -> cold.BoardLivenessSnapshot:
    identity = Path(".").stat()
    return cold.BoardLivenessSnapshot(
        board_path=Path("."),
        board_identity=identity,
        tasks_total=1,
        linked_tasks=len(live_session_ids),
        live_linked_tasks=len(live_session_ids),
        live_session_ids=live_session_ids,
        task_projection_sha256="0" * 64,
    )


def _ablate_live_board_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "hermes_cli.session_cold_archive._require_no_live_board_selection",
        lambda _snapshot, _selected_ids: None,
    )


def _mf_live_board_session_is_not_deleted() -> None:
    fired = False
    try:
        cold._require_no_live_board_selection(
            _board_snapshot(("session-live",)), ["session-live"]
        )
    except cold.ColdArchiveError:
        fired = True
    assert fired, "live board lineage was accepted for destructive retention"


def _mnf_terminal_or_unlinked_board_session_is_allowed() -> None:
    cold._require_no_live_board_selection(_board_snapshot(()), ["session-cold"])


def _ablate_candidate_filesystem_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "hermes_cli.session_cold_archive._require_candidate_filesystem_boundary",
        lambda _source: None,
    )


def _mf_same_filesystem_candidate_is_refused() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        source = Path(temp_dir) / "candidate.db"
        source.write_bytes(b"offline")
        source_device = source.stat().st_dev
        fired = False
        with mock.patch.object(
            cold,
            "_protected_live_filesystem_devices",
            return_value=frozenset({source_device}),
        ):
            try:
                cold._require_candidate_filesystem_boundary(source)
            except cold.ColdArchiveError:
                fired = True
        assert fired, "same-filesystem candidate passed the alias/race boundary"


def _mnf_separate_filesystem_candidate_is_allowed() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        source = Path(temp_dir) / "candidate.db"
        source.write_bytes(b"offline")
        with mock.patch.object(
            cold,
            "_protected_live_filesystem_devices",
            return_value=frozenset({source.stat().st_dev + 1}),
        ):
            cold._require_candidate_filesystem_boundary(source)


_COLD_NOW = 1_800_000_000.0


class _ColdFenceRclone:
    def __init__(self) -> None:
        self.remote: dict[str, bytes] = {}

    def __call__(self, command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        verb, source, destination = command[1:4]
        if verb == "copy":
            remote = destination.rstrip("/") + "/" + Path(source).name
            self.remote[remote] = Path(source).read_bytes()
        elif verb == "check":
            name = command[command.index("--include") + 1][1:]
            expected = (Path(source) / name).read_bytes()
            remote = destination.rstrip("/") + "/" + name
            if self.remote.get(remote) != expected:
                return subprocess.CompletedProcess(command, 1, "", "mismatch")
        elif verb == "copyto":
            Path(destination).write_bytes(self.remote.get(source, b""))
        else:  # pragma: no cover - a command-shape regression is a harness bug
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, 0, "", "")


def _cold_fence_age(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
    source = Path(command[-1])
    output = Path(command[command.index("-o") + 1])
    output.write_bytes(b"AGE-FENCE\n" + hashlib.sha256(source.read_bytes()).digest())
    os.chmod(output, 0o600)
    return subprocess.CompletedProcess(command, 0, "", "")


def _cold_fence_setup(root: Path) -> tuple[Path, Path, Path, dict[str, object], _ColdFenceRclone]:
    candidate = root / "candidate.db"
    db = SessionDB(db_path=candidate)
    try:
        db.create_session(session_id="eligible", source="cli")
        db.append_message(
            "eligible", "user", "must-fire-cold-archive", timestamp=_COLD_NOW - 90 * 86400
        )
        assert db._conn is not None
        db._conn.execute(
            """UPDATE sessions SET started_at=?, ended_at=?, archived=1, pinned=0,
               last_activity_at=? WHERE id='eligible'""",
            (_COLD_NOW - 90 * 86400 - 1, _COLD_NOW - 90 * 86400, _COLD_NOW - 90 * 86400),
        )
        db._conn.commit()
    finally:
        db.close()
    board = root / "fleet.board.db"
    conn = sqlite3.connect(board)
    try:
        conn.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL, session_id TEXT)"
        )
        conn.execute("INSERT INTO tasks VALUES ('control', 'done', NULL)")
        conn.commit()
    finally:
        conn.close()
    recipient = root / "recipient.txt"
    recipient.write_text("age1fencefixture", encoding="utf-8")
    config = root / "rclone.conf"
    config.write_text("[archive]\ntype = local\n", encoding="utf-8")
    os.chmod(config, 0o600)
    stage = root / "stage"
    remote = _ColdFenceRclone()
    with (
        mock.patch.object(cold, "_AUTHORITATIVE_FLEET_BOARD_PATH", board),
        mock.patch.object(
            cold,
            "_protected_live_filesystem_devices",
            return_value=frozenset({candidate.stat().st_dev + 1}),
        ),
    ):
        producer = cold.run_cold_archive_pass(
            source_db=candidate,
            stage_root=stage,
            board_db=board,
            now=_COLD_NOW,
            hold_sources=[],
            rclone_remote=f"archive:{root / 'remote'}",
            rclone_config=config,
            age_recipient_file=recipient,
            age_runner=_cold_fence_age,
            rclone_runner=remote,
        )
    approved_manifest = root / "approved-manifest.json"
    approved_manifest.write_bytes((stage / "GATE-B-MANIFEST.json").read_bytes())
    os.chmod(approved_manifest, 0o400)
    approved_producer = root / "approved-producer.json"
    approved_producer.write_bytes(
        (stage / "COLD-ARCHIVE-PRODUCER-RECEIPT.json").read_bytes()
    )
    os.chmod(approved_producer, 0o400)
    producer["_board"] = board
    producer["_approved_manifest"] = approved_manifest
    producer["_approved_producer"] = approved_producer
    return candidate, stage, config, producer, remote


def _cold_fence_apply(
    candidate: Path,
    stage: Path,
    config: Path,
    producer: dict[str, object],
    remote: _ColdFenceRclone,
) -> dict[str, object]:
    board = Path(str(producer["_board"]))
    with (
        mock.patch.object(cold, "_AUTHORITATIVE_FLEET_BOARD_PATH", board),
        mock.patch.object(
            cold,
            "_protected_live_filesystem_devices",
            return_value=frozenset({candidate.stat().st_dev + 1}),
        ),
    ):
        return cold.run_cold_archive_pass(
            source_db=candidate,
            stage_root=stage,
            board_db=board,
            apply_retention=True,
            approved_manifest_path=Path(str(producer["_approved_manifest"])),
            approved_manifest_sha256=str(producer["manifest_file_sha256"]),
            approved_producer_receipt_path=Path(str(producer["_approved_producer"])),
            approved_producer_receipt_sha256=str(producer["receipt_sha256"]),
            rclone_config=config,
            rclone_runner=remote,
        )


def _ablate_cold_board_integration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cold, "_require_no_live_board_selection", lambda *_args: None)
    monkeypatch.setattr(cold, "_require_board_matches_manifest", lambda *_args: None)


def _mf_cold_full_apply_blocks_late_live_board_reference() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        candidate, stage, config, producer, remote = _cold_fence_setup(Path(temp_dir))
        conn = sqlite3.connect(Path(str(producer["_board"])))
        try:
            conn.execute("INSERT INTO tasks VALUES ('late', 'ready', 'eligible')")
            conn.commit()
        finally:
            conn.close()
        fired = False
        try:
            _cold_fence_apply(candidate, stage, config, producer, remote)
        except cold.ColdArchiveError:
            fired = True
        assert fired, "full apply deleted a session referenced by the live board"


def _mnf_cold_full_apply_accepts_unchanged_terminal_board() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        candidate, stage, config, producer, remote = _cold_fence_setup(Path(temp_dir))
        receipt = _cold_fence_apply(candidate, stage, config, producer, remote)
        assert receipt["retention"]["applied"] is True


def _ablate_cold_board_reservation(monkeypatch: pytest.MonkeyPatch) -> None:
    original = cold._board_liveness_snapshot

    @contextmanager
    def no_reservation(board_db: Path, *, reserve_writes: bool):
        del reserve_writes
        with original(board_db, reserve_writes=False) as snapshot:
            yield snapshot

    monkeypatch.setattr(cold, "_board_liveness_snapshot", no_reservation)


def _mf_cold_full_apply_holds_board_reservation_to_commit() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        candidate, stage, config, producer, remote = _cold_fence_setup(Path(temp_dir))
        original_write = cold._exclusive_write_json
        observations: list[bool] = []

        def probe(path: Path, payload: dict[str, object]) -> Path:
            if path.name == "COLD-ARCHIVE-APPLY-PREPARED.json":
                contender = sqlite3.connect(
                    Path(str(producer["_board"])), timeout=0, isolation_level=None
                )
                try:
                    try:
                        contender.execute("BEGIN IMMEDIATE")
                    except sqlite3.OperationalError:
                        observations.append(True)
                    else:
                        observations.append(False)
                        contender.execute("ROLLBACK")
                finally:
                    contender.close()
            return original_write(path, payload)

        with mock.patch.object(cold, "_exclusive_write_json", probe):
            receipt = _cold_fence_apply(candidate, stage, config, producer, remote)
        assert receipt["retention"]["applied"] is True
        assert observations == [True], "apply did not hold the board writer reservation"


def _ablate_cold_post_commit_reopen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cold,
        "_verify_post_commit_candidate",
        lambda *_args: {"verified": True},
    )


def _mf_cold_full_apply_reopens_committed_namespace() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        candidate, stage, config, producer, remote = _cold_fence_setup(root)
        pre_commit = root / "pre-commit.db"
        shutil.copy2(candidate, pre_commit)
        original_connect = cold._connect_candidate
        first = True

        class SwapAfterCommit:
            def __init__(self, wrapped: sqlite3.Connection) -> None:
                self.wrapped = wrapped

            def __getattr__(self, name: str) -> object:
                return getattr(self.wrapped, name)

            def execute(self, sql: str, *args: object) -> object:
                result = self.wrapped.execute(sql, *args)
                if sql == "COMMIT":
                    candidate.rename(root / "committed.db")
                    shutil.copy2(pre_commit, candidate)
                return result

        def connect(path: Path) -> object:
            nonlocal first
            wrapped = original_connect(path)
            if first:
                first = False
                return SwapAfterCommit(wrapped)
            return wrapped

        fired = False
        with mock.patch.object(cold, "_connect_candidate", connect):
            try:
                _cold_fence_apply(candidate, stage, config, producer, remote)
            except cold.ColdArchiveError:
                fired = True
        assert fired, "full apply emitted success after a commit-adjacent path swap"

FENCES: tuple[FenceSpec, ...] = (
    FenceSpec(
        fence_id="kanban.create.dedup.title_scope",
        origin="PR #75",
        mechanism=(
            "create_task() consults _find_open_title_scope_duplicate() inside the "
            "same write transaction as the insert and returns the existing open "
            "card instead of minting a twin."
        ),
        ablation=Ablation(
            describes="_find_open_title_scope_duplicate() never reports a duplicate",
            targets=("hermes_cli.kanban_db._find_open_title_scope_duplicate",),
            apply=_ablate_title_scope_lookup,
        ),
        must_fire={
            "equivalent_open_card_is_not_re_minted": (
                _mf_equivalent_open_card_is_not_re_minted
            ),
            "unicode_and_whitespace_variants_fold_to_one_card": (
                _mf_unicode_and_whitespace_variants_fold_to_one_card
            ),
            "non_ready_open_card_still_blocks_re_mint": (
                _mf_non_ready_open_card_still_blocks_re_mint
            ),
            "re_mint_blocked_after_dispatcher_rewrites_workspace_path": (
                _mf_re_mint_blocked_after_dispatcher_rewrites_workspace_path
            ),
            "re_mint_blocked_after_worktree_path_rewrite": (
                _mf_re_mint_blocked_after_worktree_path_rewrite
            ),
        },
        must_not_fire={
            "distinct_work_all_mints": _mnf_distinct_work_all_mints,
            "control_tag_prefixes_stay_distinct": _mnf_control_tag_prefixes_stay_distinct,
            "closed_cards_do_not_block_recreation": (
                _mnf_closed_cards_do_not_block_recreation
            ),
            "distinct_idempotency_keys_still_mint_distinct_work": (
                _mnf_distinct_idempotency_keys_still_mint_distinct_work
            ),
            "concurrent_swarms_keep_their_own_verifier_and_synthesizer": (
                _mnf_concurrent_swarms_keep_their_own_verifier_and_synthesizer
            ),
            "allow_open_duplicate_opt_out_still_mints": (
                _mnf_allow_open_duplicate_opt_out_still_mints
            ),
        },
    ),
    FenceSpec(
        fence_id="kanban.create.dedup.receipt",
        origin="PR #75",
        mechanism=(
            "_record_create_dedup() appends a create_deduplicated task_event to "
            "the surviving card for both the idempotency-key and the "
            "normalized-title+scope suppression paths."
        ),
        ablation=Ablation(
            describes="_record_create_dedup() writes no receipt",
            targets=("hermes_cli.kanban_db._record_create_dedup",),
            apply=_ablate_dedup_receipt,
        ),
        must_fire={
            "title_scope_suppression_leaves_a_receipt": (
                _mf_title_scope_suppression_leaves_a_receipt
            ),
            "idempotency_key_suppression_leaves_a_receipt": (
                _mf_idempotency_key_suppression_leaves_a_receipt
            ),
        },
        must_not_fire={
            "legitimate_creates_emit_no_receipt": _mnf_legitimate_creates_emit_no_receipt,
            "distinct_scopes_emit_no_receipt": _mnf_distinct_scopes_emit_no_receipt,
        },
    ),
    FenceSpec(
        fence_id="kanban.decompose.fanout_cap",
        origin="PR #76",
        mechanism=(
            "decompose_triage_task() calls _enforce_decomposition_fanout_cap(), "
            "which rejects more than MAX_DECOMPOSITION_CHILDREN (15) before "
            "the write transaction can mint rows."
        ),
        ablation=Ablation(
            describes="the >15 enforcement is removed while the cap constant stays 15",
            targets=(
                "hermes_cli.kanban_db._enforce_decomposition_fanout_cap",
            ),
            apply=_ablate_decomposition_fanout_cap,
        ),
        must_fire={
            "sixteen_child_graph_is_rejected_atomically": (
                _mf_sixteen_child_graph_is_rejected_atomically
            )
        },
        must_not_fire={
            "graph_at_fifteen_child_cap_decomposes": (
                _mnf_graph_at_fifteen_child_cap_decomposes
            )
        },
    ),
    FenceSpec(
        fence_id="kanban.decompose.live_parent_chain",
        origin="PR #76",
        mechanism=(
            "_live_parent_chain() feeds decomposition_hold_reason(), while the "
            "eligible-triage query independently filters has_live_parent_chain; "
            "both paths keep descendants of live work whole."
        ),
        ablation=Ablation(
            describes="_live_parent_chain() reports no live ancestors",
            targets=("hermes_cli.kanban_db._live_parent_chain",),
            apply=_ablate_live_parent_chain,
        ),
        must_fire={
            "live_ancestor_blocks_decomposition_without_minting": (
                _mf_live_ancestor_blocks_decomposition_without_minting
            )
        },
        must_not_fire={
            "terminal_ancestor_chain_still_decomposes": (
                _mnf_terminal_ancestor_chain_still_decomposes
            )
        },
    ),
    FenceSpec(
        fence_id="kanban.decompose.idempotency",
        origin="PR #76",
        mechanism=(
            "decomposition_result_for_key() returns the original child-id list "
            "for a repeated key, preserving deterministic retry results."
        ),
        ablation=Ablation(
            describes="decomposition_result_for_key() always misses prior results",
            targets=("hermes_cli.kanban_db.decomposition_result_for_key",),
            apply=_ablate_decomposition_idempotency,
        ),
        must_fire={
            "same_key_returns_original_ids": (
                _mf_same_decomposition_key_returns_original_ids
            )
        },
        must_not_fire={
            "different_key_on_eligible_root_decomposes": (
                _mnf_different_key_on_eligible_root_decomposes
            )
        },
    ),
    FenceSpec(
        fence_id="sessions.cold_archive.live_board_liveness",
        origin="replacement for PR #100",
        mechanism=(
            "apply rechecks the selected session set against a fail-closed board "
            "liveness snapshot held under a writer reservation through commit"
        ),
        ablation=Ablation(
            describes="live board overlap is accepted",
            targets=(
                "hermes_cli.session_cold_archive._require_no_live_board_selection",
            ),
            apply=_ablate_live_board_selection,
        ),
        must_fire={
            "live_board_session_is_not_deleted": _mf_live_board_session_is_not_deleted
        },
        must_not_fire={
            "terminal_or_unlinked_board_session_is_allowed": (
                _mnf_terminal_or_unlinked_board_session_is_allowed
            )
        },
    ),
    FenceSpec(
        fence_id="sessions.cold_archive.filesystem_boundary",
        origin="replacement for PR #100",
        mechanism=(
            "an offline candidate must be on a different filesystem from every "
            "protected live profile, eliminating post-check hardlink alias creation"
        ),
        ablation=Ablation(
            describes="candidate filesystem separation is not enforced",
            targets=(
                "hermes_cli.session_cold_archive._require_candidate_filesystem_boundary",
            ),
            apply=_ablate_candidate_filesystem_boundary,
        ),
        must_fire={
            "same_filesystem_candidate_is_refused": _mf_same_filesystem_candidate_is_refused
        },
        must_not_fire={
            "separate_filesystem_candidate_is_allowed": (
                _mnf_separate_filesystem_candidate_is_allowed
            )
        },
    ),
    FenceSpec(
        fence_id="sessions.cold_archive.full_apply_board_authority",
        origin="PR #109 exact-head repair",
        mechanism=(
            "the disposable producer-to-apply path binds the canonical board "
            "identity/projection and rejects a selected live-board lineage"
        ),
        ablation=Ablation(
            describes="board authority binding and selected-live overlap both accept",
            targets=(
                "hermes_cli.session_cold_archive._require_no_live_board_selection",
                "hermes_cli.session_cold_archive._require_board_matches_manifest",
            ),
            apply=_ablate_cold_board_integration,
        ),
        must_fire={
            "full_apply_blocks_late_live_reference": (
                _mf_cold_full_apply_blocks_late_live_board_reference
            )
        },
        must_not_fire={
            "full_apply_accepts_unchanged_terminal_board": (
                _mnf_cold_full_apply_accepts_unchanged_terminal_board
            )
        },
    ),
    FenceSpec(
        fence_id="sessions.cold_archive.full_apply_board_reservation",
        origin="PR #109 exact-head repair",
        mechanism="the real apply path holds BEGIN IMMEDIATE on the board through commit",
        ablation=Ablation(
            describes="the board snapshot always uses a read transaction",
            targets=("hermes_cli.session_cold_archive._board_liveness_snapshot",),
            apply=_ablate_cold_board_reservation,
        ),
        must_fire={
            "full_apply_holds_writer_reservation": (
                _mf_cold_full_apply_holds_board_reservation_to_commit
            )
        },
        must_not_fire={
            "full_apply_still_accepts_unchanged_board": (
                _mnf_cold_full_apply_accepts_unchanged_terminal_board
            )
        },
    ),
    FenceSpec(
        fence_id="sessions.cold_archive.full_apply_post_commit_reopen",
        origin="PR #109 exact-head repair",
        mechanism=(
            "success requires reopening the committed candidate pathname and "
            "revalidating identity, logical state, integrity, FTS, and selection"
        ),
        ablation=Ablation(
            describes="post-COMMIT reopen returns an unconditional success token",
            targets=("hermes_cli.session_cold_archive._verify_post_commit_candidate",),
            apply=_ablate_cold_post_commit_reopen,
        ),
        must_fire={
            "full_apply_rejects_commit_adjacent_namespace_swap": (
                _mf_cold_full_apply_reopens_committed_namespace
            )
        },
        must_not_fire={
            "full_apply_accepts_stable_committed_namespace": (
                _mnf_cold_full_apply_accepts_unchanged_terminal_board
            )
        },
    ),
)


def _cases(kind: str) -> list:
    return [
        pytest.param(fence, name, id=f"{fence.fence_id}::{name}")
        for fence in FENCES
        for name in getattr(fence, kind)
    ]


MUST_FIRE_CASES = _cases("must_fire")
MUST_NOT_FIRE_CASES = _cases("must_not_fire")


# ---------------------------------------------------------------------------
# Driver: the 2x2 matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("fence", "case"), MUST_FIRE_CASES)
def test_must_fire_fixture_is_green_with_fence_intact(board, fence, case):
    """Top-left cell: the fence is in the tree and the fixture agrees."""
    fence.must_fire[case]()


@pytest.mark.parametrize(("fence", "case"), MUST_FIRE_CASES)
def test_must_fire_fixture_goes_red_when_fence_is_removed(
    board, fence, case, monkeypatch
):
    """Top-right cell: with the fence ablated the fixture MUST fail.

    A pass here means the fixture never depended on the fence — it is
    decoration, and deleting the fence would ship green.
    """
    fence.ablation.apply(monkeypatch)
    try:
        fence.must_fire[case]()
    except AssertionError:
        return
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        pytest.fail(
            f"HARNESS BUG: must-fire fixture {fence.fence_id}::{case} raised "
            f"{type(exc).__name__}({exc}) under ablation "
            f"[{fence.ablation.describes}] instead of failing an assertion. "
            "The fixture stopped exercising the mechanism rather than "
            "detecting its absence."
        )
    pytest.fail(
        f"VACUOUS MUST-FIRE FIXTURE: {fence.fence_id}::{case} still passes with "
        f"the fence ablated [{fence.ablation.describes}]. The fixture does not "
        f"prove the fence ({fence.origin}) is present; deleting the fence would "
        "ship green. Fix the fixture or the ablation target."
    )


@pytest.mark.parametrize(("fence", "case"), MUST_NOT_FIRE_CASES)
def test_must_not_fire_fixture_is_green_with_fence_intact(board, fence, case):
    """Bottom-left cell: the fence does not touch legitimate work."""
    fence.must_not_fire[case]()


@pytest.mark.parametrize(("fence", "case"), MUST_NOT_FIRE_CASES)
def test_must_not_fire_fixture_stays_green_when_fence_is_removed(
    board, fence, case, monkeypatch
):
    """Bottom-right cell: the negative describes the legitimate path only.

    If this goes red the "negative" was really testing the fence, so it is a
    mislabelled must-fire fixture and provides no over-firing protection.
    """
    fence.ablation.apply(monkeypatch)
    try:
        fence.must_not_fire[case]()
    except AssertionError as exc:
        pytest.fail(
            f"MISLABELLED FIXTURE: must-not-fire {fence.fence_id}::{case} failed "
            f"with the fence ablated [{fence.ablation.describes}]: {exc}. A "
            "must-not-fire fixture must describe legitimate work that passes "
            "with or without the fence; this one depends on the fence and "
            "therefore guards nothing against over-firing."
        )


# ---------------------------------------------------------------------------
# Meta: the registry itself cannot rot
# ---------------------------------------------------------------------------


def test_every_registered_fence_covers_both_directions():
    missing = [
        fence.fence_id
        for fence in FENCES
        if not fence.must_fire or not fence.must_not_fire
    ]
    assert missing == [], (
        f"fences registered without both-directions coverage: {missing}"
    )


def test_fence_ids_are_unique():
    ids = [fence.fence_id for fence in FENCES]
    assert len(ids) == len(set(ids)), f"duplicate fence ids in registry: {ids}"


@pytest.mark.parametrize(
    ("fence_id", "target"),
    [
        pytest.param(fence.fence_id, target, id=f"{fence.fence_id}::{target}")
        for fence in FENCES
        for target in fence.ablation.targets
    ],
)
def test_ablation_targets_still_exist(fence_id, target):
    """A renamed/removed mechanism must break the harness, not slip past it."""
    try:
        resolved = _resolve(target)
    except (ImportError, AttributeError) as exc:
        pytest.fail(
            f"STALE ABLATION TARGET for {fence_id}: {target} no longer resolves "
            f"({type(exc).__name__}: {exc}). The fence moved or was deleted — "
            "re-point the ablation at the mechanism that replaced it, or drop "
            "the fence from the registry deliberately."
        )
    assert callable(resolved), f"{target} is not callable"
