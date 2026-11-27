"""Finding the conversations that worked on a node from elsewhere, and their sessions."""

from pathlib import Path

from apm.core.conversations import conversation_of, related_runs, runs_that_changed, session_of
from apm.core.model import Status
from apm.core.ops import NewNode, create_nodes, set_status
from apm.core.project import Project
from apm.core.store import PROJECT, Store


def test_a_plan_chat_that_changed_a_task_is_found_from_the_task(project: Project) -> None:
    store = Store(project.apm_dir)
    task, other = create_nodes(
        project, [NewNode(title="Store costs"), NewNode(title="Other")]
    ).created
    first = store.create_run(node_id=PROJECT, agent="apm-assistant", prompt="Where are we?")
    second = store.create_run(node_id=PROJECT, agent="apm-assistant", parent_run=first.id)
    unrelated = store.create_run(node_id=PROJECT, agent="apm-assistant", prompt="Hello")
    own = store.create_run(node_id=task, agent="apm-implementer")
    set_status(project, task, Status.IN_PROGRESS, actor="agent:apm-assistant", run=second.id)
    set_status(project, other, Status.IN_PROGRESS, actor="agent:apm-assistant", run=unrelated.id)

    assert runs_that_changed(project, task) == {second.id}
    assert [r.id for r in conversation_of(store, second.id)] == [first.id, second.id]
    # The whole conversation, newest first, and never the task's own runs.
    assert [r.id for r in related_runs(project, store, task)] == [second.id, first.id]
    assert own.id not in {r.id for r in related_runs(project, store, task)}


def test_a_conversation_opens_in_a_terminal_where_it_ran(project: Project, tmp_path: Path) -> None:
    store = Store(project.apm_dir)
    first = store.create_run(node_id=PROJECT, agent="apm-assistant", session_id="s-1")
    store.add_event(first.id, {"type": "system", "subtype": "init", "cwd": str(tmp_path)})
    later = store.create_run(
        node_id=PROJECT, agent="apm-assistant", session_id="s-2", parent_run=first.id
    )
    store.add_event(later.id, {"type": "system", "subtype": "init", "cwd": str(tmp_path / "gone")})

    session = session_of(store, first.id)  # any turn gives the conversation's latest session
    assert session is not None
    assert session.session_id == "s-2"
    assert session.cwd is None  # its directory is gone: resume from anywhere
    assert session.command == "claude --resume s-2"

    alone = store.create_run(node_id=PROJECT, agent="apm-assistant", session_id="s 3")
    store.add_event(alone.id, {"type": "system", "cwd": str(tmp_path)})
    alone_session = session_of(store, alone.id)
    assert alone_session is not None
    assert alone_session.command == f"cd {tmp_path} && claude --resume 's 3'"
