"""Relations between work items.

Uses the classic `work-items/<id>/relations/` endpoint, which every Plane release
serves, Community Edition v1.4.2 included. The newer `dependencies/`,
`work-item-relations/` and `work-item-relation-definitions/` endpoints answer 404
there, so custom relation definitions are not offered. The public API of v1.4.2 can
list and create relations but not remove one (only the session API behind the web UI
can), so there is no delete action.
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
    "of workitem_id: blocked_by means workitem_id is blocked by every id in workitem_ids. "
    "A relation cannot be removed through this API; do that in the Plane web UI."
)

LEGACY = {
    "list_work_item_relations": "list",
    "create_work_item_relation": "create",
}

_NO_DEFINITIONS = "Custom relation definitions need a newer Plane than this instance runs."

LEGACY_UNMAPPED = {
    "remove_work_item_relation": "Plane's public API v1.4.2 cannot remove a relation: use the web UI.",
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
            # The SDK model expects lists of ids; Plane answers with full work item
            # objects, so `relations.list` fails to validate once a relation exists.
            return client.work_items.relations._get(
                f"{workspace_slug}/projects/{project_id}/work-items/{workitem_id}/relations"
            )

        if action == "create":
            targets = coerce_list(workitem_ids)
            if not targets:
                return missing(action, "workitem_ids")
            if not relation_type:
                return missing(action, "relation_type")
            if error := one_of("relation_type", relation_type, RELATION_TYPES):
                return error
            return client.work_items.relations.create(
                workspace_slug=workspace_slug,
                project_id=project_id,
                work_item_id=workitem_id,
                data=CreateWorkItemRelation(
                    relation_type=relation_type,  # type: ignore[arg-type]
                    issues=targets,
                ),
            )
