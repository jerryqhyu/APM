"""What the task reviewer returns: `pass`, `too_big` (with at least two proposed children) or
`unclear` (with what is missing), judged against the task size limit
(`apm.core.settings.feature_settings`). The model is strict: anything else is rejected.
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
