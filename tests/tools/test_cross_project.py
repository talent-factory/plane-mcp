"""workitem list/count with assignee or state_group, across projects.

The Community Edition lists no workspace and rejects PQL, so the filter runs
here. These tests pin what a caller can rely on: only matching items come back,
a project the caller cannot read is reported rather than fatal, and a walk that
stopped early says so, naming what is missing.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from plane.errors.errors import HttpError

from plane_mcp.toolkit import scan as scan_module

ME = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


def _page(rows, nxt=None, promised=None):
    return SimpleNamespace(
        results=rows, next_cursor=nxt or "", next_page_results=bool(nxt) if promised is None else promised
    )


def _item(n, group, assignees=(ME,), expanded=True):
    state = SimpleNamespace(id="s", name=group, group=group) if expanded else "state-uuid"
    return SimpleNamespace(
        id=f"i{n}",
        sequence_id=n,
        name=f"Item {n}",
        state=state,
        priority="medium",
        project="p",
        assignees=list(assignees),
        model_dump=lambda include=None, n=n, group=group, assignees=assignees: {
            "id": f"i{n}",
            "sequence_id": n,
            "state": {"group": group},
            "assignees": list(assignees),
        },
    )


class World:
    """Two readable projects, one closed one, and a user endpoint."""

    def __init__(self):
        self.listed = []
        self.projects_asked = []
        self.items = {
            "p1": [_item(1, "started"), _item(2, "completed"), _item(3, "started", (OTHER,))],
            "p2": [_item(1, "started")],
            "p3": [],
        }
        self.failure = {"p3": HttpError("denied", 403, {"error": "no"})}
        self.project_pages = {None: _page([SimpleNamespace(id=p, identifier=p.upper()) for p in ("p1", "p2", "p3")])}
        self.me = SimpleNamespace(id=ME)

        def projects(workspace_slug, params):
            self.projects_asked.append(params)
            return self.project_pages[params.cursor]

        def work_items(workspace_slug, project_id, params):
            self.listed.append((project_id, params))
            if project_id in self.failure:
                raise self.failure[project_id]
            page = self.items[project_id]
            return page(params) if callable(page) else _page(page)

        def me():
            if isinstance(self.me, Exception):
                raise self.me
            return self.me

        self.client = SimpleNamespace(
            projects=SimpleNamespace(list_lite=projects),
            work_items=SimpleNamespace(list=work_items),
            users=SimpleNamespace(get_me=me),
        )


@pytest.fixture
def world(registered, monkeypatch):
    from plane_mcp.tools import workitem

    w = World()
    monkeypatch.setattr(workitem, "get_plane_client_context", lambda: (w.client, "acme"))
    return w


def run(registered, **args):
    return registered["workitem"].fn(**args)


def ids(result):
    return [r["identifier"] for r in result["results"]]


def test_assignee_me_and_state_group_filter_across_projects(world, registered):
    result = run(registered, action="list", assignee="me", state_group="started")

    assert ids(result) == ["P1-1", "P2-1"]
    assert result["projects_scanned"] == 2
    assert result["skipped_projects"] == ["P3"]
    assert result["complete"] is False  # P3 was unreadable, so "nothing else" is not known


def test_a_state_filter_alone_drops_what_does_not_match(world, registered):
    result = run(registered, action="list", state_group="completed")

    assert ids(result) == ["P1-2"]


def test_the_walk_asks_for_the_state_object_and_a_full_page(world, registered):
    run(registered, action="list", assignee="me", fields="description_html")

    params = world.listed[0][1]
    assert params.expand == "state" and params.per_page == 100
    assert params.fields.split(",")[-1] == "description_html"
    assert len(set(params.fields.split(","))) == len(params.fields.split(","))


def test_an_item_whose_state_cannot_be_classified_is_counted_not_dropped(world, registered):
    world.items["p2"] = [_item(1, "started", expanded=False)]

    result = run(registered, action="list", state_group="started")

    assert ids(result) == ["P1-1", "P1-3"]
    assert result["unclassified"] == 1 and result["complete"] is False


def test_count_groups_by_project(world, registered):
    world.failure.clear()
    result = run(registered, action="count", assignee=ME, state_group=["started", "completed"])

    assert result["count"] == 3 and result["complete"] is True
    assert result["by_project"] == {"P1": 2, "P2": 1, "P3": 0} or result["by_project"] == {"P1": 2, "P2": 1}


def test_a_partial_count_is_not_called_count(world, registered):
    result = run(registered, action="count", assignee="me")

    assert "count" not in result and result["count_at_least"] == 3


def test_count_never_stops_at_the_match_limit(world, registered, monkeypatch):
    monkeypatch.setattr(scan_module, "MAX_MATCHES", 1)

    assert run(registered, action="count", assignee="me")["count_at_least"] == 3


def test_project_id_narrows_the_walk_by_uuid_or_identifier(world, registered):
    for ref in ("p2", "P2"):
        world.listed.clear()
        result = run(registered, action="list", project_id=ref, assignee="me")

        assert [pid for pid, _ in world.listed] == ["p2"]
        assert ids(result) == ["P2-1"] and result["projects_scanned"] == 1 and result["complete"] is True


def test_an_unknown_project_is_an_error(world, registered):
    assert run(registered, action="list", project_id="nope", assignee="me").startswith("Error:")


def test_a_project_you_cannot_read_is_an_error_not_an_empty_list(world, registered):
    result = run(registered, action="list", project_id="p3", assignee="me")

    assert isinstance(result, str) and "no access" in result


def test_an_invalid_state_group_names_the_choices(world, registered):
    result = run(registered, action="list", state_group="open")

    assert result.startswith("Error: state_group must be one of") and not world.listed


@pytest.mark.parametrize("who", ["Daniel", "d@x.ch", "not-a-uuid"])
def test_an_assignee_that_is_not_me_or_a_uuid_is_refused(world, registered, who):
    assert run(registered, action="list", assignee=who).startswith("Error: assignee must be")
    assert not world.listed


@pytest.mark.parametrize("action", ["list_archived", "search", "retrieve", "delete"])
def test_the_filters_are_refused_on_other_actions(world, registered, action):
    result = run(registered, action=action, assignee="me", project_id="p1", query="x", workitem_id="w")

    assert result.startswith("Error:") and action in result


@pytest.mark.parametrize("action", ["list", "count"])
@pytest.mark.parametrize(
    "extra",
    [
        {"pql": "x"},
        {"cursor": "c"},
        {"per_page": 5},
        {"order_by": "name"},
        {"group_by": "priority"},
        {"sub_group_by": "priority"},
        {"expand": "state"},
        {"external_id": "e"},
        {"external_source": "s"},
    ],
)
def test_arguments_the_walk_cannot_honour_are_refused_not_dropped(world, registered, action, extra):
    result = run(registered, action=action, assignee="me", **extra)

    assert result.startswith("Error:") and next(iter(extra)) in result
    assert not world.listed


def test_me_that_plane_cannot_name_is_an_error_before_any_walk(world, registered):
    world.me = SimpleNamespace(id="")

    assert run(registered, action="list", assignee="me").startswith("Error: Plane did not say who")
    assert not world.listed and not world.projects_asked


def test_the_walk_is_bounded_and_names_what_it_missed(world, registered, monkeypatch):
    monkeypatch.setattr(scan_module, "MAX_REQUESTS", 3)

    result = run(registered, action="list", assignee="me")

    assert "budget" in result["truncated"] and result["complete"] is False
    assert result["unscanned_projects"] == ["P3"]
    assert ids(result) == ["P1-1", "P1-2", "P2-1"]


def test_the_match_limit_keeps_the_hits_and_names_the_rest(world, registered, monkeypatch):
    monkeypatch.setattr(scan_module, "MAX_MATCHES", 1)

    result = run(registered, action="list", assignee="me")

    assert ids(result) == ["P1-1"]
    assert result["partial_projects"] == ["P1"] and result["unscanned_projects"] == ["P2", "P3"]


def test_the_limit_is_not_reported_when_nothing_is_left(world, registered, monkeypatch):
    monkeypatch.setattr(scan_module, "MAX_MATCHES", 1)
    world.failure.clear()

    result = run(registered, action="list", project_id="p2", assignee="me")

    assert result["truncated"] is None and result["partial_projects"] == []


def test_a_429_keeps_the_hits_found_so_far(world, registered):
    world.failure["p2"] = HttpError("slow down", 429, None)

    result = run(registered, action="list", assignee="me")

    assert result["truncated"].startswith("Plane answered 429")
    assert ids(result) == ["P1-1", "P1-2"]
    assert result["partial_projects"] == [] and result["unscanned_projects"] == ["P2", "P3"]


def test_a_429_on_the_project_list_is_a_partial_answer_too(world, registered):
    world.client.projects.list_lite = lambda **_: (_ for _ in ()).throw(HttpError("slow", 429, None))

    result = run(registered, action="list", assignee="me")

    assert result["results"] == [] and result["truncated"].startswith("Plane answered 429")


def test_other_errors_still_raise(world, registered):
    world.failure["p2"] = HttpError("boom", 500, None)

    with pytest.raises(HttpError):
        run(registered, action="list", assignee="me")


def test_work_item_pages_are_followed_by_cursor(world, registered):
    world.failure.clear()
    world.items["p1"] = lambda params: (
        _page([_item(2, "started")]) if params.cursor == "next" else _page([_item(1, "started")], "next")
    )

    result = run(registered, action="count", project_id="p1", assignee="me")

    assert result["count"] == 2
    assert [p.cursor for pid, p in world.listed if pid == "p1"] == [None, "next"]


def test_a_promised_page_without_a_cursor_is_reported(world, registered):
    world.items["p1"] = lambda params: _page([_item(1, "started")], promised=True)

    result = run(registered, action="list", project_id="p1", assignee="me")

    assert "no cursor" in result["truncated"] and result["partial_projects"] == ["P1"]


def test_project_pages_are_followed_and_counted(world, registered):
    world.failure.clear()
    world.project_pages = {
        None: _page([SimpleNamespace(id="p1", identifier="P1")], "more"),
        "more": _page([SimpleNamespace(id="p2", identifier="P2")]),
    }
    world.items["p3"] = []

    result = run(registered, action="list", assignee="me")

    assert ids(result) == ["P1-1", "P1-2", "P2-1"] and result["projects_scanned"] == 2


def test_no_filter_keeps_the_old_workspace_listing(registered, spy):
    run(registered, action="list")

    assert spy.recorder.only().method == "work_items.list_workspace"
