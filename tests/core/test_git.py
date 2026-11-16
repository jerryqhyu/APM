import pytest

from apm.core.git import (
    CommitMessage,
    GitError,
    add,
    commit,
    format_commit_message,
    git,
    is_ancestor,
    log,
    rev_parse,
    show,
)
from tests.conftest import TempRepo

NODE_A = "0192f3a1-7c4e-7b91-a2d5-3f8e1c0b9a44"
NODE_B = "0192f2c8-1a0d-7e33-8c41-5b2a9d0e7f10"


def test_format_puts_trailers_in_the_last_paragraph() -> None:
    message = CommitMessage(
        subject="decompose 1c0b9a44: 2 children",
        body="Split the streaming work.",
        trailers=[("APM-Op", "decompose"), ("APM-Nodes", f"{NODE_A},{NODE_B}")],
    )
    assert format_commit_message(message) == (
        "decompose 1c0b9a44: 2 children\n\nSplit the streaming work.\n\n"
        f"APM-Op: decompose\nAPM-Nodes: {NODE_A},{NODE_B}\n"
    )
    assert format_commit_message(CommitMessage(subject="s")) == "s\n"


@pytest.mark.parametrize(
    ("message", "pattern"),
    [
        (CommitMessage(subject=""), "subject"),
        (CommitMessage(subject="a\nb"), "subject"),
        (CommitMessage(subject="s", trailers=[("Bad Key", "v")]), "trailer key"),
        (CommitMessage(subject="s", trailers=[("-Op", "v")]), "trailer key"),
        (CommitMessage(subject="s", trailers=[("K", "a\nK2: b")]), "one non-blank line"),
    ],
)
def test_format_rejects_values_that_would_corrupt_the_message(
    message: CommitMessage, pattern: str
) -> None:
    with pytest.raises(ValueError, match=pattern):
        format_commit_message(message)


def test_commits_with_trailers_that_round_trip_through_log(temp_repo: TempRepo) -> None:
    (temp_repo.dir / "graph.ndjson").write_text("")
    add(temp_repo.dir, ["graph.ndjson"])
    sha = commit(
        temp_repo.dir,
        CommitMessage(
            subject="create 1c0b9a44: Stream agent output",
            trailers=[
                ("APM-Op", "create"),
                ("APM-Nodes", NODE_A),
                ("APM-Actor", "agent:apm-decomposer"),
            ],
        ),
    )
    assert len(sha) == 40
    [entry] = log(temp_repo.dir)
    assert entry.sha == sha
    assert entry.subject == "create 1c0b9a44: Stream agent output"
    assert entry.trailers == {
        "APM-Op": "create",
        "APM-Nodes": NODE_A,
        "APM-Actor": "agent:apm-decomposer",
    }
    assert entry.time > 1_700_000_000


def test_keeps_a_subject_starting_with_a_hash(temp_repo: TempRepo) -> None:
    temp_repo.commit_file("a", "1", "#12: keep me")
    assert log(temp_repo.dir)[0].subject == "#12: keep me"


def test_handles_an_empty_repo(temp_repo: TempRepo) -> None:
    assert rev_parse(temp_repo.dir, "HEAD") is None
    assert log(temp_repo.dir) == []


def test_logs_newest_first_or_oldest_first_filtered_by_path_and_range(temp_repo: TempRepo) -> None:
    first = temp_repo.commit_file("graph.ndjson", "1\n")
    second = temp_repo.commit_file("other", "x")
    third = temp_repo.commit_file("graph.ndjson", "2\n")

    def shas(*args: str, **kwargs: bool) -> list[str]:
        return [e.sha for e in log(temp_repo.dir, *args, **kwargs)]

    assert shas() == [third, second, first]
    assert [e.sha for e in log(temp_repo.dir, "HEAD", ["graph.ndjson"], reverse=True)] == [
        first,
        third,
    ]
    assert shas(f"{first}..HEAD") == [third, second]


def test_show_reads_files_at_a_commit_byte_for_byte(temp_repo: TempRepo) -> None:
    first = temp_repo.commit_file("graph.ndjson", "old\r\n")
    temp_repo.commit_file("graph.ndjson", "new\n")
    assert show(temp_repo.dir, first, "graph.ndjson") == "old\r\n"
    assert show(temp_repo.dir, "HEAD", "graph.ndjson") == "new\n"
    with pytest.raises(GitError):
        show(temp_repo.dir, "HEAD", "missing")


def test_resolves_revisions_and_tests_ancestry(temp_repo: TempRepo) -> None:
    first = temp_repo.commit_file("a", "1")
    second = temp_repo.commit_file("a", "2")
    assert rev_parse(temp_repo.dir, "HEAD") == second
    assert rev_parse(temp_repo.dir, "HEAD~1") == first
    assert rev_parse(temp_repo.dir, "no-such-branch") is None
    assert is_ancestor(temp_repo.dir, first, second)
    assert not is_ancestor(temp_repo.dir, second, first)
    assert is_ancestor(temp_repo.dir, second, second)

    # A rewritten history: the old HEAD is no longer an ancestor of the new one.
    git(temp_repo.dir, ["reset", "--quiet", "--hard", first])
    rewritten = temp_repo.commit_file("a", "3")
    assert not is_ancestor(temp_repo.dir, second, rewritten)


def test_git_error_carries_the_exit_code_and_stderr(temp_repo: TempRepo) -> None:
    with pytest.raises(GitError) as caught:
        git(temp_repo.dir, ["merge-base", "--is-ancestor", "nope", "HEAD"])
    assert caught.value.exit_code == 128
    assert "nope" in caught.value.stderr
    with pytest.raises(GitError):
        is_ancestor(temp_repo.dir, "nope", "HEAD")
