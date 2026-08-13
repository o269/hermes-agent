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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

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


def _mnf_distinct_work_all_mints() -> None:
    """Different title, tenant, parent set, or workspace is different work."""
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

    minted = {
        base,
        other_title,
        other_tenant,
        under_parent_a,
        under_parent_b,
        other_workspace,
    }
    assert len(minted) == 6, f"legitimate work collapsed: {sorted(minted)}"


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
# Registry
# ---------------------------------------------------------------------------

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
