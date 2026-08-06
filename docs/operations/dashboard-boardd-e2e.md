# Private dashboard-to-boardd acceptance

Dashboard end-to-end and disposable acceptance requests must never target an
installed dashboard or a live board. Archiving a test card does not undo the
write: task and event rows remain in the board's durable audit history.

Run the repository-owned private acceptance test instead:

```bash
pytest -q tests/test_dashboard_boardd_e2e.py
```

The test starts a temporary `boardd` process with a pytest-owned database and
Unix socket. It overrides `HERMES_KANBAN_HOME`, `HERMES_KANBAN_DB`,
`HERMES_KANBAN_BOARD`, and `BOARDD_SOCK` for both broker and dashboard child
processes, then drives the real dashboard plugin through create, complete,
archive, and read-back over `boardd_shim.BrokerConnection`.

The parent test process is deliberately seeded with must-not-touch board and
socket paths that look like inherited Fleet selectors. The test proves those
paths remain absent. This makes the command safe to launch from a Fleet-pinned
worker without creating even an archived E2E card on that worker's board.
