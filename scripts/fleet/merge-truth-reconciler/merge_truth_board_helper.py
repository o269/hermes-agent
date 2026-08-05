#!/usr/bin/env python3
"""Broker-only board helper for merge-truth-reconciler (post-PR36/PR38/PR41).

Speaks the JSON stdin/stdout contract documented in
``docs/merge-truth-reconciler-runbook.md``. All production board I/O goes through
boardd via ``hermes_cli.kb_client`` and lifecycle functions over
``BrokerConnection``. There is no direct SQLite open of the fleet DB and no
SQL fallback when the broker is unavailable.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

IDENTITY = "merge-truth-reconciler"
REQUIRED_CAPABILITIES = frozenset(
    {
        "inventory_hex_v1",
        "card_citations_hex_v2",
        "card_authority_v1",
        "ownership_converge_v1",
        "semantic_url_release_v1",
        "gate_complete_opid_v1",
        "evidence_comment_opid_v1",
        "recompute_ready_receipts_v1",
    }
)
TERMINAL_STATUSES = frozenset(
    {
        "done",
        "archived",
        "cancelled",
        "canceled",
        "failed",
        "error",
        "errored",
        "timed_out",
        "timed-out",
        "timedout",
        "timeout",
        "rejected",
        "aborted",
        "abandoned",
    }
)
ACTIVE_STATUSES = frozenset({"running", "in_progress", "in-progress"})
TERMINAL_CUSTODY_ASSIGNEES = frozenset({"fable", "s4", "operator-gate", "terminal"})
PR_URL_RE = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)",
    re.IGNORECASE,
)
OPID_RE = re.compile(r"\bOPID=([A-Za-z0-9:_./+-]+)\b")


class HelperError(RuntimeError):
    """Structured helper failure (mapped to ok=false)."""


class CapabilityError(HelperError):
    """Installed broker surface cannot prove a required capability."""


def canonical_pr_url(value: str) -> str:
    match = PR_URL_RE.search(value.strip())
    if not match:
        raise ValueError("not a canonical GitHub pull-request URL")
    owner, repo, number = match.groups()
    return f"https://github.com/{owner.lower()}/{repo.lower()}/pull/{int(number)}"


def _parse_skills(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item).strip()]
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return [text]
        if isinstance(parsed, list):
            return [str(item) for item in parsed if str(item).strip()]
        return [text]
    return [str(raw)]


def _authority_flags(status: str, assignee: str, title: str, body: str) -> dict[str, bool]:
    status_l = (status or "").strip().lower()
    assignee_l = (assignee or "").strip().casefold()
    joined = f"{title or ''}\n{body or ''}"
    terminal = status_l in TERMINAL_STATUSES
    active_run = status_l in ACTIVE_STATUSES
    protected = (
        assignee_l in TERMINAL_CUSTODY_ASSIGNEES
        or bool(re.search(r"\b(fable|terminal)[- ]custody\b", joined, re.I))
        or bool(
            re.search(
                r"\b(OPERATOR(?:-GATE|-HOLD)?|DECISION REQUIRED|DO NOT DISPATCH|QUIESCE|FREEZE-GATE)\b",
                joined,
                re.I,
            )
        )
    )
    if terminal and active_run:
        # Canonical lifecycle never claims both; prefer terminal for finished cards.
        active_run = False
    return {
        "terminal": terminal,
        "protected_custody": protected,
        "active_run": active_run,
    }


def _text_mentions_url(text: str, urls: set[str]) -> bool:
    if not text:
        return False
    found = set()
    for match in PR_URL_RE.finditer(text):
        try:
            found.add(canonical_pr_url(match.group(0)))
        except ValueError:
            continue
    return bool(found.intersection(urls))


def _hex_text(value: str | None) -> str:
    return (value or "").encode("utf-8", errors="surrogatepass").hex()


def _comment_has_opid(body: str, operation_id: str) -> bool:
    if not body:
        return False
    if f"OPID={operation_id}" in body:
        return True
    match = OPID_RE.search(body)
    return bool(match and match.group(1) == operation_id)


@dataclass
class BoardHelper:
    """Capability-versioned broker adapter used by merge-truth-reconciler."""

    query: Callable[..., list[dict[str, Any]]]
    add_comment: Callable[..., Mapping[str, Any]]
    complete_task: Callable[..., bool]
    recompute_ready: Callable[[], int]
    exec_write: Callable[..., Mapping[str, Any]]
    run_txn: Callable[[Callable[[Any], Any]], Any]
    now: Callable[[], int] = lambda: int(time.time())

    @classmethod
    def from_env(cls) -> "BoardHelper":
        if os.environ.get("HERMES_KANBAN_BROKER") != "1":
            raise CapabilityError(
                "HERMES_KANBAN_BROKER=1 is required; broker-only helper has no SQL fallback"
            )
        sock = os.environ.get("BOARDD_SOCK", "").strip()
        if not sock:
            raise CapabilityError("BOARDD_SOCK is required for broker helper")

        from hermes_cli import boardd_shim, kanban_db, kb_client

        client = kb_client.get_client()

        def query(sql: str, params: Sequence[Any] | None = None, max_rows: int | None = None):
            return client.query(sql, list(params or []), max_rows=max_rows)

        def add_comment(task_id: str, author: str, body: str, *, op_id: str | None = None):
            return client.add_comment(task_id, author, body, op_id=op_id)

        def complete_task(task_id: str, receipt: str) -> bool:
            conn = boardd_shim.BrokerConnection(client=client)
            try:
                return bool(
                    kanban_db.complete_task(
                        conn,
                        task_id,
                        result=receipt,
                        summary=receipt,
                    )
                )
            finally:
                conn.close()

        def recompute_ready() -> int:
            conn = boardd_shim.BrokerConnection(client=client)
            try:
                return int(kanban_db.recompute_ready(conn) or 0)
            finally:
                conn.close()

        def exec_write(sql: str, params: Sequence[Any] | None = None, *, op_id: str):
            return client.exec_write(sql, list(params or []), op_id=op_id)

        def run_txn(work: Callable[[Any], Any]) -> Any:
            """Run work inside one broker interactive transaction."""
            conn = boardd_shim.BrokerConnection(client=client)
            try:
                with kanban_db.write_txn(conn):
                    return work(conn)
            finally:
                conn.close()

        # Prove the socket is live before advertising capabilities.
        try:
            client.ping()
        except Exception as exc:  # noqa: BLE001 - surface as capability block
            raise CapabilityError(f"boardd ping failed: {exc}") from exc

        return cls(
            query=query,
            add_comment=add_comment,
            complete_task=complete_task,
            recompute_ready=recompute_ready,
            exec_write=exec_write,
            run_txn=run_txn,
        )

    def handle(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(request, Mapping):
            raise HelperError("request must be a JSON object")
        action = str(request.get("action") or "").strip()
        author = str(request.get("author") or "").strip()
        payload = request.get("payload") or {}
        if author != IDENTITY:
            raise HelperError(f"author must be {IDENTITY}")
        if not isinstance(payload, Mapping):
            raise HelperError("payload must be an object")
        handlers = {
            "capabilities": self._capabilities,
            "inventory": self._inventory,
            "list_cards_citing": self._list_cards_citing,
            "converge_ownership": self._converge_ownership,
            "complete_gate_card": self._complete_gate_card,
            "add_evidence_comment": self._add_evidence_comment,
            "recompute_ready": self._recompute_ready,
        }
        handler = handlers.get(action)
        if handler is None:
            raise HelperError(f"unknown action={action}")
        result = handler(payload)
        return {"ok": True, "result": result}

    def _capabilities(self, _payload: Mapping[str, Any]) -> dict[str, Any]:
        return {"capabilities": sorted(REQUIRED_CAPABILITIES)}

    def _inventory(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        encoding = str(payload.get("body_encoding") or "hex")
        if encoding != "hex":
            raise HelperError("inventory requires body_encoding=hex")

        ownership_rows = self.query(
            "SELECT DISTINCT canonical_url AS canonical_url "
            "FROM task_pr_ownership "
            "WHERE canonical_url IS NOT NULL AND TRIM(canonical_url) != '' "
            "ORDER BY canonical_url ASC",
            max_rows=50_000,
        )
        canonical_urls: list[str] = []
        for row in ownership_rows:
            raw = str(row.get("canonical_url") or "").strip()
            if not raw:
                continue
            try:
                canonical_urls.append(canonical_pr_url(raw))
            except ValueError:
                continue
        # Stable unique order
        canonical_urls = sorted(set(canonical_urls))

        body_rows = self.query(
            "SELECT hex(COALESCE(body, '')) AS body_hex FROM tasks "
            "WHERE body IS NOT NULL AND TRIM(body) != ''",
            max_rows=50_000,
        )
        comment_rows = self.query(
            "SELECT hex(COALESCE(body, '')) AS body_hex FROM task_comments "
            "WHERE body IS NOT NULL AND TRIM(body) != ''",
            max_rows=100_000,
        )
        hex_texts = [
            str(row.get("body_hex") or "")
            for row in list(body_rows) + list(comment_rows)
            if str(row.get("body_hex") or "")
        ]
        return {
            "canonical_urls": canonical_urls,
            "hex_texts": hex_texts,
        }

    def _list_cards_citing(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        encoding = str(payload.get("body_encoding") or "hex")
        if encoding != "hex":
            raise HelperError("list_cards_citing requires body_encoding=hex")
        raw_urls = payload.get("canonical_urls") or []
        if not isinstance(raw_urls, list):
            raise HelperError("canonical_urls must be a list")
        urls: set[str] = set()
        for item in raw_urls:
            try:
                urls.add(canonical_pr_url(str(item)))
            except ValueError as exc:
                raise HelperError(f"invalid canonical url: {item}") from exc
        if not urls:
            return {"cards": []}

        # Ownership ledger is the primary citation index; body/comment scan is
        # a bounded enrichment so hex-only or split receipts still resolve.
        ownership_hits = self.query(
            "SELECT DISTINCT task_id FROM task_pr_ownership "
            "WHERE canonical_url IN (%s)" % ",".join("?" for _ in urls),
            tuple(sorted(urls)),
            max_rows=20_000,
        )
        task_ids = {str(row["task_id"]) for row in ownership_hits if row.get("task_id")}

        task_rows = self.query(
            "SELECT id, title, body, status, assignee, skills FROM tasks "
            "WHERE status NOT IN ('archived')",
            max_rows=50_000,
        )
        tasks_by_id = {str(row["id"]): row for row in task_rows if row.get("id")}

        for task_id, row in tasks_by_id.items():
            blob = f"{row.get('title') or ''}\n{row.get('body') or ''}"
            if _text_mentions_url(blob, urls):
                task_ids.add(task_id)

        if not task_ids:
            return {"cards": []}

        placeholders = ",".join("?" for _ in task_ids)
        comment_rows = self.query(
            f"SELECT task_id, body FROM task_comments WHERE task_id IN ({placeholders}) "
            "ORDER BY id ASC",
            tuple(sorted(task_ids)),
            max_rows=200_000,
        )
        comments_by_task: dict[str, list[str]] = {task_id: [] for task_id in task_ids}
        for row in comment_rows:
            task_id = str(row.get("task_id") or "")
            body = str(row.get("body") or "")
            if task_id not in comments_by_task:
                continue
            comments_by_task[task_id].append(body)
            if _text_mentions_url(body, urls):
                task_ids.add(task_id)

        cards: list[dict[str, Any]] = []
        for task_id in sorted(task_ids):
            row = tasks_by_id.get(task_id)
            if row is None:
                continue
            title = str(row.get("title") or "")
            body = str(row.get("body") or "")
            status = str(row.get("status") or "").strip().lower()
            assignee = str(row.get("assignee") or "")
            if not status:
                raise HelperError(f"card {task_id} lacked status")
            comments = tuple(comments_by_task.get(task_id, ()))
            # Keep only cards that still cite at least one requested URL after
            # decoding (ownership hit alone can be stale relative to body edits).
            combined = "\n".join((title, body, *comments))
            if not _text_mentions_url(combined, urls) and task_id not in {
                str(r.get("task_id")) for r in ownership_hits
            }:
                continue
            authority = _authority_flags(status, assignee, title, body)
            cards.append(
                {
                    "task_id": task_id,
                    "title": title,
                    "body_hex": _hex_text(body),
                    "comments_hex": [_hex_text(item) for item in comments],
                    "status": status,
                    "assignee": assignee,
                    "skills": _parse_skills(row.get("skills")),
                    "terminal": authority["terminal"],
                    "protected_custody": authority["protected_custody"],
                    "active_run": authority["active_run"],
                }
            )
        return {"cards": cards}

    def _converge_ownership(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        merge = payload.get("merge") or {}
        if not isinstance(merge, Mapping):
            raise HelperError("merge payload must be an object")
        try:
            url = canonical_pr_url(str(merge.get("canonical_url") or ""))
        except ValueError as exc:
            raise HelperError("merge.canonical_url is invalid") from exc
        operation_id = str(payload.get("operation_id") or "").strip()
        if not operation_id:
            raise HelperError("operation_id is required")
        if payload.get("atomic") is not True:
            raise HelperError("converge_ownership requires atomic=true")

        def work(conn: Any) -> dict[str, Any]:
            before = conn.execute(
                "SELECT task_id, declared FROM task_pr_ownership WHERE canonical_url = ?",
                (url,),
            ).fetchall()
            affected = sorted({str(row["task_id"]) for row in before if row["task_id"]})
            cleared = 0
            if before:
                cur = conn.execute(
                    "DELETE FROM task_pr_ownership WHERE canonical_url = ?",
                    (url,),
                )
                cleared = int(cur.rowcount or 0)

            # Semantic release: no declared custody remains for this URL, and
            # no in-window ownership row remains. Comment citations alone are
            # references (disowned) under post-PR36 active_pr custody.
            remaining = conn.execute(
                "SELECT task_id, declared FROM task_pr_ownership WHERE canonical_url = ?",
                (url,),
            ).fetchall()
            if remaining:
                raise CapabilityError(
                    f"broker could not prove semantic URL release: {url}"
                )
            declared_left = conn.execute(
                "SELECT 1 FROM task_pr_ownership WHERE canonical_url = ? AND declared = 1 LIMIT 1",
                (url,),
            ).fetchone()
            if declared_left is not None:
                raise CapabilityError(
                    f"broker could not prove semantic URL release: {url}"
                )

            # Durable receipt event for op-id replay observability.
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    affected[0] if affected else "t_system",
                    None,
                    "merge_truth_ownership_converge",
                    json.dumps(
                        {
                            "operation_id": operation_id,
                            "canonical_url": url,
                            "cleared_rows": cleared,
                            "affected_task_ids": affected,
                            "author": IDENTITY,
                        },
                        sort_keys=True,
                    ),
                    self.now(),
                ),
            )
            return {
                "cleared_rows": cleared,
                "affected_task_ids": affected,
                "semantic_released": True,
            }

        try:
            return self.run_txn(work)
        except CapabilityError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HelperError(f"converge_ownership failed: {exc}") from exc

    def _complete_gate_card(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        task_id = str(payload.get("task_id") or "").strip()
        receipt = str(payload.get("receipt") or "")
        operation_id = str(payload.get("operation_id") or "").strip()
        if not task_id or not receipt or not operation_id:
            raise HelperError("complete_gate_card requires task_id, receipt, operation_id")

        rows = self.query(
            "SELECT id, status FROM tasks WHERE id = ?",
            (task_id,),
            max_rows=1,
        )
        if not rows:
            raise HelperError(f"unknown task {task_id}")
        status = str(rows[0].get("status") or "").lower()

        comment_rows = self.query(
            "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id ASC",
            (task_id,),
            max_rows=10_000,
        )
        already = any(_comment_has_opid(str(row.get("body") or ""), operation_id) for row in comment_rows)
        if already and status in TERMINAL_STATUSES:
            return {"changed": False}

        body = f"{receipt}\nOPID={operation_id}\nauthor={IDENTITY}"
        # Native boardd applied_ops makes the receipt comment exactly-once.
        self.add_comment(task_id, IDENTITY, body, op_id=f"mtr-complete-comment:{operation_id}")

        if status in TERMINAL_STATUSES:
            return {"changed": False}

        changed = bool(self.complete_task(task_id, receipt))
        if not changed:
            # complete_task is status-CAS; treat already-done as idempotent success.
            post = self.query(
                "SELECT status FROM tasks WHERE id = ?",
                (task_id,),
                max_rows=1,
            )
            post_status = str((post[0] if post else {}).get("status") or "").lower()
            if post_status not in TERMINAL_STATUSES and post_status not in {"done"}:
                # todo/ready/blocked that failed CAS is an error; try set via
                # lifecycle only — do not force arbitrary SQL status writes.
                raise HelperError(
                    f"complete_gate_card CAS failed for {task_id} status={post_status}"
                )
            return {"changed": False}
        return {"changed": True}

    def _add_evidence_comment(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        task_id = str(payload.get("task_id") or "").strip()
        receipt = str(payload.get("receipt") or "")
        operation_id = str(payload.get("operation_id") or "").strip()
        if not task_id or not receipt or not operation_id:
            raise HelperError("add_evidence_comment requires task_id, receipt, operation_id")

        comment_rows = self.query(
            "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id ASC",
            (task_id,),
            max_rows=10_000,
        )
        if any(_comment_has_opid(str(row.get("body") or ""), operation_id) for row in comment_rows):
            return {"changed": False}

        body = f"{receipt}\nOPID={operation_id}\nauthor={IDENTITY}"
        self.add_comment(task_id, IDENTITY, body, op_id=f"mtr-evidence:{operation_id}")
        return {"changed": True}

    def _recompute_ready(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        operation_id = str(payload.get("operation_id") or "").strip()
        if not operation_id:
            raise HelperError("recompute_ready requires operation_id")
        before_rows = self.query(
            "SELECT id FROM tasks WHERE status = 'ready'",
            max_rows=100_000,
        )
        before = {str(row["id"]) for row in before_rows if row.get("id")}
        promoted = int(self.recompute_ready() or 0)
        after_rows = self.query(
            "SELECT id FROM tasks WHERE status = 'ready'",
            max_rows=100_000,
        )
        after = {str(row["id"]) for row in after_rows if row.get("id")}
        actual = len(after - before)
        # Prefer the receipt-backed delta; fall back to lifecycle return only when
        # the set delta is unavailable (should not happen on a healthy broker).
        count = actual if actual or promoted == 0 else promoted
        if promoted and actual and promoted != actual:
            # Disagreement is still receipt-backed via actual set delta.
            count = actual
        return {
            "actual_promoted_count": int(count),
            "lifecycle_return": int(promoted),
            "operation_id": operation_id,
        }


def main(argv: Sequence[str] | None = None) -> int:
    del argv  # argv intentionally unused — no secrets/card text on argv
    try:
        raw = sys.stdin.read()
        request = json.loads(raw) if raw.strip() else {}
        helper = BoardHelper.from_env()
        response = helper.handle(request)
    except CapabilityError as exc:
        response = {"ok": False, "error": str(exc), "etype": "CapabilityBlocked"}
    except HelperError as exc:
        response = {"ok": False, "error": str(exc), "etype": "HelperError"}
    except Exception as exc:  # noqa: BLE001 - never leak stack to caller stdout contract
        response = {"ok": False, "error": f"helper failure: {exc}", "etype": type(exc).__name__}
    sys.stdout.write(json.dumps(response, sort_keys=True, separators=(",", ":")))
    sys.stdout.write("\n")
    return 0 if response.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
