"""Relations between work items.

Uses the classic `work-items/<id>/relations/` endpoint, which Plane Community
Edition v1.4.2 serves. On that version the newer `dependencies/`,
`work-item-relations/` and `work-item-relation-definitions/` endpoints answer 404,
so custom relation definitions are not offered. Its public API also answers 404 on
the `relations/remove` call the SDK offers (observed against a live instance; only
the session API behind the web UI removes a relation), so there is no delete action.
"""

from __future__ import annotations

from typing import Any, Literal, get_args

from fastmcp import FastMCP
from plane.models.enums import WorkItemRelationTypeEnum
from plane.models.work_items import CreateWorkItemRelation

from plane_mcp.client import get_plane_client_context
from plane_mcp.toolkit import (
    Action,
    build_annotations,
    build_description,
    coerce_list,
    missing,
    needs,
    one_of,
)

NAME = "workitem_relation"
TITLE = "Work item relations"

RELATION_TYPES: tuple[str, ...] = get_args(WorkItemRelationTypeEnum)

ACTIONS = (
    Action("list", ("project_id", "workitem_id"), read=True),
    Action("create", ("project_id", "workitem_id", "workitem_ids", "relation_type")),
)

FOOTER = (
    f"relation_type is one of {', '.join(RELATION_TYPES)}. It is read from the point of view "
    "of workitem_id: blocked_by means workitem_id is blocked by every id in workitem_ids; "
    "blocking means it blocks them. list returns the related ids grouped by relation type. "
    "This tool cannot remove a relation; do that in the Plane web UI."
)

LEGACY = {
    "list_work_item_relations": "list",
    "create_work_item_relation": "create",
}

_NO_DEFINITIONS = (
    "Custom relation definitions are not offered: Plane Community Edition v1.4.2 has no endpoint for them."
)

LEGACY_UNMAPPED = {
    "remove_work_item_relation": (
        "Plane Community Edition v1.4.2 offers no way to remove a relation through its public API, "
        "and workitem_relation has no delete action: use the Plane web UI."
    ),
    "list_work_item_relation_definitions": _NO_DEFINITIONS,
    "create_work_item_relation_definition": _NO_DEFINITIONS,
    "update_work_item_relation_definition": _NO_DEFINITIONS,
    "delete_work_item_relation_definition": _NO_DEFINITIONS,
}


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name=NAME,
        description=build_description("Relations between work items.", ACTIONS, FOOTER),
        annotations=build_annotations(TITLE, ACTIONS),
    )
    def workitem_relation(
        action: Literal["list", "create"],
        project_id: str = "",
        workitem_id: str = "",
        workitem_ids: list[str] | None = None,
        relation_type: str = "",
    ) -> Any:
        client, workspace_slug = get_plane_client_context()

        if error := needs(action, project_id=project_id, workitem_id=workitem_id):
            return error

        if action == "list":
            # Deliberately bypasses `relations.list`: its model declares lists of ids,
            # but Plane answers with {project_id, issue_id} objects, so validation
            # fails as soon as one relation exists. `_get` is a private SDK method
            # (plane-sdk is pinned); revisit once the SDK model is fixed.
            raw = client.work_items.relations._get(
                f"{workspace_slug}/projects/{project_id}/work-items/{workitem_id}/relations"
            )
            if raw is None or isinstance(raw, str):  # an empty body or a non-JSON page, not relations
                return f"Error: Plane returned no relation data for work item {workitem_id}."
            return raw

        if action == "create":
            targets = coerce_list(workitem_ids)
            if not targets:
                return missing(action, "workitem_ids")
            if not relation_type:
                return missing(action, "relation_type")
            if error := one_of("relation_type", relation_type, RELATION_TYPES):
                return error
            created = client.work_items.relations.create(
                workspace_slug=workspace_slug,
                project_id=project_id,
                work_item_id=workitem_id,
                data=CreateWorkItemRelation(
                    relation_type=relation_type,  # type: ignore[arg-type]
                    issues=targets,
                ),
            )
            # Plane drops an id that is no work item of the workspace and still answers
            # 201, so a short list is the only sign that a relation was not created.
            if isinstance(created, list) and len(created) < len(targets):
                return (
                    f"Error: Plane returned {len(created)} of {len(targets)} requested relations; "
                    f"ids that are no work item of this workspace are skipped. Returned: {created}"
                )
            return created
