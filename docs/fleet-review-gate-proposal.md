# Fleet verdict bridge and merge-gate proposal

Status: proposal only. Do not enable repository protection from this change.

Live measurements below were refreshed at 2026-08-11T07:47:14Z. The bridge is
`scripts/fleet/fleet_verdict_bridge.py`; it is dry-run by default and performs no
GitHub write unless `--apply` is supplied.

## Live defect and current exposure

`o269/omnia` main protection currently has `strict: true`, `enforce_admins:
true`, and exactly two required checks: `Build & Test` and `migration-drift`.
Both are attributed only to the generic GitHub Actions App (ID 15368). There is
no `required_pull_request_reviews` block and the repository has no rulesets.
These settings were read only; this work did not change them.

Ten currently open PRs in fleet-owned repositories have an unresolved board
`FIX_REQUIRED`/`REWORK` and zero native GitHub reviews:

- `o269/hermes-agent#29`
- `o269/oasis-command-center#182`
- `o269/oasis-command-center#194`
- `o269/oasis-command-center#206`
- `o269/omnia-contractor-hub#9`
- `o269/omnia#293`
- `o269/omnia#391`
- `o269/omnia#589`
- `o269/omnia#720`
- `o269/omnia#759`

Eight are mechanically attributable by the conservative parser and live GitHub
lookup. Two legacy cards (`omnia#391` and `omnia#589`) were hand-attributed to
currently open Omnia PR numbers because their old titles omit the repository.
The measurement treats an old blocking verdict as unresolved until a later
exact-head PASS exists; merely pushing a new head does not clear it.

There is one additional upstream exposure, `NousResearch/hermes-agent#67477`.
It has a fleet `FIX_REQUIRED` and one unrelated maintainer `COMMENTED` review,
but no fleet changes-requested review. The broad cross-owner count is therefore
11; the strict zero-native-review fleet-owned count is 10.

Historical prose is not a typed verdict ledger. The bridge therefore refuses
ambiguous repository mappings instead of defaulting every `PR #N` title to
Omnia. A future card convention should always include a GitHub PR URL, exact
head, terminal verdict line, and `PUBLIC_FINDINGS:` section.

## Bridge behavior

For the latest unambiguous verdict on a PR:

- PASS-class -> GitHub `APPROVE`
- FIX_REQUIRED / REWORK / other changes-class -> GitHub `REQUEST_CHANGES`
- the review includes fleet card, reviewer seat, exact reviewed 40-byte head,
  timestamp, and public-safe findings
- each review carries an `fvb:v3` marker containing card, verdict, head, and a
  findings digest; replaying identical content is a no-op
- changed findings or a later verdict post a new review from the same identity;
  GitHub's latest review from that identity supersedes its prior state
- a stale PASS is refused; stale changes remain blocking and are labeled
  `SUPERSEDED HEAD`
- the reviewed commit must resolve uniquely among PR commits; force-pushed
  blocking verdicts are attached to the live head but name the superseded head
- reviewer identity must be cryptographically verified and must differ from the
  PR author
- scan cursors advance only after a fully successful apply pass

Private card prose is never copied wholesale to a public PR. A blocking verdict
must contain a public-safe `file:line` finding. Phone-like strings, email
addresses, database URLs, IPs, and token/secret patterns are redacted. If no
safe finding can be extracted, the bridge refuses the write. PASS reviews say
"No blocking findings."

The bridge also creates a completed GitHub Check Run named
`fleet-review-gate` on the live PR head:

- any explicit changes verdict -> `failure`, regardless of path
- sensitive path + exact-head PASS -> `success`
- no verdict + sensitive path -> `failure` with
  `review-required:no-board-verdict`
- no verdict + no sensitive path -> `success` with
  `skip:no-sensitive-paths`
- non-main bases and heads shared by multiple open PRs always fail
- a newer legacy verdict with an ambiguous repository target prevents
  non-sensitive auto-success on same-number PRs; the card must be repaired with
  an explicit URL before the failure can clear

Check Runs use the App's existing `checks:write` permission. Idempotency uses
App slug, name, conclusion, live SHA, and an `fvb:v3` external-ID fingerprint.
A push produces a new SHA with no inherited success. Native reviews are the
self-contained human audit surface; the App-pinned SHA Check Run is the
path-aware enforceable gate. Path scoping controls only whether *absence* of a
verdict may auto-skip; it never turns a known blocking verdict into success.

Initial sensitive paths are intentionally conservative:

- `scripts/**`
- `apps/api/src/lib/crm/**`
- `apps/api/src/**/migrations/**`
- `supabase/migrations/**`
- `packages/db/**`
- `.rls-admin-allowlist`
- `.github/workflows/**`
- `**/*rls*`

## Reviewer identity and live proof

The operator designated `omnia-lander[bot]` (GitHub App/integration ID
`4555281`) as the distinct review principal. The bridge never merges; Fable
remains sole lander.

Both native review transitions were exercised on closed scratch PR
https://github.com/o269/omnia/pull/856, authored by `o269`, at exact head
`d6926bd5feb2acd64bbefefeca419ff2d00d25bb`:

1. `REQUEST_CHANGES`, review ID `4904106844`, posted at 08:01:08Z.
2. `APPROVE`, review ID `4904106975`, posted one second later from the same App.

The scratch PR is closed without merge and its branch was deleted. Fable supplied
these receipts; this worker did not repeat the proof.

The first real native delivery is also live: `o269/omnia#709` review ID
`4904111164` is `CHANGES_REQUESTED` from `omnia-lander[bot]`, pinned to exact
commit `13983ea622646cd9f1ebdf9e768d34db499cd2a5`. Its body carries the full p207
`FIX_REQUIRED` verdict and artifact SHA-256
`3bae55590b58ca5caed89142005605b07c5d5897dfaaff5eaca8808ea478aacb`.
A read-only receipt check found the PR OPEN at that same head with live
`reviewDecision: CHANGES_REQUESTED`. The mechanical bridge in this PR is what
turns that proven manual delivery into repeatable card-to-review projection.

The App JWT was verified against `/app`; installation ID `152840057` was used
to mint the installation token for App ID `4555281` only after App ID/slug
verification. The private key at `/etc/omnia-lander.pem` remained root-owned
mode 0600 and was read via `sudo -n` only inside the point-of-use process. It
was not copied, logged, committed, or placed on argv.

The live App permission proof showed `checks: write`, `pull_requests: write`,
`metadata: read`, `contents: write`, and `statuses: read`, with installation
access restricted to `o269/omnia`. The bridge needs only Pull requests write,
Checks write, and Metadata read. Contents write belongs to separate landing
custody and increases blast radius; a dedicated reviewer App is the cleaner
long-term split. Do not grant the reviewer App a ruleset bypass.

## Existing-check name collision and CODEOWNERS mitigation

The existing `Build & Test` and `migration-drift` requirements are pinned to
GitHub Actions App ID 15368, not to a workflow file. A candidate workflow can
emit a colliding job/check name from the same App. GitHub documents that a
required check can be pinned to a specific App, but that does not distinguish
two workflows run by the same GitHub Actions App.

Before activation, protect workflow definitions themselves:

1. land a base-owned CODEOWNERS entry covering all `/.github/workflows/` and
   `/.github/CODEOWNERS`;
2. enable required Code Owner review with stale-review dismissal; and
3. verify a candidate PR cannot modify a workflow to spoof an existing check
   without the independent owner approval.

Current CODEOWNERS protects only two OpenAPI workflows and names `@o269` as the
owner. GitHub CODEOWNERS accepts write-capable users or visible teams, not a
GitHub App. Therefore `omnia-lander[bot]` can satisfy ordinary native approval
semantics but cannot itself be the CODEOWNER. For o269-authored workflow PRs,
turning the requirement on before adding a distinct write-capable machine user
or team would deadlock. Provision that owner first (or document an explicit
operator-only admin bypass); do not pretend the current App solves this GitHub
platform constraint.

References:

- https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners
- https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks
- https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets
- https://docs.github.com/en/rest/repos/rules

## Proposed ruleset JSON

`scripts/fleet/fleet-review-gate.ruleset.proposal.json` is valid JSON and
intentionally has `enforcement: disabled` with no bypass actors. It contains:

- App-pinned required check `fleet-review-gate` (integration ID `4555281`); and
- required Code Owner review with stale-review dismissal and zero blanket
  approval count, so review scope comes from CODEOWNERS rather than every PR.

The existing branch protection remains in force and continues to require
`Build & Test` and `migration-drift`. Before activation, the operator must:

1. land broad workflow/CODEOWNERS ownership with a distinct user or team;
2. prove required Code Owner review does not deadlock an o269-authored scratch
   workflow PR;
3. let App 4555281 emit `fleet-review-gate` on a scratch/current SHA using its
   existing Checks permission;
4. run the poller in observation mode long enough to clear parser refusals and
   backfill exact-head reviews;
5. change the proposal from `disabled` to `active`; and
6. read back effective rules and prove all three checks plus workflow-owner
   review are enforced.

Do not add blanket `required_approving_review_count: 1`. The path-aware Check Run
and CODEOWNERS rule avoid converting ordinary Fleet volume into a rubber-stamp
queue. If a blanket count is later chosen, `dismiss_stale_reviews_on_push: true`
is mandatory and the second identity must be monitored as a merge-critical
dependency.

## Last-30 merge impact

Point-in-time dry-run method (2026-08-11T06:34Z): inspect the latest 30 merged
`o269/omnia` PRs, fetch files, apply the bridge's exact sensitive globs, and ask
whether a structured exact-head board PASS existed before merge. Block duration
is PR creation to merge for a PR that would still be blocked. This measures the
proposed parser, not a generous human reading of unstructured prose.

- sample: 30 merged PRs
- non-sensitive auto-skip: 21
- sensitive: 9
- sensitive with a structured PASS before merge: 0
- would still have been blocked at merge: 9

| PR | sensitive reason | minimum blocked duration |
|---|---|---:|
| #847 | RLS guard test | 3h 54m |
| #843 | `.rls-admin-allowlist` + RLS guard test | 3h 27m |
| #840 | Supabase migration | 1h 20m |
| #825 | allowlist + authority scripts | 33m |
| #820 | Supabase migration | 67h 21m |
| #816 | `.rls-admin-allowlist` | 48m |
| #815 | RLS guard test | 48h 14m |
| #813 | Supabase migration | 48h 9m |
| #811 | RLS guard test | 28m |

Activating before review lanes emit typed, head-bound verdicts would have
blocked 30% of the measured merge sample. Land the bridge and typed convention
first, observe non-required Check Runs, then activate protection.

## Deadlocks and break-glass

Expected deadlocks:

1. Poller/App down: every new SHA lacks its App-pinned Check Run; even
   non-sensitive auto-skip cannot be emitted.
2. Boardd down or malformed response: verdict reads fail closed.
3. Blocking verdict lacks a public-safe `file:line` finding: public review is
   refused and the Check Run remains absent/failing.
4. App key lost after REQUEST_CHANGES: the clearing approval cannot come from
   the same identity until recovery.
5. Code Owner rule enabled while workflow ownership still names only the PR
   author: workflow PRs deadlock.

Normal emergency path: expedited exact-head review -> PASS -> bridge posts
APPROVE + success Check Run -> Fable lands.

Board/bridge outage break-glass: the operator, not the App, temporarily disables
the new ruleset, records incident/card/reason and before-state, Fable lands the
emergency PR, then the operator re-enables and reads back protection. Repository
rule changes are audit-visible. If repository-settings access is also lost, the
repository may remain locked until access recovery; there is no honest in-band
escape.

## Rollout order

1. Land this dry-run bridge, tests, documentation, and disabled proposal.
2. Land broad workflow/CODEOWNERS ownership using a distinct write-capable user
   or team; then enable/verify Code Owner review in observation conditions.
3. Run the bridge without a required rule. Native review block/clear is proven;
   next prove an App-authored Check Run on a scratch SHA.
4. Backfill/re-review the 10 live fleet-owned exposures; never post historical
   blocking reviews to merged PRs.
5. Observe parser refusals, path classification, and projection latency.
6. Activate the App-pinned rule and read back effective protection before
   allowing normal landing traffic.
