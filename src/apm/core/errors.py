"""Errors a person can cause, and how they are worded.

Everything that goes wrong because of what the user (or an agent) asked for is an `ApmError`. The
CLI prints it as one line, the MCP server returns it as the tool's error, and the HTTP API turns it
into a status and a message. Anything else is a bug and keeps its traceback.
"""

from pydantic_core import ErrorDetails


class ApmError(Exception):
    """An error that is the caller's to fix."""

    http_status = 422
    """What the HTTP API answers with."""


def format_issue(error: ErrorDetails) -> str:
    """A pydantic error as one `<path>: <problem>` line, for example
    `code.repos.app.path: Field required` or `depends_on[0]: must be a ...`.
    """
    path = ""
    message = error["msg"].removeprefix("Value error, ")
    for part in error["loc"]:
        if part == "[key]":
            message = f"invalid key: {message}"
        elif isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}" if path else part
    return f"{path}: {message}" if path else message
