"""Feature branches: a feature's tasks go into its branch, and its review step's pull request
takes the branch on, to the enclosing feature's branch or the default branch.
"""

from apm.core.delivery import Base, choose_base, ensure_pull_request, feature_branch
from apm.core.git import git
from apm.core.model import Status
from apm.core.ops import NewNode, attach_delivery, create_nodes, decompose, link, set_status
from apm.core.project import Project, load_graph
from apm.core.reconcile import reconcile
from apm.runner.agents import base_instructions
from tests.conftest import CodeRepo
from tests.fake_gh import FakeGh


def feature_with_tasks(project: Project, *titles: str, parent: str | None = None) -> tuple:
    feature = create_nodes(project, [NewNode(title="Export", parent=parent)]).created[0]
    tasks = decompose(project, feature, [NewNode(title=t) for t in titles]).created
    step = load_graph(project).review_step(feature)
    assert step is not None
    return feature, step, *tasks


def on_origin(repo: CodeRepo, branch: str) -> str | None:
    out = git(repo.dir, ["ls-remote", "origin", f"refs/heads/{branch}"]).strip()
    return out.split()[0] if out else None


def test_a_task_goes_into_its_feature_s_branch_which_apm_creates(code_repo: CodeRepo) -> None:
    project = code_repo.project
    feature, step, task = feature_with_tasks(project, "Write the file")
    name = feature_branch(load_graph(project), feature)
    assert name.startswith("feature/")
    assert name.endswith("-export")
    assert on_origin(code_repo, name) is None

    assert choose_base(project, task) == Base(name, feature=feature)
    main = git(code_repo.dir, ["rev-parse", "main"]).strip()
    assert on_origin(code_repo, name) == main  # created from the default branch
    delivery = load_graph(project)[step].delivery
    assert delivery is not None
    assert delivery.branch == name  # and recorded on the review step

    # Asking again changes nothing.
    assert choose_base(project, task) == Base(name, feature=feature)


def test_a_nested_feature_branches_off_its_parent_s_branch(code_repo: CodeRepo) -> None:
    project = code_repo.project
    outer, _, _ = feature_with_tasks(project, "Outer task")
    inner, inner_step, inner_task = feature_with_tasks(project, "Inner task", parent=outer)
    graph = load_graph(project)
    outer_branch, inner_branch = feature_branch(graph, outer), feature_branch(graph, inner)

    base = choose_base(project, inner_task)
    assert base == Base(inner_branch, feature=inner)
    assert on_origin(code_repo, outer_branch) is not None  # made first, to branch off
    # The inner feature's own pull request goes into the outer feature's branch.
    assert choose_base(project, inner_step) == Base(outer_branch, feature=outer)


def test_a_review_step_never_stacks_on_its_own_tasks(code_repo: CodeRepo) -> None:
    project = code_repo.project
    _, step, first, second = feature_with_tasks(project, "First", "Second")
    code_repo.push_branch("apm/first")
    attach_delivery(project, first, branch="apm/first")
    set_status(project, first, Status.DONE)
    set_status(project, second, Status.DONE)
    assert choose_base(project, step) == Base("main")


def test_a_task_stacked_on_a_sibling_s_branch_opens_its_pr_into_the_feature_branch(
    code_repo: CodeRepo, fake_gh: FakeGh
) -> None:
    project = code_repo.project
    feature, _, first, second = feature_with_tasks(project, "First", "Second")
    link(project, second, first)
    name = choose_base(project, first).branch
    assert name is not None
    code_repo.push_branch("apm/first", base=f"origin/{name}")
    attach_delivery(project, first, branch="apm/first")

    base = choose_base(project, second)
    assert base == Base("apm/first", stacked_on=first, feature=feature, target=name)
    assert base.pr_base == name
    prompt = base_instructions(load_graph(project), base)
    assert "start from origin/apm/first" in prompt
    assert f"`gh pr create --base {name}`" in prompt

    code_repo.push_branch("apm/second", base="apm/first")
    attach_delivery(project, second, branch="apm/second")
    outcome = ensure_pull_request(project, second)
    assert (outcome.action, outcome.base) == ("opened", name)
    assert fake_gh.field(fake_gh.calls[-1], "base") == name


def test_a_task_is_done_once_merged_into_its_feature_branch(
    code_repo: CodeRepo, fake_gh: FakeGh
) -> None:
    project = code_repo.project
    _, step, task = feature_with_tasks(project, "Write the file")
    name = choose_base(project, task).branch
    assert name is not None
    code_repo.push_branch("apm/write", base=f"origin/{name}")
    url = fake_gh.add(3, base=name, state="closed", merged=True)
    attach_delivery(project, task, branch="apm/write", pr=url)
    set_status(project, task, Status.IN_PROGRESS)

    report = reconcile(project)
    assert [r.node_id for r in report.done] == [task]
    assert load_graph(project)[task].status is Status.DONE
    assert load_graph(project).states[step].ready  # every task is in: time to review


def test_the_review_step_s_pull_request_landing_finishes_the_feature(
    code_repo: CodeRepo, fake_gh: FakeGh
) -> None:
    project = code_repo.project
    feature, step, task = feature_with_tasks(project, "Write the file")
    name = choose_base(project, task).branch
    assert name is not None
    set_status(project, task, Status.DONE)

    # A passing review opened the feature's pull request: feature branch → main.
    set_status(project, step, Status.IN_PROGRESS)
    outcome = ensure_pull_request(project, step, title="Export")
    assert (outcome.action, outcome.base) == ("opened", "main")
    post = next(call for call in fake_gh.calls if call[:3] == ["api", "-X", "POST"])
    assert f"head={name}" in post
    assert "title=Export" in post

    fake_gh.add(7, base="main", state="closed", merged=True)
    report = reconcile(project)
    assert [r.node_id for r in report.done] == [step, feature]
    graph = load_graph(project)
    assert graph[step].status is Status.DONE
    assert graph[feature].status is Status.DONE
