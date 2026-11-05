# AI Backlog Agent

A local automation that works through the **Eghtanem Backend** GitHub Project backlog
(<https://github.com/orgs/Eghtanemm/projects/1>) like a small engineering team:

```
Backlog → select → claim → isolated worktree → analyze → plan → implement (Claude Code)
        → quality gates → independent review → (repair)* → commit → push → Draft PR
        → project status "In review" → next issue … until no eligible issue remains
```

One issue at a time, one branch/worktree and one Draft PR per issue. It never merges, never
closes issues, never deploys, and never touches production.

```bash
python3 scripts/ai-backlog-agent/main.py --dry-run   # preview, changes nothing
python3 scripts/ai-backlog-agent/main.py             # process the whole eligible backlog
```

---

## 1. Architecture

| Module | Responsibility |
|---|---|
| `main.py` | CLI, preflight checks, wiring, exit codes |
| `engine.py` | Sequential loop: refresh project → select → process → repeat; stops on systemic errors |
| `issue_selector.py` | Eligibility rules + priority ordering (pure logic) |
| `issue_processor.py` | One issue end-to-end as a **resumable stage machine** |
| `project_client.py` | GitHub Projects v2 GraphQL: schema/field/option discovery, paginated items, dependencies, linked PRs, status updates |
| `github_client.py` | `gh` wrapper with retries, rate-limit back-off, auth/scope detection |
| `git_manager.py` / `worktree_manager.py` | Branches, worktrees, diff, commit, push (no force, no reset, no remote deletion) |
| `claude_runner.py` | Non-interactive Claude Code invocations with per-stage minimal permissions |
| `code_reviewer.py` | Independent review in a fresh Claude session over the actual diff |
| `quality_gate.py` / `php_env.py` | Pint, PHPUnit, PHPStan/Larastan, migrations, composer audit, baseline-aware |
| `php_sandbox.py` | The only way Claude can run PHP (phpunit / pint / route-list) |
| `safety.py` | Redaction, human-approval rules, diff scanning, branch protection |
| `execution_state.py` | SQLite registry: executions, stages, events, locks, baselines |
| `pr_manager.py` | Idempotent Draft PR creation and the PR description |
| `report.py` | Dry-run plan and the end-of-run report |
| `prompts/*.md` | Stage prompts: analyze, plan, implement, review, repair, investigate |

### Stage machine (per issue)

`claimed → workspace_ready → baseline_done → analyzed → planned → implemented → quality_passed → reviewed → committed → pushed → pr_created → project_updated → completed`

Each transition is committed to SQLite before the next stage starts. `--resume` re-enters at the
first unfinished stage and does not repeat finished work: no second analysis, commit or PR.

### Discovery (nothing hard-coded)

The project id, the fields, the Status options, the items (paginated, 50 per page), labels, native
`blockedBy` relations, sub-issues and linked/closing PRs are all discovered through GraphQL on every
run. Two kinds of data are interpreted:

* **Priority** comes from a `Priority` project field if one exists, otherwise from `priority:P0..P3`
  labels. This project has no Priority field, so labels are used.
* **Dependencies** come from GitHub's native "blocked by" relation and from `**Blocked by:** #N`
  lines in the issue body. `**Related:**` lines don't block but do affect ordering.

Status names are matched case-insensitively. For example, the config value `In Progress` matches
the project's `In progress` option. Missing options are reported and never created. This project
has no `Blocked` option, which is reported at startup.

---

## 2. Prerequisites

* Ubuntu (or another Linux) with **Python 3.10+** and **PyYAML** (`python3-yaml`).
  Everything else uses the standard library.
* **git** 2.30+.
* **GitHub CLI** `gh`, logged in, with the `project` scope.
* **Claude Code CLI** `claude`, logged in with your subscription. No API key is needed.
* A PHP environment for the quality gates, which is one of:
  * **Docker** plus the project image (`docker compose build` produces `eghtanem-backend-app`).
    This is the default when `php` is not installed on the host.
  * a local `php` 8.2+ on `PATH`.
* `vendor/` installed in the main checkout. Worktrees reuse it read-only.

## 3. Installation

```bash
cd /path/to/Eghtanem-Backend
sudo apt install -y python3 python3-yaml git gh docker.io     # if missing
docker compose build                                           # builds eghtanem-backend-app
docker compose run --rm app composer install                   # vendor/ for the gates
cp scripts/ai-backlog-agent/config.example.yaml scripts/ai-backlog-agent/config.yaml   # optional
```

## 4. GitHub authentication

```bash
gh auth login                 # if not logged in
gh auth refresh -s project    # projects need the 'project' scope
gh auth status                # should list 'project' and 'repo' scopes
```

Your account needs **write** access to `Eghtanemm/Eghtanem-Backend` and access to org project #1.
The agent never reads or stores the token; it only calls `gh`.

## 5. Claude Code authentication

```bash
claude --version
claude auth status            # expect "loggedIn": true, "authMethod": "claude.ai"
claude auth login             # if needed (uses your subscription; no ANTHROPIC_API_KEY required)
```

## 6. Configuration

All keys are optional and are documented in `config.example.yaml`. The most relevant ones:

| Key | Default | Meaning |
|---|---|---|
| `github.base_branch` | `auto` | Repository default branch (`main`); set e.g. `development` to target another branch |
| `claude.model` | `null` | Your Claude Code default; or `sonnet`, `opus`, … |
| `claude.max_turns` / `timeout` | 60 / 1800s | Per Claude invocation |
| `claude.max_repair_attempts` | 2 | Per failing source (gates, review, safety scan) |
| `execution.default_limit` | `null` | **No limit**: the whole eligible backlog |
| `execution.max_attempts_per_issue` | 2 | Failed attempts before `--retry-failed` gives up |
| `quality_gates.on_unavailable` | `fail` | If PHP can't run, no PR is created |
| `selection.investigate_only_labels` | `[production-readiness]` | Analysed and documented for a human, never implemented |
| `project_status.in_review_fallback` | `null` | Used only if `In review` doesn't exist |

Unknown keys and unsafe values (auto-merge, non-draft PRs, parallel mode, production access,
disabling human approval) are rejected at startup.

## 7. Default: process the entire eligible backlog

```bash
python3 scripts/ai-backlog-agent/main.py
```

The agent processes the first eligible issue completely (through its Draft PR), fetches the project
again, selects the next eligible issue, and repeats until **no eligible issue remains**. There is no
default limit, it never waits for a PR to be merged, and it never asks for confirmation between
issues. Ctrl+C or SIGTERM stops it safely and the run can be resumed.

"Done" means that no *eligible* issue remains. Blocked, human-required, failed and already-PR'd
issues stay visible in the final report together with the reason each one was skipped.

## 8. One issue / a specific issue

```bash
python3 scripts/ai-backlog-agent/main.py --once          # next eligible issue, then exit
python3 scripts/ai-backlog-agent/main.py --issue 7       # only #7; all eligibility rules still apply
```

## 9. Limited run

```bash
python3 scripts/ai-backlog-agent/main.py --limit 5
```

This makes at most 5 **attempts**, counting failures and human escalations, and continues past
isolated failures.

## 10. Dry run

```bash
python3 scripts/ai-backlog-agent/main.py --dry-run
```

The dry run prints every project issue with its priority, status, mode, dependencies and labels,
the proposed execution order, and the reason for every skip. It writes nothing: no state, branches,
commits, PRs or field updates.

`--check` runs only the preflight checks: authentication and scopes, repository permission, base
branch, project schema and status mapping, the PHP environment, and Claude auth.

## 11. Resume

```bash
python3 scripts/ai-backlog-agent/main.py --resume
```

The agent finds executions that were interrupted (Ctrl+C, crash, reboot, a systemic stop) and
continues each from its last completed stage. Before resuming it checks that the issue is still
open and that no *other* PR has appeared. After that it continues with the normal backlog loop.
Without `--resume`, interrupted issues are reported and skipped.

## 12. Failed issues

When an issue fails (gates still red after the repair attempts, a blocking review, Claude timeout,
push or PR error), the agent:

1. records the stage, the reason and the recommended next action;
2. moves the issue back to `Backlog`;
3. **keeps the worktree** if it holds work (a pristine one is removed);
4. continues with the next issue.

A failed issue is never retried in the same run, and later runs skip it until you ask:

```bash
python3 scripts/ai-backlog-agent/main.py --retry-failed   # only previously failed, still-eligible issues
```

Retries are capped by `execution.max_attempts_per_issue`. The previous attempt is preserved as a
local `ai-archive/issue-N-…` branch.

Failures that happen **after the verified commit** (push errors, PR creation errors) don't throw
the work away. The execution is kept resumable, the issue stays `In progress`, and `--resume`
finishes the push, PR and status update without re-running Claude.

**Systemic failures stop the whole batch.** These include GitHub or Claude authentication failure,
a Claude usage limit, a corrupted or uncertain repository state, a production-pointing environment,
a missing base branch, and an attempt to push a protected branch.

## 13. Human approval

The agent never rotates or revokes credentials or `APP_KEY`, touches production, writes destructive
or edits existing migrations, deploys, deletes data, or removes public APIs without a plan. Issues
that need such work are routed by:

* **Rules**: the title or acceptance criteria mention credential/APP_KEY rotation or revocation,
  exposed secrets, incident response, history rewrites or production deploys; or the issue has a
  `needs-human` / `ai:needs-human` / `do-not-automate` label or a `production-readiness` label.
  These issues get **investigate mode**, where Claude analyses the code read-only and writes a
  briefing (code changes vs. human actions).
* **Analysis**: Claude sets `requires_human` (or the finding can't be verified) during
  analysis, plan or implementation.
* **Safety scan** (deterministic, after every implementation): edited or deleted existing
  migrations, destructive `up()` operations, `.env`/key files, secrets in added lines,
  deleted or skipped tests, or an oversized diff.

The agent then comments on the issue, adds the `ai:needs-human` label, moves the status back to
`Backlog` (`Blocked` doesn't exist in this project), saves a private briefing under
`state/reports/human/`, and continues with other issues. For `security`-labelled issues the public
comment contains only the reasons; evidence stays local. Removing the label makes the issue
eligible again.

**Post-merge human actions don't block the code change.** For example: deleting a file from the
servers and confirming a 404, or checking whether a secret leaked and rotating it. When the code fix
itself is safe, the agent still opens the Draft PR, titled "Partially address #N". The PR uses
`Refs #N` (the issue stays open) and lists the required steps in a "Required Human Follow-up"
section.

`needs-verification` issues are investigated first. They are implemented only if Claude finds
concrete evidence in the code; otherwise they go to a human with the evidence.

## 14. Quality gates

| Gate | When | Blocks on |
|---|---|---|
| Pint | changed PHP files (auto-formats them first) | style violations |
| PHPUnit | always; full suite, SQLite in-memory | **new** failures compared with the base commit |
| PHPStan/Larastan | only if installed and configured (it isn't yet → *skipped*, never "passed") | new errors |
| Migrations | new migration files | `migrate` → `rollback` → `migrate` fails on a disposable SQLite DB |
| composer audit | `composer.json`/`lock` changed | new advisories |
| Safety scan | always | see §13 |
| Independent review | always | `critical`/`high` findings or `reject` |

**Baseline-aware testing.** `main` currently has failing tests (8 at the time of writing) and Pint
violations. The agent measures the base commit once per SHA in a separate scratch worktree and fails
the gate only for tests that pass on base and fail with the change. Pre-existing failures are
listed in the PR.

**Isolation.** PHP runs in `docker run --network none` with only the issue worktree mounted and
`vendor/` mounted read-only, using an ephemeral throw-away `APP_KEY` and SQLite in-memory. No `.env`
is present, so no real database or service is reachable. The exception is `composer audit`, which
needs network access and runs only when dependencies change.

If a check can't run, the result is `unavailable`, the issue fails (`on_unavailable: fail`) and
nothing is reported as passed.

## 15. Pull request workflow

* Branch: `ai/fix-issue-<n>-<slug>`, created from the latest `origin/<base>` in a worktree under
  `~/.cache/ai-backlog-agent/worktrees/`.
* Commit: `fix(#<n>): <summary>`, created only on that branch, only after the gates and the review
  pass.
* Push: an explicit refspec with no `--force`. Protected and non-agent branches are refused.
* PR: **Draft**, titled `[AI] Fix #<n> - <summary>`. The description has Summary, Root Cause,
  Implementation, Files Changed, Testing (the commands actually executed), Quality Gates,
  Independent Review, Risks and Limitations, Database Changes, and AI Disclosure.
* `Closes #<n>` is used only when the plan, the implementation *and* the reviewer all agree that the
  issue is fully resolved. Otherwise the title says "Partially address" and the body uses `Refs #<n>`.
* The agent comments on the issue with the PR link, moves the issue to `In review`, and leaves it
  open. It never merges or closes anything.
* Idempotent: an existing open PR for the branch is reused, never duplicated.

`--no-pr` is a local-only mode. It commits on the issue branch but doesn't push, open PRs, comment,
or change fields.

## 16. Claude permissions

Each stage runs `claude -p --output-format json --restricted --permission-mode dontAsk
--permission-prompts none` with an explicit tool allowlist:

| Stage | Tools |
|---|---|
| analyze / plan / review / investigate | Read, Glob, Grep, read-only git (`status/diff/log/show/blame/grep/ls-files`), PHP sandbox |
| implement / repair | the above + Edit, Write |

`--restricted` confines the file tools to the worktree. Anything that isn't allowlisted, such as
other shell commands, network access, `git commit`/`push`, or reads outside the worktree, is denied
without a prompt, and the denials are logged. The analyze → plan → implement → repair stages
continue one session; the reviewer is a fresh session.

## 17. Logs, state and reports

```
scripts/ai-backlog-agent/state/          (git-ignored, mode 0600 DB)
├── agent.db                 executions, stages, events, locks, baselines
├── agent.lock               single-process lock
├── logs/agent.jsonl         structured JSON log (redacted)
├── logs/<execution-id>/     each Claude prompt + raw JSON output
└── reports/run-<id>.md|json end-of-run report; reports/human/ briefings
```

Inspect them with:

```bash
sqlite3 scripts/ai-backlog-agent/state/agent.db \
  "select issue_number,status,stage,failure_reason,pr_url from executions order by created_at"
```

## 18. Tests

```bash
cd scripts/ai-backlog-agent/tests
python3 -m unittest discover -p 'test_*.py'                 # unit tests (fakes; no network/GitHub/Claude)

# Safe integration tests against the real environment. Read-only on GitHub; a local scratch
# branch that is never pushed and is deleted afterwards.
AI_AGENT_INTEGRATION=1 python3 -m unittest -v test_integration_safe
AI_AGENT_INTEGRATION=1 AI_AGENT_INTEGRATION_CLAUDE=1 python3 -m unittest -v test_integration_safe   # + 1 small Claude call
AI_AGENT_INTEGRATION=1 AI_AGENT_INTEGRATION_E2E=1 python3 -m unittest -v test_integration_safe.SyntheticEndToEndTest
#   ^ full pipeline with real Claude on an in-memory synthetic issue (several Claude calls)
```

## 19. Troubleshooting

| Symptom | Fix |
|---|---|
| `GitHub token lacks the project scope` | `gh auth refresh -s project` |
| `Claude Code is not authenticated` | `claude auth login` |
| `PHP environment unavailable` | `docker compose build`, or set `quality_gates.docker_image` / install PHP |
| `vendor/ is missing` | `docker compose run --rm app composer install` |
| `Another ai-backlog-agent process holds …agent.lock` | another run is active (only one at a time) |
| issue skipped: "interrupted (use --resume)" | `main.py --resume` |
| issue skipped: "previous attempt failed" | inspect the kept worktree / report, then `--retry-failed` |
| `local branch ai/fix-issue-N-… already exists` | inspect it; delete it if obsolete (`git branch -D …`) |
| `Status option 'Blocked' does not exist` | informational; add the option to the project if you want it used |
| Claude usage limit → batch stopped | wait for the reset, then `main.py --resume` |
| leftover worktrees | `git worktree list`; remove with `git worktree remove <path>` after checking them |

## 20. Security notes and limitations

* **Coordination across machines** relies on the GitHub status (`In progress`) plus a remote-branch
  check right before claiming. Two machines claiming the same issue in the same second could still
  race. A per-machine SQLite lock and a single-process lock prevent local races.
* **Issue text is untrusted.** Prompts treat it as requirements only, and Claude's permissions limit
  what a malicious issue body could cause. Even so, anyone who can edit project issues can steer
  the agent's changes, which is why every PR is a draft that needs human review.
* **The AI review is not a security sign-off.** Gates and review reduce risk; they don't replace a
  maintainer's review of every Draft PR.
* In `php_runner: local` mode, PHP runs on the host without network isolation. Docker mode is
  recommended.
* `composer audit` needs the network and runs only when dependencies change.
* PHPStan/Larastan isn't installed in this repository yet, so that gate is reported as *skipped*.
* Secrets are redacted from logs and state using pattern matching. Novel secret formats may not be
  recognised.
* The agent's own files must be present in the checkout you run it from (it lives on the branch
  where it was committed).
