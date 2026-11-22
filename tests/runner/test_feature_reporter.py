"""The feature report, end to end through the runner: written after a passing review and linked
from the feature's pull request, or by hand. A fake `claude`, real git and a fake `gh`.
"""

import pytest

from apm.core.delivery import choose_base
from apm.core.git import git, log
from apm.core.ids import handle
from apm.core.model import Status
from apm.core.ops import NewNode, attach_delivery, create_nodes, decompose, set_status
from apm.core.project import Project, load_graph
from apm.core.report import REPORTER, ReportRefused
from apm.runner import runner as runner_module
from apm.runner.runner import RunnerBusy
from tests.conftest import CodeRepo
from tests.core.test_feature_review import built, feature
from tests.fake_gh import FakeGh
from tests.runner.fake import FakeClaude
from tests.runner.test_feature_reviewer import notices
from tests.runner.test_runner import Harness, option

REVIEWER = "apm-feature-reviewer"


def configure(project: Project, text: str) -> None:
    config = project.apm_dir / "apm.yaml"
    config.write_text(config.read_text() + text)


def tip(repo: CodeRepo, branch: str) -> str:
    return git(repo.dir, ["rev-parse", f"origin/{branch}"]).strip()


def reporter_run(harness: Harness, node: str) -> str:
    (run,) = [r for r in harness.store.list_runs(node_id=node) if r.agent == REPORTER]
    return run.id


async def test_a_passing_review_puts_the_report_in_the_feature_s_pull_request(
    code_repo: CodeRepo, fake_claude: FakeClaude, fake_gh: FakeGh
) -> None:
    project = code_repo.project
    node, step, _, branch = built(code_repo, project, "Write", "Read")
    reviewed = tip(code_repo, branch)
    pr = fake_gh.add(7, body="The spec.\n\nAPM-Node: x\n")  # what POST will open
    harness = Harness(project)

    review = harness.runner.start(REVIEWER, node, "Look hard at errors.")
    await harness.runner.wait(review.id)
    assert harness.runner.active() == 0

    # The pull request opened first: the report never holds it up.
    posted = next(i for i, c in enumerate(fake_gh.calls) if c[:3] == ["api", "-X", "POST"])
    patched = next(i for i, c in enumerate(fake_gh.calls) if "PATCH" in c)
    assert posted < patched

    _, call = fake_claude.calls
    assert call["agent"] == REPORTER
    assert ".claude/worktrees/apm-report-" in call["cwd"]  # a checkout of the feature branch
    assert option(call, "--permission-mode") == "dontAsk"  # nobody may be watching
    assert "Write" in option(call, "--disallowedTools")
    assert "--json-schema" in call["argv"]
    prompt = call["prompt"]
    assert "at **standard** detail" in prompt
    assert f"# The record of feature {handle(node)} “Export”" in prompt
    assert "Look hard at errors." in prompt  # what the user said to the review is on record

    # APM committed the report onto the feature branch, on top of what was reviewed.
    path = f"docs/features/export-{handle(node)}.md"
    head = tip(code_repo, branch)
    assert git(code_repo.dir, ["rev-parse", f"{head}^"]).strip() == reviewed
    assert git(code_repo.dir, ["show", f"{head}:{path}"]).startswith(
        "# Search\n\nA standard report"
    )
    (entry,) = log(code_repo.dir, f"{reviewed}..{head}")
    assert entry.trailers["APM-Node"] == node

    # The pull request links it, and the store has it for the UI.
    url = f"https://github.com/o/r/blob/{branch}/{path}"
    assert fake_gh.pr(7)["body"].startswith(f"**Feature report:** [{path}]({url})\n\nThe spec.")
    report = harness.store.latest_report(node)
    assert report is not None
    assert (report.sha, report.path, report.branch, report.detail) == (
        head,
        path,
        branch,
        "standard",
    )
    assert report.summary == "Search for the app."

    written = reporter_run(harness, node)
    assert harness.store.get_run(written).status == "succeeded"
    said = [text for _, text in notices(harness, written)]
    assert said[0].startswith(f"Writing the report from `{branch}` at `{reviewed[:7]}`")
    assert f"**Report written**: committed to `{branch}` as `{head[:7]}`" in said[-1]
    assert f"Its pull request links it: {pr}" in said[-1]
    in_review = [text for _, text in notices(harness, review.id)]
    assert "APM is writing the feature report" in in_review[-2]
    assert "The feature report is written" in in_review[-1]
    assert "report.written" in harness.types()


async def test_the_detail_level_reaches_the_prompt(
    code_repo: CodeRepo, fake_claude: FakeClaude, fake_gh: FakeGh
) -> None:
    project = code_repo.project
    node, _, _, branch = built(code_repo, project, "Write")
    configure(project, f"features:\n  overrides:\n    {node}: {{report_detail: detailed}}\n")
    harness = Harness(project)
    run = harness.runner.start(REVIEWER, node)
    await harness.runner.wait(run.id)
    prompt = fake_claude.calls[-1]["prompt"]
    assert "at **detailed** detail" in prompt
    assert "Implementation notes" in prompt
    assert "Timeline" in prompt
    shown = git(code_repo.dir, ["show", f"origin/{branch}:docs/features/export-{handle(node)}.md"])
    assert "A detailed report" in shown
    assert "at detailed detail" in shown  # the line APM adds says so too


@pytest.mark.parametrize(
    "setting",
    [
        "features:\n  report: false\n",
        "features:\n  overrides:\n    {node}: {{report: false}}\n",
    ],
    ids=["project-wide", "this-feature"],
)
async def test_with_reports_off_a_pass_writes_nothing(
    code_repo: CodeRepo, fake_claude: FakeClaude, fake_gh: FakeGh, setting: str
) -> None:
    project = code_repo.project
    node, _, _, branch = built(code_repo, project, "Write")
    configure(project, setting.format(node=node))
    reviewed = tip(code_repo, branch)
    harness = Harness(project)
    run = harness.runner.start(REVIEWER, node)
    await harness.runner.wait(run.id)

    assert [c["agent"] for c in fake_claude.calls] == [REVIEWER]
    assert any(c[:3] == ["api", "-X", "POST"] for c in fake_gh.calls)  # the PR still opens
    assert not any("PATCH" in c for c in fake_gh.calls)
    assert tip(code_repo, branch) == reviewed
    assert harness.store.latest_report(node) is None
    assert not any("report" in text for _, text in notices(harness, run.id))


@pytest.mark.parametrize(
    ("mode", "why"),
    [
        ("report-fails", "claude exited with code 1: boom: something broke"),
        ("report-bad", "not a valid report: the markdown is empty"),
        ("report-hangs", "the reporter took longer than 0.01 minutes, so APM stopped it"),
    ],
)
async def test_a_failed_report_never_stops_the_pull_request(
    code_repo: CodeRepo,
    fake_claude: FakeClaude,
    fake_gh: FakeGh,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    why: str,
) -> None:
    project = code_repo.project
    monkeypatch.setattr(runner_module, "REPORT_TIMEOUT_S", 0.6)
    node, step, _, branch = built(code_repo, project, "Write")
    reviewed = tip(code_repo, branch)
    fake_claude.mode(mode)
    harness = Harness(project)
    run = harness.runner.start(REVIEWER, node)
    await harness.runner.wait(run.id)

    assert harness.store.get_run(run.id).status == "succeeded"  # the review itself is fine
    delivery = load_graph(project)[step].delivery
    assert delivery is not None
    assert delivery.pr == "https://github.com/o/r/pull/7"
    assert tip(code_repo, branch) == reviewed
    assert harness.store.latest_report(node) is None
    failed = harness.store.get_run(reporter_run(harness, node))
    assert failed.status == "failed"
    assert failed.error == why
    level, text = notices(harness, run.id)[-1]
    assert level == "warning"
    assert text == (
        f"No feature report: {why}. The pull request is open without it. Write it by hand with"
        f" **Write report** on the feature, or `apm report {handle(node)}`."
    )


async def test_a_report_written_by_hand_and_rewritten(
    code_repo: CodeRepo, fake_claude: FakeClaude, fake_gh: FakeGh
) -> None:
    # Before the review: the tasks are merged into the feature branch, nothing more.
    project = code_repo.project
    node, step, _, branch = built(code_repo, project, "Write")
    before = tip(code_repo, branch)
    harness = Harness(project)

    run = harness.runner.start(REPORTER, step, "Keep it short.")  # on the step: its feature's
    assert run.node_id == node
    await harness.runner.wait(run.id)
    assert harness.store.get_run(run.id).status == "succeeded"
    first = tip(code_repo, branch)
    assert git(code_repo.dir, ["rev-parse", f"{first}^"]).strip() == before
    (call,) = fake_claude.calls
    assert "## Guidance from the user\n\nKeep it short." in call["prompt"]
    assert option(call, "--permission-mode") == "auto"  # by hand: the project's default
    said = notices(harness, run.id)[-1][1]
    assert "The feature's pull request will contain it once it is opened." in said
    assert fake_gh.calls == []  # no pull request yet, so nothing to link

    # Again, briefer: a new commit on top, never a force-push. (The same report again would
    # change nothing, and make no commit.)
    configure(project, "features:\n  report_detail: brief\n")
    again = harness.runner.start(REPORTER, node)
    await harness.runner.wait(again.id)
    second = tip(code_repo, branch)
    assert git(code_repo.dir, ["rev-parse", f"{second}^"]).strip() == first
    assert log(code_repo.dir, f"{first}..{second}")[0].subject.startswith("Update the feature")
    path = f"docs/features/export-{handle(node)}.md"
    assert "A brief report" in git(code_repo.dir, ["show", f"{second}:{path}"])

    # A reply in its conversation rewrites it, from the same checkout.
    reply = harness.runner.continue_run(again.id, "Say more about the index.")
    await harness.runner.wait(reply.id)
    third = tip(code_repo, branch)
    assert git(code_repo.dir, ["rev-parse", f"{third}^"]).strip() == second
    assert "rewritten" in git(code_repo.dir, ["show", f"{third}:{path}"])
    assert fake_claude.calls[-1]["cwd"] == fake_claude.calls[-2]["cwd"]
    latest = harness.store.latest_report(node)
    assert latest is not None
    assert (latest.sha, latest.run_id) == (third, reply.id)


async def test_one_report_of_a_feature_at_a_time(
    code_repo: CodeRepo, fake_claude: FakeClaude
) -> None:
    project = code_repo.project
    node, _, _, _ = built(code_repo, project, "Write")
    fake_claude.mode("hang")
    harness = Harness(project)
    run = harness.runner.start(REPORTER, node)
    with pytest.raises(RunnerBusy, match="already running on this feature"):
        harness.runner.start(REPORTER, node)
    await harness.runner.cancel(run.id)


async def test_a_report_needs_a_feature_with_work(
    code_repo: CodeRepo, fake_claude: FakeClaude
) -> None:
    project = code_repo.project
    node, _, (task,) = feature(project, "Write")
    harness = Harness(project)
    with pytest.raises(ReportRefused, match="only a feature has a report"):
        harness.runner.start(REPORTER, task)
    with pytest.raises(ReportRefused, match="nothing is built yet"):
        harness.runner.start(REPORTER, node)

    # Delivered, but not on the feature branch: the run says why, before Claude starts.
    attach_delivery(project, task, branch="apm/elsewhere")
    run = harness.runner.start(REPORTER, node)
    await harness.runner.wait(run.id)
    failed = harness.store.get_run(run.id)
    assert failed.status == "failed"
    assert failed.error is not None
    assert "the feature has no branch yet" in failed.error
    assert fake_claude.calls == []


async def test_a_nested_feature_s_report_travels_up_with_it(
    code_repo: CodeRepo, fake_claude: FakeClaude, fake_gh: FakeGh
) -> None:
    project = code_repo.project
    outer, _, (first,) = feature(project, "Write")
    (inner,) = create_nodes(project, [NewNode(title="Import", parent=outer)]).created
    (task,) = decompose(project, inner, [NewNode(title="Read")]).created
    inner_branch = choose_base(project, task).branch
    outer_branch = choose_base(project, first).branch
    assert inner_branch is not None
    assert outer_branch is not None
    sha = code_repo.push_branch(f"apm/{task[-6:]}", base=f"origin/{inner_branch}")
    git(code_repo.dir, ["push", "--quiet", "origin", f"{sha}:refs/heads/{inner_branch}"])
    attach_delivery(project, task, branch=f"apm/{task[-6:]}", commits=[sha])
    set_status(project, task, Status.DONE)
    fake_gh.add(7, base=outer_branch)
    harness = Harness(project)

    run = harness.runner.start(REVIEWER, inner)
    await harness.runner.wait(run.id)

    post = next(c for c in fake_gh.calls if c[:3] == ["api", "-X", "POST"])
    assert fake_gh.field(post, "head") == inner_branch
    assert fake_gh.field(post, "base") == outer_branch  # its PR goes into the outer feature
    path = f"docs/features/import-{handle(inner)}.md"
    assert git(code_repo.dir, ["show", f"origin/{inner_branch}:{path}"]).startswith("# Search")
    assert fake_gh.pr(7)["body"].startswith(f"**Feature report:** [{path}]")
    report = harness.store.latest_report(inner)
    assert report is not None
    assert report.branch == inner_branch
    prompt = fake_claude.calls[-1]["prompt"]
    assert f"`git diff origin/{outer_branch}...HEAD`" in prompt
