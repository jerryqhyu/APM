"""Proposals (plans.md §7.2): a breakdown an agent suggests, held until you accept it.

Planning never commits on its own. The decomposer returns JSON in the shape below; accepting it
turns the children into real nodes in one `decompose` commit.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from apm.core.errors import ApmError, format_issue
from apm.core.files import render_body
from apm.core.graph import Graph
from apm.core.ids import HandleError
from apm.core.model import Kind, NodeId
from apm.core.mutate import Mutation, OpError
from apm.core.ops import NewNode, decompose
from apm.core.project import Project, load_graph
from apm.core.store import Proposal, Store

NonEmpty = Annotated[str, Field(min_length=1)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class ProposedChild(_Model):
    key: NonEmpty
    """A temporary name, unique within the proposal: the node has no id yet."""
    title: NonEmpty
    kind: Kind = Kind.WORK
    intent: str = ""
    acceptance: list[str] = Field(default_factory=list)
    depends_on_keys: list[str] = Field(default_factory=list)
    """Other children of this proposal."""
    depends_on_ids: list[str] = Field(default_factory=list)
    """Existing nodes, by id or handle."""


class Decomposition(_Model):
    rationale: str = ""
    children: list[ProposedChild]
    warnings: list[str] = Field(default_factory=list)


class ReviewIssue(_Model):
    severity: str = "minor"
    child_key: str | None = None
    problem: NonEmpty
    suggestion: str = ""


class Review(_Model):
    """What the critic returns."""

    verdict: str = "accept"
    """`accept` or `revise`."""
    summary: str = ""
    issues: list[ReviewIssue] = Field(default_factory=list)


# Written out by hand rather than generated: this is exactly what `claude --json-schema` gets.
DECOMPOSITION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["rationale", "children", "warnings"],
    "properties": {
        "rationale": {"type": "string", "description": "Why this breakdown, in a few sentences."},
        "children": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "key",
                    "title",
                    "kind",
                    "intent",
                    "acceptance",
                    "depends_on_keys",
                    "depends_on_ids",
                ],
                "properties": {
                    "key": {"type": "string", "description": "Short temporary name, e.g. 'a'."},
                    "title": {"type": "string"},
                    "kind": {"type": "string", "enum": ["work", "decision"]},
                    "intent": {"type": "string", "description": "Why it exists; the outcome."},
                    "acceptance": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Testable criteria.",
                    },
                    "depends_on_keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Keys of other children this one needs first.",
                    },
                    "depends_on_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Handles or ids of existing nodes this one needs first.",
                    },
                },
            },
        },
        "warnings": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Anything the user should know: overlaps, open questions, risks.",
        },
    },
}

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "summary", "issues"],
    "properties": {
        "verdict": {"type": "string", "enum": ["accept", "revise"]},
        "summary": {"type": "string"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["severity", "child_key", "problem", "suggestion"],
                "properties": {
                    "severity": {"type": "string", "enum": ["major", "minor"]},
                    "child_key": {"type": ["string", "null"]},
                    "problem": {"type": "string"},
                    "suggestion": {"type": "string"},
                },
            },
        },
    },
}


class ProposalError(ApmError, ValueError):
    """The proposal can't be read or applied as it is."""


class ProposalResolvedError(ApmError, ValueError):
    """The proposal was already accepted or rejected."""

    http_status = 409


def parse_decomposition(data: object) -> Decomposition:
    try:
        decomposition = Decomposition.model_validate(data)
    except ValidationError as err:
        problems = "; ".join(format_issue(e) for e in err.errors())
        raise ProposalError(f"not a valid decomposition: {problems}") from None
    if not decomposition.children:
        raise ProposalError("the decomposition has no children")
    return decomposition


def parse_review(data: object) -> Review:
    try:
        return Review.model_validate(data)
    except ValidationError as err:
        problems = "; ".join(format_issue(e) for e in err.errors())
        raise ProposalError(f"not a valid review: {problems}") from None


def accept_decomposition(
    project: Project,
    parent: NodeId,
    decomposition: Decomposition,
    *,
    actor: str = "user",
    run: str | None = None,
) -> Mutation:
    """Turns the proposed children into nodes under `parent`, in one commit. Additive only."""
    graph = load_graph(project)
    children = [_new_node(graph, child) for child in decomposition.children]
    try:
        return decompose(
            project, parent, children, actor=actor, run=run, note=decomposition.rationale
        )
    except OpError as err:
        raise ProposalError(str(err)) from None


def _new_node(graph: Graph, child: ProposedChild) -> NewNode:
    deps = []
    for ref in child.depends_on_ids:
        try:
            deps.append(graph.resolve(ref))
        except HandleError as err:
            raise ProposalError(f'"{child.title}": {err}') from None
    return NewNode(
        title=child.title,
        kind=child.kind,
        key=child.key,
        depends_on=deps,
        depends_on_keys=child.depends_on_keys,
        body=render_body(intent=child.intent, acceptance=child.acceptance),
    )


def accept_proposal(
    project: Project,
    store: Store,
    proposal_id: str,
    children: list[dict[str, Any]] | None = None,
) -> Mutation:
    """Accepts a stored proposal: its children become nodes in one commit. Pass `children` to
    accept an edited or trimmed list instead of the original. If it can't be applied, the
    proposal stays pending.
    """
    proposal = _claim(store, proposal_id, "accepted")
    try:
        data = dict(proposal.data)
        if children is not None:
            data["children"] = children
        run = store.get_run(proposal.run_id) if proposal.run_id else None
        return accept_decomposition(
            project,
            proposal.node_id,
            parse_decomposition(data),
            actor=f"agent:{run.agent}" if run else "user",
            run=proposal.run_id,
        )
    except BaseException:
        store.reopen_proposal(proposal_id)
        raise


def reject_proposal(store: Store, proposal_id: str) -> Proposal:
    return _claim(store, proposal_id, "rejected")


def _claim(store: Store, proposal_id: str, status: Literal["accepted", "rejected"]) -> Proposal:
    """Marks a pending proposal resolved first, so two callers can't both act on it."""
    try:
        return store.resolve_proposal(proposal_id, status)
    except ValueError as err:
        raise ProposalResolvedError(str(err)) from None
