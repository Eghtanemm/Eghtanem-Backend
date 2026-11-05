# Repair

Your implementation for issue #{{issue_number}} ("{{issue_title}}") did not pass the {{repair_source}}.
Repair attempt {{attempt}} of {{max_attempts}}.

## Problems to fix

{{problems}}

## Rules

- First analyse the root cause of each problem; don't patch symptoms.
- Fix only what is needed for these problems; keep the change minimal and within the issue's scope.
- Never disable, skip or delete tests, and never weaken validation/security to get green checks.
- Tests already failing on the base branch are not yours to fix (they are excluded from the gate).
- Re-run the relevant tests via `python3 {{sandbox}} phpunit --filter <TestName>` and
  `python3 {{sandbox}} pint <changed files>` before finishing (exact form, one command at a time; if they
  cannot run, finish anyway - the orchestrator re-runs every gate independently).
- If a problem cannot be fixed without human approval, set `requires_human=true` with reasons.

Return the updated structured implementation report (covering the whole change, not just this repair).
