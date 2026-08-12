#!/usr/bin/env bash
# Deterministic stand-in for ~/godmode-bus/bin/brief-path-guard.sh.
# Used by dispatch tests so the must-fire path does not depend on vps2 SSH.
#
# Contract matches the real checker:
#   --stdin [--host vps2]  → inspect brief text on stdin
#   rc 0 pass / 4 violation / 5 unverifiable / 2 usage
#
# A citation of MUST_FIRE_BLITZ_ONLY.md is the known-positive violation
# the guard exists to catch (blitz-only bus artifact).
set -uo pipefail

mode="${1:-}"
if [[ "$mode" != "--stdin" && "$mode" != "--task" ]]; then
  echo "usage: brief-path-guard-stub.sh --stdin --host vps2 | --task <id>" >&2
  exit 2
fi
if [[ "$mode" == "--task" ]]; then
  echo "stub does not read boardd; pipe the brief via --stdin" >&2
  exit 2
fi

body="$(cat)"
if grep -qE 'MUST_FIRE_BLITZ_ONLY(\.md)?' <<<"$body"; then
  echo "stdin VIOLATION to-orchestrator/MUST_FIRE_BLITZ_ONLY.md  (exists on blitz, ABSENT on vps2 — worker cannot read it; push via vps2-outbox/ or attach)"
  exit 4
fi
if grep -qE '\bUNVERIFIABLE_BARE_NAME_XYZ\b' <<<"$body"; then
  echo "stdin UNVERIFIABLE bare citation 'UNVERIFIABLE_BARE_NAME_XYZ' — resolves to no blitz bus file; cannot check target-host readability (cite a full path)"
  exit 5
fi
echo "stdin PASS"
exit 0
