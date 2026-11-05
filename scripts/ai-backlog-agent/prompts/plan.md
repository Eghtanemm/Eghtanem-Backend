# Stage 3: Implementation plan

Using your analysis of issue #{{issue_number}} ("{{issue_title}}"), produce an implementation plan.
**Do not modify any file in this stage.**

## Analysis (from the previous stage)

```json
{{analysis}}
```

## Requirements for the plan

- State the root cause precisely (file + mechanism). Do not plan before you understand it; re-read code
  if needed.
- Choose the **smallest correct change** that satisfies every acceptance criterion and follows the
  repository's existing Laravel conventions. No speculative abstractions, no unrelated refactoring.
- Preserve existing API contracts (routes, request fields, response shapes, status codes) unless the
  issue explicitly requires changing them; if it does, explain the compatibility impact.
- Database: only *new* migrations, never edits to existing ones; no destructive operations in `up()`;
  describe indexes/constraints and the rollback (`down()`).
- Security: state the implications (authn, authz, validation, rate limiting, abuse scenarios).
- Tests: list the regression/feature tests you will add (PHPUnit, following the style in `tests/`).
  Tests run on SQLite in-memory with no network.
- List risks and rollback considerations.
- Carry over `post_merge_human_actions` (steps after merge that only a person can do). They never block the
  code change. Set `requires_human=true` only if the code change itself cannot be made safely (analysis rules).
- Set `fully_resolves_issue` honestly: false if the plan intentionally leaves acceptance criteria
  unaddressed or post-merge human actions remain (e.g. deployment verification, secret rotation).
- Provide `commit_summary`: an imperative summary of the change, max 60 characters, no issue number.

Return the structured output only.
