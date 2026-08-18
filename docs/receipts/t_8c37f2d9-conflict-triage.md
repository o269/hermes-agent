# CARD: t_8c37f2d9

Hermes-agent conflict pile triage. Do not merge this receipt; it is the Step 1 artifact.

- Repo: `o269/hermes-agent` (fork only)
- Current main at triage: `7ca0e5e5b`
- Method: unique tip commits vs `origin/main`, not GitHub's 30k–139k-file stale diffs
- Probe: `git log --grep=boardd` hit known commits; `git log --grep=ZZZ_NO_SUCH_STRING_t_8c37f2d9` returned empty
- Probe: `rg worker_session` in `tools/kanban_tools.py` hit; `rg ZZZ_NOSUCH_t_8c37f2d9` in `hermes_cli/kanban_db.py` returned empty
- Fleet heartbeat: `hermes kanban --board fleet comment t_8c37f2d9` failed (`no such task`). Card is not on the live fleet board.

## Why these PRs "conflict"

Almost every listed PR is an old fork stack whose merge-base still contains already-landed SHAs (`#11` `#19` `#27` `#37` `#38` `#40` `#51` `#52` `#54` `#58` `#65`, …). GitHub therefore reports `CONFLICTING` with 67–1567 files. The **unique intent** is usually 1–5 tip commits and a handful of files.

Do **not** rebase the 17 branches.

## Step 1 table

| PR | Unique tip (what it actually does) | Still real on current main? | Overlaps | Recommendation |
|---:|---|---|---|---|
| 61 | Tip `06981f4da`: board regeneration / create-dedup + decompose idempotency + `MAX_DECOMPOSITION_CHILDREN=15` (15 files). The 100-file GitHub diff is stale stack. | Core intent already has current-main vehicles: **#75** (refuse equivalent open-card re-mints, MERGEABLE) and **#76** (bound/dedupe decompositions, MERGEABLE). | #75, #76 | **CLOSE as obsolete** (superseded by #75+#76). Do not rebase. |
| 60 | Tip `cf57ddcb6`: idle+dead read-conn sweeper in `hermes_state.py` (FD leak p87). 1567-file GitHub diff is upstream-sync noise. | Dead-thread prune (`_prune_dead_read_conns_unlocked`) **is already on main**. Idle timeout + daemon sweeper is **#77** (MERGEABLE, same 2 files, current main). | **#77** | **CLOSE as obsolete** (superseded by #77). |
| 59 | Tip `d83653d83`: fold crash/timeout failure accounting into the reclaim `write_txn` so a successor claim cannot race the post-loop `_record_task_failure`. | **YES.** `enforce_max_runtime` still increments failures *outside* the reclaim txn (`hermes_cli/kanban_db.py` ~8348). `crash_details` still collected for a second txn. | none of the 17 | **consolidate** → new current-main PR (crash-accounting cluster). |
| 56 | Tip `a040489b2`: isolate dashboard boardd e2e (`tests/test_dashboard_boardd_e2e.py` imports `scripts/fleet/boardd.py`). | **NO.** `scripts/fleet/boardd.py` is not in current main. | #29 | **CLOSE as obsolete**. |
| 53 | 5-commit RSS fence series (`conversation_compression.py`, sidecar/rotation). | **Not in this pile.** GitHub-merged #42 telemetry files are also **absent** from current main (upstream rewrite). This is compression, not kanban/boardd. Stale stack would fight `kanban_db.py` for no reason. | none of the 17 | **CLOSE as obsolete** for this pile. Re-port only under a dedicated compression card if RSS is still live. |
| 50 | Tip `0f61bece0`: `resolve_max_spawn_ceiling`, default 16, fail-closed invalid parse, `HERMES_KANBAN_MAX_SPAWN`. | **YES.** `dispatch_once(max_spawn=None)` still means unlimited. No `DEFAULT_MAX_SPAWN` / `resolve_max_spawn_ceiling` on main. Config default is unset. | dispatch cluster (#47/#48) | **consolidate** → new current-main PR (spawn-ceiling cluster). |
| 49 | Tip `a44316c15`: refuse `gh pr` against `NousResearch/hermes-agent`; force `o269/hermes-agent`. | **YES.** No `github_pr_destination_guard.py` on main. Update/banner still point at upstream. | none of the 17 | **consolidate** → new current-main PR (destination-guard cluster). Not kanban_db. |
| 48 | Tips `4b9350763` + `fea3b072f`: fail-closed `_assignee_has_spawn_target` — never spawn `default` / unresolvable / missing profile registry. | **YES.** Current `_assignee_has_spawn_target` still `return True` if `profiles` cannot be imported. `assignee=default` still passes `profile_exists`. Default-assignee import failure still fail-opens (`_default_assignee_resolved = True`). | #39 (roster), #11 (landed) | **consolidate** → new current-main PR (spawn-policy cluster). |
| 47 | Tip `ea5285c5d`: `hermes kanban dispatch --task-id` fail-closed exact target (no generic fallback). | **YES.** `dispatch_once` has no `task_ids`. CLI dispatch has no `--task-id`. | #50 (same dispatch path) | **consolidate** → own current-main PR (targeted-dispatch cluster). Keep separate from #50/#48 so the conflict surface stays small. |
| 45 | Tips `7a86e5876` + rework + Gate-B/O5: `session_cold_archive.py` (~2k lines). | Feature **absent** from current main even though GitHub lists #52 as merged — `hermes_cli/session_cold_archive.py` is not in `origin/main` history (upstream rewrite dropped it). Archival is operator-gated (`t_30ab9f7a`). | GitHub-#52 (not on tree) | **CLOSE as obsolete**. Do **not** revive this stale stack. Cold-archive needs a fresh operator-gated card, not a conflict rebase. |
| 39 | Tips `8f0d0ed15` (+ already-landed terminal-custody commit): live roster / de-roster prefixes / `kanban_assignment_policy.py`. | Terminal custody **already on main** (`_is_terminal_custody_row`, landed #38). Roster module does not exist. Unique remainder is fleet de-roster policy on a 10-file stale stack. | #48, #11 | **CLOSE as obsolete**. Do not revive. If de-roster prefixes are still required, new small card. |
| 33 | Tip `5c5bafd1b`: do not rmtree a **running** parent's scratch when the last child goes terminal; also crash-log tails / launch-failure window. | **YES — the live-parent rmtree bug is still on main.** `_try_cleanup_parent_workspaces` still selects only `workspace_kind, workspace_path` and deletes when children are terminal, ignoring parent status and worker liveness. #65 is a different fix (host-aware PID reclaim). | #81 (tmp reaper; different), history of live-workspace deletion | **consolidate** → new current-main PR (workspace-reap cluster). Launch-log/oracle leftover stays out to keep the PR small. |
| 32 | Tips `3140c748f` + `671160623`: `authority_revision` CAS + defer triggers on partial schemas. | **Schema change** (`authority_revision` triggers). Current main has `assignment_generation` resume CAS, not this comment/link trigger CAS. Stale stack also edits missing `boardd.py`. | later authority-lane lands (#11/#23/#69) | **CLOSE as obsolete**. Do not add schema/triggers from this pile. |
| 29 | Tip `0ecfa43f8`: harden boardd restart schema tests against `/opt/hermes-boardd/.../boardd.py`. | **NO.** `tests/test_boardd_runtime.py` and `scripts/fleet/boardd.py` are not on current main. #27 (restart-safe) already merged. | #56, #27 | **CLOSE as obsolete**. |
| 28 | Tip `f002eff46`: `boardd-backup-monitor.py` + systemd units. | Not installed (timers present are traverse-guard / intruder-watch). `boardd.py` not in repo. Ops-only, not a kanban_db conflict. | none of the 17 | **CLOSE as obsolete** for this pile. Fresh ops card if backup coverage is still missing. |
| 13 | Tip `bb14a3cac`: TUI `_soft_park_session_after_grace` (lease release, lazy worker recreate). | Soft-park **functions are not on main**. Current `tui_gateway/server.py` already parks/reaps disconnects differently. 2500-line stale TUI patch would conflict with current disconnect paths and is not the kanban pile. | none of the 17 | **CLOSE as obsolete** for this pile. Dedicated TUI card if Symptom still reproduces. |
| 71 | Tips `1ddc18053` + `34d8f90a4`: add `tasks.worker_session_id` (schema) + fail-closed claim-lock CAS on turnover. | **YES, and already MERGEABLE.** Column is **not** on main (`kanban_db.py` has no `worker_session_id`; only `tools/kanban_tools.py` stamps a dict key). Desktop sibling is **#72** (MERGEABLE, desktop-only). | #72 (UI only) | **LAND as-is** (already mergeable). **SCHEMA CHANGE** — `tasks.worker_session_id`. Do not rewrite this branch. |

## Step 2 — what survives (small clusters)

Do **not** produce one giant PR.

| New vehicle | Supersedes | Module | Schema? |
|---|---|---|---|
| Existing **#71** (already mergeable) | #71 | kanban_db + run_agent | **YES** `worker_session_id` |
| Existing **#77** (already mergeable; not in the 17) | #60 | hermes_state | no |
| Existing **#75** + **#76** (already mergeable; not in the 17) | #61 | kanban create/decompose | no |
| **New** workspace-reap PR | #33 | `hermes_cli/kanban_db.py` `_try_cleanup_parent_workspaces` | no |
| **New** spawn-policy PR | #48 | `_assignee_has_spawn_target` / default assignee | no |
| **New** spawn-ceiling PR | #50 | `resolve_max_spawn_ceiling` | no |
| **New** crash-accounting PR | #59 | `enforce_max_runtime` / `detect_crashed_workers` | no |
| **New** targeted-dispatch PR | #47 | `dispatch_once(task_ids=...)` | no |
| **New** destination-guard PR | #49 | `tools/github_pr_destination_guard.py` | no |

Close without replacement: **#61 #60 #56 #53 #45 #39 #32 #29 #28 #13**.

## Safety notes

- Fail-closed on workspace deletion: a running parent, or a terminal parent that still has `worker_pid` set, must keep its scratch dir.
- #71 is a schema add (`worker_session_id TEXT` + migrate). Call it out before land.
- Do not import #32/#45 schema or trigger changes through this card.
- Do not rebase the 17 stale branches.

## Checks used (actual)

```
git log origin/main --oneline --grep=boardd   # hits (positive)
git log origin/main --oneline --grep=ZZZ_NO_SUCH_STRING_t_8c37f2d9   # empty (negative)
rg worker_session tools/kanban_tools.py   # hits (positive)
rg ZZZ_NOSUCH_t_8c37f2d9 hermes_cli/kanban_db.py   # empty (negative)
gh pr list --repo o269/hermes-agent --state merged   # #52/#42 listed merged
git log origin/main -- hermes_cli/session_cold_archive.py   # empty — #52 not on tree
ls scripts/fleet/boardd.py   # missing
```

## DONE REPORT

CARD: t_8c37f2d9
STATUS: STEP-1 TABLE FILED; KEEPER PRS FOLLOW
BRANCH: docs/t_8c37f2d9-conflict-triage
HEAD: 7740625eda5de7ba3c9a5cd9965b208c024cc35e
FILES: docs/receipts/t_8c37f2d9-conflict-triage.md
CHECKS: unique-tip isolation + main symbol/file probes (see above)
SCOPE-FENCE: triage document only
RISKS: card id missing from fleet board so heartbeat could not be written
BLOCKED-ON: none for Step 1; landing of #71/#75/#76/#77 is Fable
