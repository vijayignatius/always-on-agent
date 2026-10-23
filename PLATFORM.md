# Always-On Ops Agent — platform (Dev 3)

The always-on loop around the two agent capabilities: **state** (only process what changed),
**write-back** (idempotent, dry-run by default), **eval** (score against the gold set) and the
**run report**. Python 3 standard library only — no installs, no API calls.

## Files

| File | What it does |
|---|---|
| `prompts/orchestrator.md` | The routine's top-level instructions (calls Dev 1's and Dev 2's prompts, then `run.py`) |
| `state.py` | sha256 per file in `issues/` and `contracts/` → `state.json`; reports new / changed / removed |
| `writeback.py` | Writes triage back to `issues/*.json` and one issue per compliance violation — never duplicates |
| `report.py` | Writes `out/run-report.md` (changes, triage, needs-human, compliance matrix, write-back, score) |
| `run.py` | One pass: diff state → write-back → report → record state (in that order) |
| `eval/gold.json` | Expected answers: 5 issues + 21 contract × rule findings (judgement calls accept >1 value) |
| `eval/grade.py` | Scores `out/` against the gold set |
| `eval/fixtures/perfect/`, `eval/fixtures/flawed/` | Sample outputs to test the grader and write-back |

## Interface for Dev 1 and Dev 2

The platform reads only these — keep the names and fields:

- **Dev 1 (triage):** `out/triage/<issue-id>.json`, one per issue, fields:
  `id, is_incident, severity ("P0".."P3" or null), root_cause, evidence[], runbook (path or null),
  related_deploy ({service, version, rollback_to} or null), linked_issues[], recommended_action,
  page (string or null), confidence, needs_human`.
- **Dev 2 (compliance):** `out/compliance/<vendor>__<rule>.json` (vendor = contract file stem, e.g.
  `sirius-storage`), fields: `contract, vendor, rule, status ("compliant"|"violation"|"needs_review"),
  clause_ref, quote, policy_quote, reason, suggested_fix`. Rules: `data_residency, audit_rights,
  termination, liability_cap, subprocessors, breach_notification, governing_law`.
- **Dev 2 (issue template, optional):** `templates/violation_issue.md`. **First line = issue title.**
  Placeholders in either style — `{field}` or `<field>` (any finding field works): `contract clause_ref quote
  policy_quote reason suggested_fix`, plus `{vendor_title}` / `<Vendor>` (vendor name) and `{rule_title}` /
  `<rule>` (readable rule name, e.g. "Liability Cap"). Other `<...>` text is left as-is. Without a template, a
  built-in one is used. *(Both styles supported since the Dev 2 merge — the plan used `<…>`, this doc `{…}`.)*

`eval/fixtures/perfect/` shows exactly what valid outputs look like.

## Commands

```bash
python3 eval/grade.py                              # score out/
python3 eval/grade.py --out eval/fixtures/flawed   # see what failures look like
python3 state.py                                   # what's new/changed since the last run
python3 run.py                                     # dry run: report + planned write-back, nothing written
python3 run.py --apply                             # write back, report, record state.json
python3 run.py --apply --github                    # ...and open GitHub issues (gh default repo = the fork)
```

## How a run works

1. `state.py` lists new/changed files → the agent triages / re-checks only those (`prompts/orchestrator.md`).
2. Dev 1's and Dev 2's prompts write `out/triage/` and `out/compliance/`.
3. `run.py --apply`: write-back → `out/run-report.md` (with the eval score) → `state.json` recorded **last**,
   so the agent's own edits to `issues/*.json` don't look like new changes next run.

**Idempotency:** each agent comment carries a `marker` (hash of the triage record) — the same record is never
posted twice; a changed record gets one new comment. Compliance issues are skipped if
`compliance-issues/<vendor>__<rule>.md` (or a GitHub issue with the same title) already exists.
`needs_review` findings are reported for a human, never opened as issues.

## Verified (2026-10-23, on a throwaway copy)

| Test | Result |
|---|---|
| Grade the perfect fixture | 53/53 (100%), violation precision/recall 100%, 0 Globex false positives |
| Grade the flawed fixture | 44/53 — caught all 9 planted errors (red-herring deploy, wrong severity, missing link, paging a feature request, "scale pods" + "raise LB timeout", Globex false positive, missed Acme violation, wrong Sirius verdict) |
| Grade an empty `out/` | 0/53 (missing outputs fail) |
| `run.py` (dry run) | 5 issues / 9 violations planned, nothing written |
| `run.py --apply` | 5 issues updated (severity, labels, 1 comment each), 9 violation issues, 3 sent for review, state recorded |
| `run.py --apply` again | 0 updated, 0 opened — idempotent |
| Edit one contract | `state.py` reports exactly that file as changed |

## Safety

- Never runs runbook commands, deploys, rolls back or pages — pages/rollbacks are listed under
  "Needs human approval" in the report.
- Dry run is the default; `--github` only acts with `--apply`.
- **The fork is public** — everything committed or opened as an issue is visible. Data is synthetic.
