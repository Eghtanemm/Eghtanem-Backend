"""GitHub Projects (v2) integration.

Everything is discovered dynamically: project id, fields, single-select
options, items (paginated), dependencies and linked pull requests. Nothing is
hard-coded except the organization login and project number from config.
"""

from __future__ import annotations

import re
from typing import Any

from github_client import GitHubClient
from models import FieldOption, GitHubError, Issue, IssueRef, ProjectField, ProjectSchema, PullRef

SCHEMA_QUERY = """
query($org:String!, $number:Int!) {
  organization(login:$org) {
    projectV2(number:$number) {
      id number title shortDescription url closed
      fields(first:100) {
        nodes {
          __typename
          ... on ProjectV2FieldCommon { id name dataType }
          ... on ProjectV2SingleSelectField { options { id name } }
        }
      }
    }
  }
}
"""

ISSUE_FIELDS = """
  number title body url state
  repository { nameWithOwner }
  labels(first:50) { nodes { name } }
  blockedBy(first:30) { nodes { number state repository { nameWithOwner } } }
  parent { number }
  subIssues(first:50) { nodes { number state repository { nameWithOwner } } }
  closedByPullRequestsReferences(first:20, includeClosedPrs:true) {
    nodes { number url state isDraft headRefName }
  }
"""

FIELD_VALUES = """
  fieldValues(first:50) {
    nodes {
      __typename
      ... on ProjectV2ItemFieldSingleSelectValue { name optionId field { ... on ProjectV2FieldCommon { name } } }
      ... on ProjectV2ItemFieldTextValue { text field { ... on ProjectV2FieldCommon { name } } }
      ... on ProjectV2ItemFieldNumberValue { number field { ... on ProjectV2FieldCommon { name } } }
      ... on ProjectV2ItemFieldIterationValue { title field { ... on ProjectV2FieldCommon { name } } }
    }
  }
"""

ITEMS_QUERY = (
    """
query($id:ID!, $first:Int!, $after:String) {
  node(id:$id) {
    ... on ProjectV2 {
      items(first:$first, after:$after) {
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes {
          id isArchived type
"""
    + FIELD_VALUES
    + """
          content {
            __typename
            ... on Issue { """
    + ISSUE_FIELDS
    + """ }
            ... on PullRequest { number title url state }
            ... on DraftIssue { title }
          }
        }
      }
    }
  }
}
"""
)

SINGLE_ISSUE_QUERY = (
    """
query($owner:String!, $name:String!, $number:Int!) {
  repository(owner:$owner, name:$name) {
    issue(number:$number) {
"""
    + ISSUE_FIELDS
    + """
      projectItems(first:20, includeArchived:false) {
        nodes { id project { id } """
    + FIELD_VALUES
    + """ }
      }
    }
  }
}
"""
)

OPEN_PRS_QUERY = """
query($owner:String!, $name:String!, $after:String) {
  repository(owner:$owner, name:$name) {
    pullRequests(states:OPEN, first:100, after:$after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number url state isDraft headRefName body
        closingIssuesReferences(first:20) { nodes { number repository { nameWithOwner } } }
      }
    }
  }
}
"""

UPDATE_STATUS_MUTATION = """
mutation($project:ID!, $item:ID!, $field:ID!, $option:String!) {
  updateProjectV2ItemFieldValue(input:{projectId:$project, itemId:$item, fieldId:$field,
                                       value:{singleSelectOptionId:$option}}) {
    projectV2Item { id }
  }
}
"""

AGENT_BRANCH = re.compile(r"^ai/fix-issue-(\d+)-")
PR_REFERENCE = re.compile(
    r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|refs?|references|part of|addresses|related to)\s*:?\s+#(\d+)\b"
)
DEP_LINE = re.compile(r"(?im)^\s*[-*]?\s*\**\s*(blocked by|depends on|requires|related|see also)\s*:?\**\s*:?\s*(.*)$")


def parse_body_dependencies(body: str) -> tuple[list[int], list[int]]:
    """Return (blocked_by, related) issue numbers declared in the issue body."""
    blocked: list[int] = []
    related: list[int] = []
    for match in DEP_LINE.finditer(body or ""):
        kind = match.group(1).lower()
        numbers = [int(n) for n in re.findall(r"#(\d+)", match.group(2))]
        target = blocked if kind in ("blocked by", "depends on", "requires") else related
        for n in numbers:
            if n not in target:
                target.append(n)
    return blocked, related


def _field_values(item: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for node in (item.get("fieldValues") or {}).get("nodes") or []:
        if not node:
            continue
        field_name = ((node.get("field") or {}).get("name")) or ""
        if not field_name:
            continue
        for key in ("name", "text", "number", "title"):
            if key in node and node[key] is not None:
                values[field_name] = node[key]
                break
    return values


class ProjectClient:
    def __init__(self, gh: GitHubClient, org: str, number: int, repository: str, config, logger=None):
        self.gh = gh
        self.org = org
        self.number = number
        self.repository = repository  # owner/name
        self.config = config  # full Config
        self.logger = logger
        self._schema: ProjectSchema | None = None

    # ---------------------------------------------------------------- schema

    def schema(self, refresh: bool = False) -> ProjectSchema:
        if self._schema is None or refresh:
            data = self.gh.graphql(SCHEMA_QUERY, {"org": self.org, "number": self.number})
            project = ((data.get("organization") or {}).get("projectV2"))
            if not project:
                raise GitHubError(
                    f"Project {self.org}#{self.number} not found or not accessible "
                    "(check membership and the 'project' token scope)"
                )
            self._schema = self.parse_schema(project)
        return self._schema

    @staticmethod
    def parse_schema(project: dict[str, Any]) -> ProjectSchema:
        fields: dict[str, ProjectField] = {}
        for node in (project.get("fields") or {}).get("nodes") or []:
            if not node or not node.get("name"):
                continue
            fields[node["name"]] = ProjectField(
                id=node["id"],
                name=node["name"],
                data_type=node.get("dataType") or node.get("__typename", ""),
                options=[FieldOption(o["id"], o["name"]) for o in node.get("options") or []],
            )
        return ProjectSchema(
            id=project["id"],
            number=project["number"],
            title=project.get("title") or "",
            description=project.get("shortDescription") or "",
            url=project.get("url") or "",
            fields=fields,
        )

    # ----------------------------------------------------------------- items

    def fetch_items(self) -> list[dict[str, Any]]:
        schema = self.schema()
        items: list[dict[str, Any]] = []
        after = None
        while True:
            data = self.gh.graphql(
                ITEMS_QUERY, {"id": schema.id, "first": self.config.github.page_size, "after": after}
            )
            page = ((data.get("node") or {}).get("items")) or {}
            items.extend(n for n in page.get("nodes") or [] if n)
            info = page.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                break
            after = info.get("endCursor")
        return items

    def fetch_open_prs(self) -> list[dict[str, Any]]:
        owner, name = self.repository.split("/", 1)
        prs: list[dict[str, Any]] = []
        after = None
        while True:
            data = self.gh.graphql(OPEN_PRS_QUERY, {"owner": owner, "name": name, "after": after})
            page = (data.get("repository") or {}).get("pullRequests") or {}
            prs.extend(n for n in page.get("nodes") or [] if n)
            info = page.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                break
            after = info.get("endCursor")
        return prs

    def open_prs_by_issue(self, prs: list[dict[str, Any]]) -> dict[int, list[PullRef]]:
        """Map issue number -> open PRs that close it, reference it, or use the agent's branch for it."""
        mapping: dict[int, list[PullRef]] = {}

        def add(number: int, pr: dict[str, Any]) -> None:
            ref = PullRef(pr["number"], pr["url"], pr.get("state", "OPEN"), pr.get("isDraft", False), pr.get("headRefName", ""))
            bucket = mapping.setdefault(number, [])
            if all(existing.number != ref.number for existing in bucket):
                bucket.append(ref)

        for pr in prs:
            for node in (pr.get("closingIssuesReferences") or {}).get("nodes") or []:
                repo = (node.get("repository") or {}).get("nameWithOwner", self.repository)
                if repo.lower() == self.repository.lower():
                    add(node["number"], pr)
            branch = AGENT_BRANCH.match(pr.get("headRefName") or "")
            if branch:
                add(int(branch.group(1)), pr)
            for n in PR_REFERENCE.findall(pr.get("body") or ""):
                add(int(n), pr)
        return mapping

    def parse_item(self, item: dict[str, Any], open_prs: dict[int, list[PullRef]] | None = None) -> Issue | None:
        content = item.get("content") or {}
        typename = content.get("__typename") or item.get("type") or ""
        values = _field_values(item)
        status = values.get("Status")
        if typename != "Issue":
            return Issue(
                number=content.get("number") or 0,
                title=content.get("title") or "",
                body="",
                url=content.get("url") or "",
                state=content.get("state") or "UNKNOWN",
                repository="",
                item_id=item.get("id", ""),
                status=status,
                content_type=typename or "Unknown",
                field_values=values,
            )
        return self._build_issue(content, item.get("id", ""), values, open_prs or {})

    def _build_issue(self, content: dict[str, Any], item_id: str, values: dict[str, Any],
                     open_prs: dict[int, list[PullRef]]) -> Issue:
        sel = self.config.selection
        labels = [l["name"] for l in (content.get("labels") or {}).get("nodes") or [] if l]
        repo = (content.get("repository") or {}).get("nameWithOwner", "")

        blocked: list[IssueRef] = []
        for node in (content.get("blockedBy") or {}).get("nodes") or []:
            if node:
                blocked.append(IssueRef(node["number"], node.get("state", "UNKNOWN"),
                                        (node.get("repository") or {}).get("nameWithOwner", repo), "native"))
        body_blocked, related = parse_body_dependencies(content.get("body") or "")
        known = {b.number for b in blocked}
        for n in body_blocked:
            if n not in known:
                blocked.append(IssueRef(n, "UNKNOWN", repo, "body"))

        prs: list[PullRef] = []
        for node in (content.get("closedByPullRequestsReferences") or {}).get("nodes") or []:
            if node:
                prs.append(PullRef(node["number"], node["url"], node.get("state", ""), node.get("isDraft", False),
                                   node.get("headRefName", "")))
        for pr in open_prs.get(content["number"], []):
            if all(p.number != pr.number for p in prs):
                prs.append(pr)

        priority = self._priority(values, labels, sel)
        return Issue(
            number=content["number"],
            title=content.get("title") or "",
            body=content.get("body") or "",
            url=content.get("url") or "",
            state=content.get("state") or "UNKNOWN",
            repository=repo,
            item_id=item_id,
            status=values.get("Status"),
            labels=labels,
            priority=priority,
            size=values.get(sel.size_field),
            blocked_by=blocked,
            related=related,
            sub_issues=[IssueRef(s["number"], s.get("state", "UNKNOWN"), (s.get("repository") or {}).get("nameWithOwner", repo), "native")
                        for s in (content.get("subIssues") or {}).get("nodes") or [] if s],
            parent=(content.get("parent") or {}).get("number"),
            linked_prs=prs,
            field_values=values,
        )

    @staticmethod
    def _priority(values: dict[str, Any], labels: list[str], sel) -> str | None:
        field_value = values.get(sel.priority_field)
        if field_value:
            match = re.search(r"P\d", str(field_value), re.IGNORECASE)
            if match:
                return match.group(0).upper()
        found = []
        for label in labels:
            match = re.match(sel.priority_label_pattern, label.strip(), re.IGNORECASE)
            if match:
                found.append(match.group(1).upper())
        return sorted(found)[0] if found else None  # most urgent if several

    def fetch_issues(self) -> list[Issue]:
        """Fresh snapshot of all project items as Issue objects (dependency states resolved)."""
        items = self.fetch_items()
        open_prs = self.open_prs_by_issue(self.fetch_open_prs())
        issues = [i for i in (self.parse_item(item, open_prs) for item in items if not item.get("isArchived")) if i]
        self._resolve_dependency_states(issues)
        return issues

    def _resolve_dependency_states(self, issues: list[Issue]) -> None:
        by_number = {i.number: i for i in issues if i.content_type == "Issue"}
        unknown: set[int] = set()
        for issue in issues:
            for ref in issue.blocked_by:
                if ref.state == "UNKNOWN":
                    if ref.number in by_number:
                        ref.state = by_number[ref.number].state
                    else:
                        unknown.add(ref.number)
        if not unknown:
            return
        states = self.fetch_issue_states(sorted(unknown))
        for issue in issues:
            for ref in issue.blocked_by:
                if ref.state == "UNKNOWN":
                    ref.state = states.get(ref.number, "UNKNOWN")

    def fetch_issue_states(self, numbers: list[int]) -> dict[int, str]:
        owner, name = self.repository.split("/", 1)
        result: dict[int, str] = {}
        for start in range(0, len(numbers), 50):
            chunk = numbers[start:start + 50]
            aliases = " ".join(f"i{n}: issueOrPullRequest(number:{int(n)}) {{ ... on Issue {{ state }} ... on PullRequest {{ state }} }}" for n in chunk)
            data = self.gh.graphql(
                f"query($owner:String!,$name:String!){{ repository(owner:$owner,name:$name){{ {aliases} }} }}",
                {"owner": owner, "name": name},
            )
            repo = data.get("repository") or {}
            for n in chunk:
                node = repo.get(f"i{n}") or {}
                result[n] = node.get("state", "UNKNOWN")
        return result

    def refresh_issue(self, number: int) -> Issue | None:
        """Fetch one issue fresh (used right before claiming). Returns None if not in the project."""
        owner, name = self.repository.split("/", 1)
        data = self.gh.graphql(SINGLE_ISSUE_QUERY, {"owner": owner, "name": name, "number": number})
        content = (data.get("repository") or {}).get("issue")
        if not content:
            return None
        project_id = self.schema().id
        item = next(
            (n for n in (content.get("projectItems") or {}).get("nodes") or [] if n and (n.get("project") or {}).get("id") == project_id),
            None,
        )
        if not item:
            return None
        open_prs = self.open_prs_by_issue(self.fetch_open_prs())
        issue = self._build_issue(content, item["id"], _field_values(item), open_prs)
        self._resolve_dependency_states([issue])
        return issue

    # --------------------------------------------------------------- updates

    def status_option(self, name: str | None) -> FieldOption | None:
        field = self.schema().field("Status")
        return field.option(name) if field else None

    def set_status(self, issue: Issue, option_name: str) -> str:
        """Set the Status single-select. Returns the actual option name used. Never creates options."""
        schema = self.schema()
        field = schema.field("Status")
        if not field:
            raise GitHubError("Project has no 'Status' field")
        option = field.option(option_name)
        if not option:
            raise GitHubError(f"Status option '{option_name}' does not exist in the project")
        self.gh.graphql(
            UPDATE_STATUS_MUTATION,
            {"project": schema.id, "item": issue.item_id, "field": field.id, "option": option.id},
        )
        issue.status = option.name
        return option.name
