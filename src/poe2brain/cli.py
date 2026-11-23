from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console

from poe2brain.mcp_server import collect_engine_status

app = typer.Typer(
    name="poe2",
    help="poe2brain — Path of Exile 2 intelligence CLI (knowledge, PoB2, trade, crafting).",
    no_args_is_help=True,
)
console = Console()


@app.callback()
def _root_callback() -> None:
    """Path of Exile 2 intelligence engine for AI agents and power users."""


@app.command("version")
def version_cmd(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output engine version and readiness status as JSON."),
    ] = False,
) -> None:
    """Show poe2brain version, target PoE2 patch, default league, and runtime readiness."""
    status = collect_engine_status()
    if json_output:
        typer.echo(status.model_dump_json(indent=2))
        return

    pob_label = "ready" if status.pob2_runtime_ready else "missing (run scripts/bootstrap_pob2.sh)"
    db_label = "ready" if status.gamedata_db_ready else "not built yet"
    console.print(
        f"[bold cyan]poe2brain[/bold cyan] v{status.version} "
        f"(PoE2 [bold]{status.patch}[/bold] | League: [bold]{status.default_league}[/bold])"
    )
    console.print(f"  PoB2 runtime : {pob_label}")
    console.print(f"  Gamedata DB  : {db_label}")
    console.print(f"  Cache dir    : {status.cache_dir}")


if __name__ == "__main__":
    app()
