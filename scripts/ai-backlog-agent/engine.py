"""The sequential backlog engine.

    loop:
        fetch fresh project data
        evaluate eligibility (excludes: attempted this run, failed, existing PRs,
                              blocked, human-required, in progress, completed)
        pick the highest-priority eligible issue
        process it completely (one issue at a time, never concurrently)
    until no eligible issue remains (or --limit / --once is reached)

Isolated issue failures are recorded and the loop moves on; systemic failures
stop the batch immediately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from issue_selector import IssueSelector, SelectionContext
from models import Eligibility, ExecutionStatus, Issue, IssueOutcome, SystemicError


@dataclass
class RunOptions:
    limit: int | None = None  # None = unlimited (default)
    issue: int | None = None
    resume: bool = False
    retry_failed: bool = False


@dataclass
class RunResult:
    outcomes: list[IssueOutcome] = field(default_factory=list)
    initial_issues: list[Issue] = field(default_factory=list)
    initial_evaluations: list[Eligibility] = field(default_factory=list)
    final_issues: list[Issue] = field(default_factory=list)
    final_evaluations: list[Eligibility] = field(default_factory=list)
    systemic_error: str | None = None
    stopped_reason: str = ""
    interrupted: bool = False


class Engine:
    def __init__(self, *, config, project, selector: IssueSelector, processor, state, logger, repository: str):
        self.config = config
        self.project = project
        self.selector = selector
        self.processor = processor
        self.state = state
        self.log = logger
        self.repository = repository

    def context(self, attempted: set[int], retry_failed: bool) -> SelectionContext:
        summary = self.state.issue_summary()
        return SelectionContext(
            repository=self.repository,
            executions=summary,
            attempts={n: self.state.attempts(n) for n in summary},
            locked=set(self.state.locked_issues()),
            attempted_this_run=set(attempted),
            retry_failed=retry_failed,
        )

    def _filter(self, issues: list[Issue], opts: RunOptions) -> list[Issue]:
        if opts.issue is not None:
            return [i for i in issues if i.number == opts.issue]
        if opts.retry_failed:
            summary = self.state.issue_summary()
            return [i for i in issues if (summary.get(i.number) or {}).get("status") == ExecutionStatus.FAILED.value]
        return issues

    def run(self, opts: RunOptions) -> RunResult:
        result = RunResult()
        attempted: set[int] = set()

        def budget_left() -> bool:
            return opts.limit is None or len(attempted) < opts.limit

        try:
            if opts.resume:
                for record in self.state.interrupted():
                    if not budget_left():
                        break
                    if record["issue_number"] in attempted:
                        continue
                    attempted.add(record["issue_number"])
                    result.outcomes.append(self.processor.resume(record))

            iterations = 0
            while budget_left():
                iterations += 1
                self.log.info("Fetching GitHub Project..." if iterations == 1 else "Fetching next eligible issue...")
                issues = self.project.fetch_issues()
                if iterations == 1:
                    result.initial_issues = issues
                    result.initial_evaluations = self.selector.evaluate_all(issues, self.context(set(), opts.retry_failed))
                    backlog = sum(1 for i in issues if (i.status or "").lower() == self.config.project_status.backlog.lower() and i.is_open)
                    eligible = sum(1 for e in result.initial_evaluations if e.eligible)
                    self.log.info(f"Found {len(issues)} project items, {backlog} open in Backlog, {eligible} eligible.")
                if iterations > len(issues) + 10:  # hard guard against an unexpected infinite loop
                    raise SystemicError("loop guard tripped: more iterations than project items")

                candidates = self._filter(issues, opts)
                chosen, evaluations = self.selector.select_next(candidates, self.context(attempted, opts.retry_failed))
                if opts.issue is not None and chosen is None:
                    reason = next((", ".join(e.reasons) for e in evaluations), "not found in the project")
                    self.log.warning(f"Issue #{opts.issue} is not eligible: {reason}")
                    result.stopped_reason = f"issue #{opts.issue} not eligible: {reason}"
                    break
                if chosen is None:
                    result.stopped_reason = "no eligible issues remain"
                    self.log.info("No eligible issues remain.")
                    break
                if chosen.issue.number in attempted:
                    raise SystemicError(f"selector returned #{chosen.issue.number} twice in one run")
                attempted.add(chosen.issue.number)

                outcome = self.processor.process(chosen)
                result.outcomes.append(outcome)
                if outcome.status == ExecutionStatus.FAILED and not self.config.execution.continue_after_isolated_failure:
                    result.stopped_reason = "stopped after isolated failure (continue_after_isolated_failure=false)"
                    break
                if opts.issue is not None:
                    result.stopped_reason = f"processed requested issue #{opts.issue}"
                    break
            else:
                result.stopped_reason = f"limit of {opts.limit} issue(s) reached"
        except SystemicError as exc:
            result.systemic_error = f"{type(exc).__name__}: {exc}"
            result.stopped_reason = "systemic failure"
            self.log.error(f"SYSTEMIC FAILURE - stopping the batch: {exc}")
        except KeyboardInterrupt:
            result.interrupted = True
            result.stopped_reason = "interrupted (resume with --resume)"
            self.log.error("Interrupted. State saved; continue later with --resume.")

        # Verify the final project state for the report (best effort; never hides the original error).
        try:
            result.final_issues = self.project.fetch_issues()
            result.final_evaluations = self.selector.evaluate_all(result.final_issues, self.context(set(), False))
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"Could not refresh final project state: {exc}")
            result.final_issues, result.final_evaluations = result.initial_issues, result.initial_evaluations
        return result
