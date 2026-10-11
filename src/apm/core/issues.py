"""Pydantic validation errors as one `<path>: <problem>` line each."""

from pydantic_core import ErrorDetails


def format_issue(error: ErrorDetails) -> str:
    """For example `code.repos.app.path: Field required` or `depends_on[0]: must be a ...`."""
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
