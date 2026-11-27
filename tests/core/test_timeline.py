"""A node's story for its Details, and what changed since its task review."""

from apm.core.files import read_body
from apm.core.model import Kind, Status
from apm.core.ops import NewNode, attach_delivery, create_nodes, edit_body, link, set_status
from apm.core.project import Project, load_graph
from apm.core.store import Store, TaskReview, content_fingerprint
from apm.core.task_review import review_changes
from apm.core.timeline import builder, node_timeline
from apm.core.views import node_history


def test_the_timeline_tells_the_story_with_links(project: Project) -> None:
    store = Store(project.apm_dir)
    (needed, task) = create_nodes(
        project, [NewNode(title="Before"), NewNode(title="Ledger")]
    ).created
    link(project, task, needed)
    for text in ("one", "two", "three"):
        edit_body(project, task, f"## Intent\n\n{text}\n")
    store.add_task_review(TaskReview(task, "f", "pass", {}, None))
    run = store.create_run(node_id=task, agent="apm-implementer")
    set_status(project, task, Status.IN_PROGRESS, actor="agent:apm-implementer", run=run.id)
    pr = "https://github.com/o/r/pull/9"
    attach_delivery(project, task, branch="apm/x", pr=pr, actor="agent:apm-implementer", run=run.id)

    graph = load_graph(project)
    history = node_history(project, task)
    moments = node_timeline(store, graph, task, history)
    texts = [m.text for m in moments]
    assert texts[:3] == [
        "Created",
        f"Now needs {graph.handles[needed]}",
        "Description edited (3 times)",
    ]
    assert "Task review passed" in texts
    started = next(m for m in moments if m.text == "Started building it")
    assert (started.who, started.run) == ("Claude (Implement)", run.id)
    recorded = next(m for m in moments if m.text == "Pull request recorded")
    assert (recorded.url, recorded.run) == (pr, run.id)
    assert [m.time for m in moments] == sorted(m.time for m in moments)

    assert builder(store, task, history) == store.get_run(run.id)


def test_the_builder_can_be_a_chat_elsewhere_or_nobody(project: Project) -> None:
    store = Store(project.apm_dir)
    by_chat, by_hand = create_nodes(project, [NewNode(title="A"), NewNode(title="B")]).created
    chat = store.create_run(node_id="", agent="apm-assistant")
    set_status(project, by_chat, Status.IN_PROGRESS, actor="agent:apm-assistant", run=chat.id)
    set_status(project, by_hand, Status.IN_PROGRESS)
    assert builder(store, by_chat, node_history(project, by_chat)) == store.get_run(chat.id)
    assert builder(store, by_hand, node_history(project, by_hand)) is None


def test_a_stale_review_says_what_changed(project: Project) -> None:
    (task,) = create_nodes(project, [NewNode(title="Parse")]).created
    before = read_body(project.apm_dir, task)
    reviewed = {"title": "Parse", "kind": "work", "body": before}
    edit_body(
        project,
        task,
        before.replace("## Intent\n", "## Intent\n\nRead it.\n") + "## Approach\n\nA loop.\n",
    )
    node = load_graph(project)[task]
    body = read_body(project.apm_dir, task)
    assert review_changes(reviewed, node, body) == ["Intent edited", "Approach added"]
    renamed = {**reviewed, "title": "Old", "kind": str(Kind.DECISION)}
    assert (review_changes(renamed, node, before) or [])[:2] == ["the title", "the kind"]
    assert review_changes(None, node, body) is None  # an older review kept nothing to compare
    assert content_fingerprint(node, body)  # (the fingerprint, not this, decides staleness)
