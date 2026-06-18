"""Typer CLI entrypoint for ship-log.

M1 wires up the package skeleton: ``--version`` and a friendly ``hello`` banner.
Real subcommands (init/add/ls/show/brief) land in later milestones.
"""

from __future__ import annotations

import typer
from rich.console import Console

from . import __version__

app = typer.Typer(
    name="shiplog",
    help="A git-native captain's log for the multi-agent era. ⚓",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()

_BANNER = r"""
      ___
     |   |  ship-log 🧭⚓
   __|___|__   the captain's log of your repo's voyage
  \_o_o_o_o_/  append-only · plain-text · agent-native
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"shiplog {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show the shiplog version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ship-log — log what's been tried so nobody re-litigates a dead-end."""


@app.command()
def hello(
    name: str = typer.Option(
        "sailor",
        "--name",
        "-n",
        help="Who to greet.",
    ),
) -> None:
    """Print a friendly banner — proof the install works."""
    console.print(_BANNER, style="cyan")
    console.print(f"Ahoy, {name}! ship-log v{__version__} is aboard. ⚓", style="bold")
    console.print(
        "Next stop: [bold]shiplog init[/bold] (coming in M3). "
        "For now, see PLAN.md for the voyage.",
        style="dim",
    )


if __name__ == "__main__":  # pragma: no cover
    app()
