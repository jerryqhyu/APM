"""What the task reviewer returns: one verdict on whether a task is small enough to build.

A task should need at most the task size limit in lines of real source code: the project's
`features.task_max_lines`, 50 by default (`apm.core.settings.feature_settings` works out the limit
for a task, and the `apm-task-sizing` skill says what counts). The reviewer reads the task and the
code it touches and answers with exactly one of:

- `pass`: small enough to build as it is.
- `too_big`: with a split into two or more smaller tasks, shaped like a decomposer's children.
- `unclear`: too vague to estimate, with what is missing.

The model is strict: anything else, or anything extra, is rejected.
"""

from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from apm.core.errors import ApmError, format_issue
from apm.core.proposals import DECOMPOSITION_SCHEMA, ProposedChild

NonEmpty = Annotated[str, Field(min_length=1)]


class VerdictRefused(ApmError, ValueError):  # noqa: N818 (a refusal, not a failure)
    """The reviewer's answer is not one valid task verdict."""


class SplitChild(ProposedChild):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TaskVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: Literal["pass", "too_big", "unclear"]
    summary: str = ""
    children: list[SplitChild] = Field(default_factory=list)
    """For `too_big`: the proposed smaller tasks."""
    missing: list[NonEmpty] = Field(default_factory=list)
    """For `unclear`: what the task would need to say."""

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.verdict == "too_big" and len(self.children) < 2:
            raise ValueError("too_big needs at least two proposed child tasks")
        if self.verdict == "unclear" and not self.missing:
            raise ValueError("unclear needs a non-empty list of what is missing")
        return self


# Written out by hand, like the other schemas: this is exactly what `claude --json-schema` gets.
TASK_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "summary", "children", "missing"],
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "too_big", "unclear"]},
        "summary": {"type": "string", "description": "The verdict, the estimate and why."},
        # too_big only: two or more smaller tasks. Empty otherwise.
        "children": DECOMPOSITION_SCHEMA["properties"]["children"],
        # unclear only: what the task must say. Empty otherwise.
        "missing": {"type": "array", "items": {"type": "string"}},
    },
}


def parse_task_verdict(data: object) -> TaskVerdict:
    try:
        return TaskVerdict.model_validate(data)
    except ValidationError as err:
        problems = "; ".join(format_issue(e) for e in err.errors())
        raise VerdictRefused(f"not a valid task verdict: {problems}") from None
