"""Work items across projects, for an edition that cannot list a whole workspace.

Plane Community Edition v1.4.2 answers `work-items/` at workspace level with 404
and rejects PQL with 400, so "my open issues, everywhere" has no server-side
form. This walks the projects and pages through each one, filtering here.

Two things keep that honest:

* A project the caller cannot read answers 403. It is reported in
  `skipped_projects` and the walk goes on, rather than one closed project
  discarding every readable one.
* The walk is bounded. Plane rate-limits an API key (`API_KEY_RATE_LIMIT`, 60 a
  minute in this deployment), so it spends at most `MAX_REQUESTS` calls of its own
  and stops on a 429. `truncated` says why, `unscanned_projects` and
  `partial_projects` say what is missing, and `complete` is the one flag to
  trust. A partial answer that does not say it is partial reads as a complete one.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from fastmcp.utilities.logging import get_logger
from plane.errors.errors import HttpError
from plane.models.query_params import ProjectLiteListQueryParams, WorkItemQueryParams

from plane_mcp.toolkit.paging import dump_results
from plane_mcp.toolkit.runtime import ids_of

PAGE = 100
MAX_REQUESTS = 50
MAX_MATCHES = 500

# What a hit always carries: the filters need `state` and `assignees`, and the rest is
# enough to read a result list without a second call per item. A caller's `fields` come on top.
LEAN_FIELDS = ("id", "sequence_id", "name", "state", "priority", "project", "assignees")


UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

logger = get_logger(__name__)


@dataclass
class Scan:
    results: list[dict[str, Any]] = field(default_factory=list)
    by_project: Counter[str] = field(default_factory=Counter)
    scanned_projects: list[str] = field(default_factory=list)
    skipped_projects: list[str] = field(default_factory=list)
    partial_projects: list[str] = field(default_factory=list)
    unscanned_projects: list[str] = field(default_factory=list)
    unclassified: int = 0
    requests: int = 0
    truncated: str | None = None

    @property
    def complete(self) -> bool:
        return not (self.truncated or self.skipped_projects or self.unclassified)

    def _coverage(self) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "projects_scanned": len(self.scanned_projects),
            "skipped_projects": self.skipped_projects,
            "partial_projects": self.partial_projects,
            "unscanned_projects": self.unscanned_projects,
            "unclassified": self.unclassified,
            "truncated": self.truncated,
        }

    def listing(self) -> dict[str, Any]:
        return {"results": self.results, "returned": len(self.results)} | self._coverage()

    def counts(self) -> dict[str, Any]:
        found = sum(self.by_project.values())
        label = "count" if self.complete else "count_at_least"
        return {label: found, "by_project": dict(self.by_project)} | self._coverage()


class _Stop(Exception):
    """The walk reached a limit; what it has so far is the answer."""


def _spend(scan: Scan) -> None:
    if scan.requests >= MAX_REQUESTS:
        raise _Stop(
            f"stopped after {MAX_REQUESTS} Plane API calls (a budget of this tool, to stay under the "
            "API key's rate limit); pass project_id to scan one project"
        )
    scan.requests += 1


def _next(page: Any, what: str) -> str | None:
    """The cursor of the next page, None on the last; a promised page with no cursor is an error."""
    if not page.next_page_results:
        return None
    if not page.next_cursor:
        raise _Stop(f"Plane promised another page of {what} but sent no cursor")
    return page.next_cursor


def _projects(client: Any, workspace_slug: str, scan: Scan) -> list[tuple[str, str]]:
    """(id, identifier) of every project the workspace lists, readable or not."""
    found: list[tuple[str, str]] = []
    cursor = None
    while True:
        _spend(scan)
        page = client.projects.list_lite(
            workspace_slug=workspace_slug,
            params=ProjectLiteListQueryParams(per_page=PAGE, cursor=cursor, include_archived=False),
        )
        found += [(p.id, p.identifier) for p in page.results]
        cursor = _next(page, "projects")
        if cursor is None:
            return found


def _group(item: Any) -> str | None:
    return getattr(getattr(item, "state", None), "group", None)


def valid_assignee(value: str) -> bool:
    return value == "me" or bool(UUID.fullmatch(value))


def resolve_me(client: Any) -> str | None:
    return client.users.get_me().id or None


def scan_work_items(
    client: Any,
    workspace_slug: str,
    *,
    project_id: str = "",
    assignee_id: str | None = None,
    state_groups: list[str] | None = None,
    fields: str = "",
    capped: bool = True,
) -> Scan | str:
    """Matching work items of every readable project, or of `project_id` alone.

    Returns an error string when nothing could be read at all: `project_id` is
    unknown, or every project refused the caller.
    """
    scan = Scan()
    wanted = ",".join(dict.fromkeys([*LEAN_FIELDS, *(f.strip() for f in fields.split(",") if f.strip())]))
    projects: list[tuple[str, str]] = []
    current: str | None = None
    try:
        projects = _projects(client, workspace_slug, scan)
        if project_id:
            projects = [p for p in projects if project_id.lower() in (p[0].lower(), p[1].lower())]
            if not projects:
                return f"Error: project {project_id!r} is not an active project of this workspace."
        for index, (pid, identifier) in enumerate(projects):
            current = None
            cursor = None
            try:
                while True:
                    _spend(scan)
                    page = client.work_items.list(
                        workspace_slug=workspace_slug,
                        project_id=pid,
                        params=WorkItemQueryParams(per_page=PAGE, cursor=cursor, expand="state", fields=wanted),
                    )
                    current = identifier
                    for item in page.results:
                        if assignee_id and assignee_id not in ids_of(getattr(item, "assignees", None)):
                            continue
                        if state_groups and _group(item) is None:
                            scan.unclassified += 1
                        elif not state_groups or _group(item) in state_groups:
                            if capped and len(scan.results) >= MAX_MATCHES:
                                scan.truncated = f"stopped at {MAX_MATCHES} matches; add a filter or pass project_id"
                                scan.partial_projects.append(identifier)
                                scan.unscanned_projects = [i for _, i in projects[index + 1 :]]
                                return _finish(scan)
                            if capped:
                                row = dump_results([item], wanted)[0]
                                scan.results.append({"identifier": f"{identifier}-{item.sequence_id}", **row})
                            scan.by_project[identifier] += 1
                    cursor = _next(page, f"work items of {identifier}")
                    if cursor is None:
                        break
            except HttpError as exc:
                if exc.status_code not in (403, 404):
                    raise
                scan.skipped_projects.append(identifier)
                continue
            scan.scanned_projects.append(identifier)
            current = None
    except _Stop as stop:
        scan.truncated = str(stop)
    except HttpError as exc:
        if exc.status_code != 429:
            raise
        scan.truncated = "Plane answered 429 (rate limit); the result is partial, retry in a minute"
    if scan.truncated:
        if current and current not in scan.skipped_projects:
            scan.partial_projects.append(current)
        done = {*scan.scanned_projects, *scan.skipped_projects, *scan.partial_projects}
        scan.unscanned_projects = [i for _, i in projects if i not in done]
    return _finish(scan)


def _finish(scan: Scan) -> Scan | str:
    if scan.skipped_projects and not scan.scanned_projects and not scan.partial_projects:
        return f"Error: no access to {', '.join(scan.skipped_projects)} (403 or 404); nothing could be read."
    if scan.truncated or scan.skipped_projects or scan.unclassified:
        logger.warning(
            "cross-project scan incomplete: truncated=%r skipped=%s unclassified=%d requests=%d",
            scan.truncated,
            scan.skipped_projects,
            scan.unclassified,
            scan.requests,
        )
    return scan
