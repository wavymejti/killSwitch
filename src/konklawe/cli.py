"""Command-line interface (Typer)."""

import typer

from konklawe import __version__

app = typer.Typer(
    help="Local multi-agent environment: AI coding CLIs working together, observed live.",
    no_args_is_help=True,
)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"konklawe {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", callback=_print_version, is_eager=True, help="Show version and exit."
    ),
) -> None:
    """Local multi-agent environment: AI coding CLIs working together, observed live."""
