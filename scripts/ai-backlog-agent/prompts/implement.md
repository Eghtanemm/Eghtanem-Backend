# Stage 4+5: Implementation and report

Implement the approved plan for issue #{{issue_number}} ("{{issue_title}}") in this worktree.

## Analysis

```json
{{analysis}}
```

## Approved plan

```json
{{plan}}
```

## Rules

- Implement the smallest correct solution that follows the plan and the repository's existing Laravel
  conventions (look at neighbouring code before writing new code). Apply SOLID where it genuinely helps;
  avoid new layers or abstractions the codebase doesn't already use.
- Preserve existing API contracts unless the plan explicitly says otherwise.
- Never edit or delete existing migrations; add new ones with a correct `down()`.
- Add regression tests that fail without your fix and pass with it. Update documentation (README/docs)
  only where behaviour visible to developers/operators changes.
- Do not disable, skip or delete tests; do not weaken validation or security checks to make tests pass.
- No unrelated changes, no formatting churn in files you don't otherwise touch, no debug leftovers.
- Run the relevant tests yourself via `python3 {{sandbox}} phpunit --filter <TestName>` and fix failures,
  then run `python3 {{sandbox}} pint <changed files>` (exact form, one command at a time; if they cannot run,
  finish anyway - the orchestrator runs every quality gate independently). Note that some tests already fail on the base
  branch (listed below) - you are not required to fix those, but you must not break passing ones.
- Do not commit; the orchestrator commits after independent checks.
- If you discover the *code change itself* needs human approval (destructive migration, credential
  rotation, production access, product decision), stop and set `requires_human=true` with reasons.
  Steps a person must do after merge go in `post_merge_human_actions` and do not stop you.

Tests already failing on the base branch (pre-existing, not caused by you):
{{baseline_failures}}

When done, return the structured implementation report: root cause, files modified, changes made, tests
added, known limitations, potential risks, whether the issue is fully resolved, and a `commit_summary`
(imperative, max 60 characters, no issue number).
