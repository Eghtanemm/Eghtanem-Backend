"""JSON Schemas passed to ``claude --json-schema`` for each stage's structured output."""

from __future__ import annotations

_STR = {"type": "string"}
_BOOL = {"type": "boolean"}
_STR_LIST = {"type": "array", "items": {"type": "string"}}

ANALYSIS = {
    "type": "object",
    "properties": {
        "audit_ref": _STR,
        "problem": _STR,
        "root_cause": _STR,
        "expected_behavior": _STR,
        "acceptance_criteria": _STR_LIST,
        "severity": _STR,
        "dependencies": _STR_LIST,
        "security_impact": _STR,
        "performance_impact": _STR,
        "affected_components": _STR_LIST,
        "verification": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["verified", "not_reproducible", "uncertain"]},
                "evidence": _STR_LIST,
                "reproduction_steps": _STR_LIST,
            },
            "required": ["status", "evidence", "reproduction_steps"],
        },
        "fix_necessary": _BOOL,
        "requires_human": _BOOL,
        "human_reasons": _STR_LIST,
        "sufficient_information": _BOOL,
        "fully_resolvable": _BOOL,
        "summary": _STR,
    },
    "required": ["problem", "root_cause", "acceptance_criteria", "verification", "fix_necessary",
                 "requires_human", "human_reasons", "sufficient_information", "fully_resolvable", "summary"],
}

PLAN = {
    "type": "object",
    "properties": {
        "root_cause": _STR,
        "affected_files": _STR_LIST,
        "proposed_solution": _STR,
        "database_changes": _STR,
        "api_compatibility": _STR,
        "security_implications": _STR,
        "required_tests": _STR_LIST,
        "risks": _STR_LIST,
        "rollback": _STR,
        "fully_resolves_issue": _BOOL,
        "unaddressed_criteria": _STR_LIST,
        "requires_human": _BOOL,
        "human_reasons": _STR_LIST,
        "commit_summary": _STR,
    },
    "required": ["root_cause", "affected_files", "proposed_solution", "database_changes", "api_compatibility",
                 "security_implications", "required_tests", "risks", "rollback", "fully_resolves_issue",
                 "requires_human", "human_reasons", "commit_summary"],
}

IMPLEMENTATION = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["completed", "partial", "blocked"]},
        "root_cause": _STR,
        "files_modified": _STR_LIST,
        "changes_made": _STR_LIST,
        "tests_added": _STR_LIST,
        "tests_run": _STR_LIST,
        "known_limitations": _STR_LIST,
        "potential_risks": _STR_LIST,
        "database_changes": _STR,
        "fully_resolves_issue": _BOOL,
        "requires_human": _BOOL,
        "human_reasons": _STR_LIST,
        "commit_summary": _STR,
    },
    "required": ["status", "root_cause", "files_modified", "changes_made", "tests_added", "known_limitations",
                 "potential_risks", "fully_resolves_issue", "requires_human", "human_reasons", "commit_summary"],
}

REVIEW = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "request_changes", "reject"]},
        "summary": _STR,
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                    "title": _STR,
                    "description": _STR,
                    "file": _STR,
                    "line": {"type": "integer"},
                    "suggestion": _STR,
                },
                "required": ["severity", "title", "description"],
            },
        },
        "fully_resolves_issue": _BOOL,
        "scope_creep": _BOOL,
        "tests_adequate": _BOOL,
    },
    "required": ["verdict", "summary", "findings", "fully_resolves_issue", "scope_creep", "tests_adequate"],
}

INVESTIGATION = {
    "type": "object",
    "properties": {
        "summary": _STR,
        "current_state": _STR,
        "code_changes_recommended": _STR_LIST,
        "human_actions_required": _STR_LIST,
        "risks": _STR_LIST,
        "verification_steps": _STR_LIST,
    },
    "required": ["summary", "current_state", "code_changes_recommended", "human_actions_required", "risks",
                 "verification_steps"],
}
