"""The `apm` command (plans.md §10). Commands are added with the phases that make them work."""

from typing import Annotated

import typer

from apm import __version__

app = typer.Typer(
    name="apm",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit


@app.callback()
def root(
    version: Annotated[
        bool,
        typer.Option(
            "--version", "-v", callback=_print_version, is_eager=True, help="Print the version."
        ),
    ] = False,
) -> None:
    """A local-first project manager built for working with AI agents."""


def main() -> None:
    app()
