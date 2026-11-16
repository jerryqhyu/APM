from typing import Any

import pytest

from apm.core.body import read_body
from apm.core.git import log
from apm.core.ids import handle
from apm.core.model import Kind
from apm.core.ops import NewNode, create_nodes
from apm.core.project import Project, load_graph
from apm.core.proposals import (
    DECOMPOSITION_SCHEMA,
    ProposalError,
    accept_decomposition,
    parse_decomposition,
    parse_review,
)
from apm.core.validate import InvariantError

PROPOSAL: dict[str, Any] = {
    "rationale": "Parse first, then write.",
    "children": [
        {
            "key": "parse",
            "title": "Parse the file",
            "kind": "work",
            "intent": "Read graph.ndjson.",
            "acceptance": ["rejects bad lines", "reports line numbers"],
            "depends_on_keys": [],
            "depends_on_ids": [],
        },
        {
            "key": "format",
            "title": "Pick the output format",
            "kind": "decision",
            "intent": "",
            "acceptance": [],
            "depends_on_keys": ["parse"],
            "depends_on_ids": [],
        },
    ],
    "warnings": [],
}


def test_accepting_creates_the_children_in_one_commit(project: Project) -> None:
    parent, other = create_nodes(
        project, [NewNode(title="File I/O"), NewNode(title="Other")]
    ).created
    data = {**PROPOSAL, "children": [dict(c) for c in PROPOSAL["children"]]}
    data["children"][1]["depends_on_ids"] = [handle(other)]
    commits = len(log(project.apm_dir))

    result = accept_decomposition(
        project, parent, parse_decomposition(data), actor="agent:apm-decomposer", run="r1"
    )

    parse, fmt = result.created
    graph = load_graph(project)
    assert graph.children(parent) == (parse, fmt)
    assert graph[fmt].kind is Kind.DECISION
    assert graph[fmt].depends_on == tuple(sorted([other, parse]))
    assert read_body(project.apm_dir, parse) == (
        "## Intent\n\nRead graph.ndjson.\n\n"
        "## Acceptance criteria\n\n- [ ] rejects bad lines\n- [ ] reports line numbers\n\n"
        "## Notes\n"
    )
    assert len(log(project.apm_dir)) == commits + 1
    entry = log(project.apm_dir)[0]
    assert entry.subject == f"decompose {handle(parent)}: 2 children"
    assert entry.trailers["APM-Run"] == "r1"
    assert entry.trailers["APM-Actor"] == "agent:apm-decomposer"


@pytest.mark.parametrize(
    ("change", "error", "message"),
    [
        ({"depends_on_keys": ["nope"]}, ProposalError, 'unknown key "nope"'),
        ({"depends_on_ids": ["zzzz"]}, ProposalError, "invalid handle"),
        ({"depends_on_ids": ["00000001"]}, ProposalError, "no node matches"),
        ({"depends_on_keys": ["format"]}, InvariantError, "I2: dependency cycle"),
        ({"key": "format"}, ProposalError, 'duplicate key "format"'),
    ],
)
def test_a_proposal_that_cannot_apply_changes_nothing(
    project: Project, change: dict, error: type[Exception], message: str
) -> None:
    (parent,) = create_nodes(project, [NewNode(title="Parent")]).created
    children = [dict(c) for c in PROPOSAL["children"]]
    children[0].update(change)
    with pytest.raises(error, match=message):
        accept_decomposition(project, parent, parse_decomposition({"children": children}))
    assert len(load_graph(project)) == 1


def test_parsing_reports_what_is_wrong() -> None:
    with pytest.raises(ProposalError, match=r"children\[0\]\.title: Field required"):
        parse_decomposition({"children": [{"key": "a"}]})
    with pytest.raises(ProposalError, match="has no children"):
        parse_decomposition({"rationale": "nothing", "children": []})
    with pytest.raises(ProposalError, match="not a valid decomposition"):
        parse_decomposition("oops")
    # Extra fields from a chatty agent are ignored, and defaults fill the gaps.
    child = parse_decomposition({"children": [{"key": "a", "title": "A", "x": 1}]}).children[0]
    assert (child.kind, child.intent, child.acceptance) == (Kind.WORK, "", [])


def test_parsing_a_review() -> None:
    review = parse_review(
        {
            "verdict": "revise",
            "summary": "Overlap.",
            "issues": [{"severity": "major", "child_key": "a", "problem": "dup", "suggestion": ""}],
        }
    )
    assert review.verdict == "revise"
    assert review.issues[0].child_key == "a"
    with pytest.raises(ProposalError, match="not a valid review"):
        parse_review({"issues": [{}]})


def test_the_schema_matches_the_model() -> None:
    child = DECOMPOSITION_SCHEMA["properties"]["children"]["items"]
    assert set(child["required"]) == set(child["properties"])
    assert parse_decomposition(PROPOSAL).children[0].key == "parse"
