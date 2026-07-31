# Buzz → Kanban structured intake v1

This is a fleet-local control-plane bridge. Buzz is the signed human command and
receipt surface; the `fleet` Kanban board remains authoritative. The bridge is
implemented by `scripts/buzz_kanban_bridge.py` and is intentionally not wired
into the Hermes agent loop or Buzz core.

Production enablement is an operator action. Landing this code does not install,
start, restart, or configure a service.

## Intake envelope

Only an event that starts with the exact line `FLEET_KANBAN_INTAKE_V1` is an
intake candidate. The next value must be one JSON object, either raw or in a
`json` fence. Natural-language channel messages never trigger card creation.

~~~text
FLEET_KANBAN_INTAKE_V1
```json
{
  "schema": "buzz.kanban.intake/v1",
  "idempotency_key": "buzz-example-v1",
  "title": "Implement an example",
  "objective": "Describe the outcome, not an imperative hidden in chat prose.",
  "acceptance_criteria": [
    "Focused verification passes.",
    "Evidence is attached to the card."
  ],
  "requested_specialist": "engineer",
  "urgency": "high",
  "risk": "low",
  "workspace": "dir:/mnt/HC_Volume_106418160/fleet-workspaces/example",
  "evidence": [
    "exact-head commit",
    "focused test output"
  ],
  "reviewer": "independent security reviewer",
  "approval_gate": "Fable land and deployment gate",
  "parents": ["t_a29728ae"]
}
```
~~~

Required fields are strict and unknown or duplicate JSON keys are rejected.
`parents` is the only optional field. `urgency` is one of `low`, `normal`,
`high`, or `critical`; `risk` is one of `low`, `medium`, `high`, or `critical`.
High/critical risk cannot claim to have no approval gate. Non-scratch workspaces
must be below an operator-supplied `--workspace-root`. The requested specialist
must be in an explicit `--allowed-specialist` profile allowlist. Provider/model
fields are unknown fields and are rejected rather than forwarded.

## Safety invariants

- Exact owner pubkey, channel `h` tag, event kind, freshness, and future-clock
  skew are validated before parsing or writing.
- The bridge passes no model/provider override and does not claim, promote, or
  dispatch a card. Assignees must be predeclared operator-approved profiles.
- Cards are created through `hermes kanban --board fleet` with
  `HERMES_KANBAN_BROKER=1` and `BOARDD_SOCK` pinned to boardd.
- `--initial-status todo` is explicit. A completed/missing parent cannot turn
  trusted intake into a transient `ready` or `running` card.
- The idempotency key returns the existing non-archived card on replay. A replay
  is acknowledged only if title, body, owner, assignee, priority, workspace,
  parents, status, and idempotency key all match the broker read-back.
- A Buzz receipt is emitted only after that read-back. The bridge then reads the
  thread from Buzz and persists the receipt event ID. A bridge-authored marker
  suppresses a duplicate receipt after a crash between Buzz acceptance and the
  local state commit.
- State is held under a non-blocking process lock and written atomically with
  mode `0600`, file and directory fsync, and bounded receipt history.
- Broker or Buzz failures do not advance the failed event's cursor and do not
  produce a success receipt.
- Unauthorized senders receive no reply.

Buzz's deployed CLI currently offers timestamp-bounded `messages get`, not
`listen`. Polling therefore overlaps one full freshness window and deduplicates
intake candidates by the signed event ID. When a generic event stream is
available, the Buzz adapter can change without altering the envelope or broker
contract.

## High-signal Kanban delivery

The outbound path queries `task_events` through the same boardd client and emits
only assignment, review, blocker, failure, hold/resume, quota, and completion
events. Heartbeats and percentage snapshots remain silent. The durable integer
event cursor advances over both emitted and intentionally filtered events.

On first enablement the cursor initializes to the board's current maximum event
ID. Historical events are not replayed into `#fleet`, preventing a startup
flood. Subsequent writes use a bridge-authored event marker plus Buzz read-back
to suppress duplicate posts.

## Verification

Focused tests:

```bash
PYTHONPATH="$PWD" python3 -m pytest -q \
  tests/hermes_cli/test_buzz_kanban_bridge.py

PYTHONPATH="$PWD" python3 -m pytest -q \
  tests/hermes_cli/test_kanban_core_functionality.py::test_explicit_todo_initial_status_never_becomes_dispatchable \
  tests/hermes_cli/test_kanban_core_functionality.py::test_cli_create_todo_readback_includes_idempotency_key
```

The fixtures cover duplicate delivery, crash-after-receipt replay, malformed and
duplicate-key JSON, unauthorized sender, stale input, workspace and approval
allowlists, broker failure, broker read-back mismatch, private atomic state,
high-signal filtering, and first-run outbound cursor bootstrap.

## Operator-only enablement shape

Read the Buzz private key from an existing `0600` `EnvironmentFile`; never place
it in a unit or command line. Supply all non-secret settings as arguments:

```bash
python3 scripts/buzz_kanban_bridge.py \
  --channel <self-hosted-fleet-channel-uuid> \
  --owner-pubkey <owner-hex-pubkey> \
  --bridge-pubkey <bridge-hex-pubkey> \
  --board fleet \
  --board-socket /home/odai/.hermes/kanban/boardd-run/boardd.sock \
  --state-file /home/odai/.local/state/buzz-kanban-bridge.json \
  --workspace-root /mnt/HC_Volume_106418160/fleet-workspaces \
  --allowed-specialist orchestrator \
  --allowed-specialist engineer \
  --buzz-bin /home/odai/.local/bin/buzz \
  --hermes-bin /home/odai/.local/bin/hermes
```

Before enablement, require an independent exact-head security PASS. Fable alone
lands, installs the service, disables the aggregate ticker, runs the live owner
canary, and verifies the resulting card and Buzz receipt.
