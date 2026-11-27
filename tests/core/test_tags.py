"""Tags: labels on nodes, stored in graph.ndjson, changed in one commit, and found by search."""

import pytest

from apm.core.git import log
from apm.core.ndjson import GraphParseError, parse_graph, serialize_node
from apm.core.ops import NewNode, OpError, clean_tag, create_nodes, merge_nodes, set_tags
from apm.core.project import Project, load_graph
from apm.core.review_lock import take_review_lock
from apm.core.views import node_line, search

ID = "01930000-0000-7000-8000-00000000000a"


def test_tags_are_stored_sorted_and_only_when_there_are_some() -> None:
    (node,) = parse_graph(f'{{"id":"{ID}","title":"X","tags":["ui","backlog","ui"]}}\n')
    assert node.tags == ("backlog", "ui")
    assert serialize_node(node).endswith('"status":"todo","tags":["backlog","ui"]}')
    (bare,) = parse_graph(f'{{"id":"{ID}","title":"X"}}\n')
    assert "tags" not in serialize_node(bare)  # files without tags stay byte-identical


@pytest.mark.parametrize("tag", ["UI", "two words", "-x", "", "x" * 41, "é"])
def test_a_bad_tag_in_the_file_is_refused(tag: str) -> None:
    with pytest.raises(GraphParseError, match="tags"):
        parse_graph(f'{{"id":"{ID}","title":"X","tags":["{tag}"]}}\n')


def test_tags_are_cleaned_when_given() -> None:
    assert clean_tag("  #Area/UI ") == "area/ui"
    assert clean_tag("Q3 2027") == "q3-2027"
    with pytest.raises(OpError, match="tag"):
        clean_tag("no!")


def test_tags_change_in_one_commit_each(project: Project) -> None:
    (node,) = create_nodes(project, [NewNode(title="Search", tags=["Backlog"])]).created
    assert load_graph(project)[node].tags == ("backlog",)
    before = len(log(project.apm_dir))
    result = set_tags(project, node, add=["area/ui", "later"], remove=["backlog"])
    assert result.graph[node].tags == ("area/ui", "later")
    assert len(log(project.apm_dir)) == before + 1
    assert log(project.apm_dir)[0].subject.endswith("+area/ui +later -backlog")
    assert set_tags(project, node, add=["later"]).sha is None  # nothing changed


def test_a_task_under_review_can_still_be_tagged(project: Project) -> None:
    (node,) = create_nodes(project, [NewNode(title="Parse")]).created
    take_review_lock(project.apm_dir, node, "a-run")
    assert set_tags(project, node, add=["backlog"]).graph[node].tags == ("backlog",)


def test_merging_keeps_every_tag(project: Project) -> None:
    a, b = create_nodes(
        project, [NewNode(title="A", tags=["ui"]), NewNode(title="B", tags=["api", "ui"])]
    ).created
    assert merge_nodes(project, a, [b]).graph[a].tags == ("api", "ui")


def test_search_finds_tags_and_filters_by_them(project: Project) -> None:
    a, b, c = create_nodes(
        project,
        [
            NewNode(title="Search page", tags=["backlog", "ui"]),
            NewNode(title="Search API", tags=["api"]),
            NewNode(title="Billing", body="The search for a provider.", tags=["backlog"]),
        ],
    ).created
    graph = load_graph(project)
    found = lambda query: [hit.node_id for hit in search(project, graph, query)]  # noqa: E731
    assert found("tag:backlog") == [c, a]
    assert found("#backlog search") == [a, c]  # the title match first
    assert found("tag:backlog tag:ui") == [a]
    assert found("api") == [b]  # a plain word also matches a tag
    assert found("tag:nothing") == []
    assert node_line(graph, a).endswith(" #backlog #ui")
