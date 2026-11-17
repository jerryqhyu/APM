from apm.core.ops import NewNode, create_nodes, edit_body
from apm.core.project import Project, load_graph
from apm.core.search import search


def test_search_matches_titles_and_bodies(project: Project) -> None:
    stream, parse, other = create_nodes(
        project,
        [NewNode(title="Stream agent output"), NewNode(title="Parse"), NewNode(title="Other")],
    ).created
    edit_body(project, parse, "## Intent\n\nRead the agent OUTPUT stream line by line.\n")
    graph = load_graph(project)

    hits = search(project, graph, "output  AGENT")
    assert [h.node_id for h in hits] == [stream, parse]  # the title match first
    assert hits[0].snippet == ""
    assert hits[1].snippet == "Read the agent OUTPUT stream line by line."
    assert search(project, graph, "nothing here") == []
    assert search(project, graph, "   ") == []
    assert [h.node_id for h in search(project, graph, "other")] == [other]
