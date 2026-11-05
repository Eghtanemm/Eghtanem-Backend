import unittest

from helpers import FakeGraphQL, FakeRunner, fail, make_config, make_issue, ok

from github_client import GitHubClient
from models import AuthError, GitHubError
from project_client import ProjectClient, parse_body_dependencies

SCHEMA = {
    "organization": {"projectV2": {
        "id": "PVT_1", "number": 1, "title": "Roadmap", "shortDescription": "desc", "url": "https://x/projects/1",
        "fields": {"nodes": [
            {"__typename": "ProjectV2Field", "id": "F_T", "name": "Title", "dataType": "TITLE"},
            {"__typename": "ProjectV2SingleSelectField", "id": "F_S", "name": "Status", "dataType": "SINGLE_SELECT",
             "options": [{"id": "o1", "name": "Backlog"}, {"id": "o2", "name": "In progress"},
                         {"id": "o3", "name": "In review"}, {"id": "o4", "name": "Done"}]},
            {"__typename": "ProjectV2SingleSelectField", "id": "F_Z", "name": "Size", "dataType": "SINGLE_SELECT",
             "options": [{"id": "s1", "name": "S"}]},
        ]},
    }}
}


def issue_item(n, status="Backlog", labels=("priority:P1",), body="## Dependencies\n- **Blocked by:** #99 (X-1)\n- **Related:** #7",
               blocked=(), prs=(), state="OPEN", size=None):
    values = [{"__typename": "ProjectV2ItemFieldSingleSelectValue", "name": status, "field": {"name": "Status"}}]
    if size:
        values.append({"__typename": "ProjectV2ItemFieldSingleSelectValue", "name": size, "field": {"name": "Size"}})
    return {
        "id": f"ITEM_{n}", "isArchived": False, "type": "ISSUE",
        "fieldValues": {"nodes": values},
        "content": {
            "__typename": "Issue", "number": n, "title": f"T{n}", "body": body, "url": f"https://x/issues/{n}",
            "state": state, "repository": {"nameWithOwner": "acme/app"},
            "labels": {"nodes": [{"name": l} for l in labels]},
            "blockedBy": {"nodes": [{"number": b, "state": s, "repository": {"nameWithOwner": "acme/app"}} for b, s in blocked]},
            "parent": None, "subIssues": {"nodes": []},
            "closedByPullRequestsReferences": {"nodes": list(prs)},
        },
    }


class Responder:
    def __init__(self, pages, open_prs=(), states=None):
        self.pages = pages
        self.open_prs = list(open_prs)
        self.states = states or {}

    def __call__(self, query, variables):
        if "organization(login" in query:
            return SCHEMA
        if "items(first" in query:
            index = 0 if variables.get("after") is None else int(variables["after"])
            nodes = self.pages[index]
            has_next = index + 1 < len(self.pages)
            return {"node": {"items": {"totalCount": 0, "nodes": nodes,
                                       "pageInfo": {"hasNextPage": has_next, "endCursor": str(index + 1) if has_next else None}}}}
        if "pullRequests(states:OPEN" in query:
            return {"repository": {"pullRequests": {"nodes": self.open_prs, "pageInfo": {"hasNextPage": False}}}}
        if "issueOrPullRequest" in query:
            return {"repository": {f"i{n}": {"state": s} for n, s in self.states.items()}}
        if "updateProjectV2ItemFieldValue" in query:
            return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": variables["item"]}}}
        raise AssertionError(f"unexpected query {query[:60]}")


def client(responder):
    gql = FakeGraphQL(responder)
    return ProjectClient(gql, "acme", 1, "acme/app", make_config()), gql


class SchemaTest(unittest.TestCase):
    def test_fields_and_options_discovered_dynamically(self):
        pc, _ = client(Responder([[]]))
        schema = pc.schema()
        self.assertEqual(schema.id, "PVT_1")
        status = schema.field("status")
        self.assertEqual([o.name for o in status.options], ["Backlog", "In progress", "In review", "Done"])
        # config says "In Progress"/"In Review"; project uses lower-case - matched case-insensitively
        self.assertEqual(pc.status_option("In Progress").id, "o2")
        self.assertEqual(pc.status_option("In Review").id, "o3")
        self.assertIsNone(pc.status_option("Blocked"))

    def test_missing_project_raises(self):
        pc, _ = client(lambda q, v: {"organization": {"projectV2": None}})
        with self.assertRaises(GitHubError):
            pc.schema()

    def test_schema_is_cached(self):
        pc, gql = client(Responder([[]]))
        pc.schema()
        pc.schema()
        self.assertEqual(sum(1 for q, _ in gql.queries if "organization(login" in q), 1)


class ItemsTest(unittest.TestCase):
    def test_pagination_collects_all_pages(self):
        pages = [[issue_item(1), issue_item(2)], [issue_item(3)], [issue_item(4)]]
        pc, gql = client(Responder(pages, states={99: "CLOSED"}))
        issues = pc.fetch_issues()
        self.assertEqual([i.number for i in issues], [1, 2, 3, 4])
        afters = [v.get("after") for q, v in gql.queries if "items(first" in q]
        self.assertEqual(afters, [None, "1", "2"])

    def test_parsing_priority_status_size_and_dependencies(self):
        pc, _ = client(Responder([[issue_item(1, blocked=[(5, "OPEN")], size="S"), issue_item(5)]], states={99: "OPEN"}))
        issue = {i.number: i for i in pc.fetch_issues()}[1]
        self.assertEqual(issue.priority, "P1")
        self.assertEqual(issue.status, "Backlog")
        self.assertEqual(issue.size, "S")
        self.assertEqual(sorted(b.number for b in issue.blocked_by), [5, 99])
        self.assertEqual({b.number: b.source for b in issue.blocked_by}, {5: "native", 99: "body"})
        self.assertEqual(issue.related, [7])
        self.assertEqual(len(issue.unresolved_blockers), 2)

    def test_body_blocker_resolved_from_project_state(self):
        pc, _ = client(Responder([[issue_item(1), issue_item(99, state="CLOSED", body="x")]]))
        issue = {i.number: i for i in pc.fetch_issues()}[1]
        self.assertEqual(issue.unresolved_blockers, [])

    def test_existing_pr_detection_from_closing_refs_branch_and_body(self):
        prs = [
            {"number": 10, "url": "u10", "state": "OPEN", "isDraft": True, "headRefName": "feature/x", "body": "",
             "closingIssuesReferences": {"nodes": [{"number": 1, "repository": {"nameWithOwner": "acme/app"}}]}},
            {"number": 11, "url": "u11", "state": "OPEN", "isDraft": True, "headRefName": "ai/fix-issue-2-something", "body": "",
             "closingIssuesReferences": {"nodes": []}},
            {"number": 12, "url": "u12", "state": "OPEN", "isDraft": False, "headRefName": "x", "body": "Refs #3",
             "closingIssuesReferences": {"nodes": []}},
        ]
        items = [issue_item(n, body="long enough body") for n in (1, 2, 3, 4)]
        pc, _ = client(Responder([items], open_prs=prs))
        issues = {i.number: i for i in pc.fetch_issues()}
        self.assertEqual([p.number for p in issues[1].active_prs], [10])
        self.assertEqual([p.number for p in issues[2].active_prs], [11])
        self.assertEqual([p.number for p in issues[3].active_prs], [12])
        self.assertEqual(issues[4].active_prs, [])

    def test_merged_pr_reference_is_not_active(self):
        merged = {"number": 5, "url": "u", "state": "MERGED", "isDraft": False, "headRefName": "x"}
        pc, _ = client(Responder([[issue_item(1, prs=[merged], body="b")]]))
        issue = pc.fetch_issues()[0]
        self.assertEqual(len(issue.linked_prs), 1)
        self.assertEqual(issue.active_prs, [])

    def test_draft_issue_items_are_typed(self):
        draft = {"id": "I", "isArchived": False, "type": "DRAFT_ISSUE", "fieldValues": {"nodes": []},
                 "content": {"__typename": "DraftIssue", "title": "idea"}}
        pc, _ = client(Responder([[draft]]))
        self.assertEqual(pc.fetch_issues()[0].content_type, "DraftIssue")


class StatusUpdateTest(unittest.TestCase):
    def test_set_status_uses_discovered_ids(self):
        pc, gql = client(Responder([[]]))
        issue = make_issue(1)
        name = pc.set_status(issue, "In Review")
        self.assertEqual(name, "In review")
        mutation = [v for q, v in gql.queries if "updateProjectV2ItemFieldValue" in q][0]
        self.assertEqual(mutation, {"project": "PVT_1", "item": "ITEM_1", "field": "F_S", "option": "o3"})

    def test_never_invents_missing_option(self):
        pc, gql = client(Responder([[]]))
        with self.assertRaises(GitHubError):
            pc.set_status(make_issue(1), "Blocked")
        self.assertFalse(any("updateProjectV2ItemFieldValue" in q for q, _ in gql.queries))


class BodyDependencyParsingTest(unittest.TestCase):
    def test_formats(self):
        body = "## Dependencies\n- **Blocked by:** #6 (SEC-05), #8\n- **Related:** #18 (AUTH-02), #19\n- **Sub-issues:** #36\nDepends on #3"
        blocked, related = parse_body_dependencies(body)
        self.assertEqual(blocked, [6, 8, 3])
        self.assertEqual(related, [18, 19])


class GitHubClientRetryTest(unittest.TestCase):
    def test_retries_transient_errors(self):
        runner = FakeRunner()
        attempts = {"n": 0}

        def flaky(args, **kw):
            attempts["n"] += 1
            return fail("HTTP 502: Bad Gateway") if attempts["n"] < 3 else ok('{"data": {"viewer": {"login": "me"}}}')

        runner.on(lambda a: a[:3] == ["gh", "api", "graphql"], flaky)
        gh = GitHubClient(runner, max_retries=5, sleep=lambda s: None)
        self.assertEqual(gh.viewer_login(), "me")
        self.assertEqual(attempts["n"], 3)

    def test_rate_limit_waits_then_succeeds(self):
        runner = FakeRunner()
        sleeps = []
        responses = [fail("API rate limit exceeded"), ok('{"data": {"viewer": {"login": "me"}}}')]
        runner.on(lambda a: True, lambda a, **k: responses.pop(0))
        gh = GitHubClient(runner, sleep=sleeps.append)
        self.assertEqual(gh.viewer_login(), "me")
        self.assertGreaterEqual(sleeps[0], 60)

    def test_auth_and_scope_failures_are_systemic(self):
        runner = FakeRunner().on(lambda a: True, fail("gh auth login required: not logged in"))
        with self.assertRaises(AuthError):
            GitHubClient(runner, sleep=lambda s: None).viewer_login()
        runner = FakeRunner().on(lambda a: True, ok('{"errors": [{"type": "INSUFFICIENT_SCOPES", "message": "needs read:project"}]}'))
        with self.assertRaises(AuthError):
            GitHubClient(runner, sleep=lambda s: None).graphql("query{x}")

    def test_permanent_error_is_not_retried(self):
        runner = FakeRunner().on(lambda a: True, fail("GraphQL: Could not resolve to a Repository"))
        gh = GitHubClient(runner, sleep=lambda s: None)
        with self.assertRaises(GitHubError):
            gh.viewer_login()
        self.assertEqual(len(runner.calls), 1)


if __name__ == "__main__":
    unittest.main()
