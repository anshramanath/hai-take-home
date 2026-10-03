"""CLI entry point. Phase 1 only wires `reset` and `status`; `tick`, `approve`,
`reject`, `explain`, and `demo` are added in later phases.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from harness.app import DEFAULT_DB_PATH, Harness
from harness.world.seed import FIXTURES

app = typer.Typer(help="Harmony agent harness CLI")
console = Console()

DbOption = typer.Option(DEFAULT_DB_PATH, "--db", help="Path to the SQLite database file")


@app.command()
def reset(
    fixture: str = typer.Option(
        "scenario_a", "--fixture", help=f"One of: {', '.join(FIXTURES)}"
    ),
    db: Path = DbOption,
) -> None:
    """Wipe the database and reseed it with a named fixture."""
    if fixture not in FIXTURES:
        console.print(f"[red]Unknown fixture '{fixture}'. Choose one of: {', '.join(FIXTURES)}[/red]")
        raise typer.Exit(code=1)
    harness = Harness(db)
    harness.reset(fixture)
    console.print(
        f"[green]Reset[/green] {db} with fixture [bold]{fixture}[/bold]. "
        f"Today is {harness.clock.today().isoformat()}."
    )


@app.command()
def status(db: Path = DbOption) -> None:
    """Show row counts for every table and the current simulated date."""
    if not db.exists():
        console.print(f"[red]No database at {db}. Run 'reset' first.[/red]")
        raise typer.Exit(code=1)
    harness = Harness(db)
    counts = harness.status()
    today = counts.pop("today")

    table = Table(title=f"Harness status ({db})")
    table.add_column("Table")
    table.add_column("Rows", justify="right")
    for name, count in counts.items():
        table.add_row(name, str(count))
    console.print(table)
    console.print(f"Today: [bold]{today}[/bold]")


if __name__ == "__main__":
    app()
