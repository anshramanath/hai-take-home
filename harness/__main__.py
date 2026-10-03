"""CLI entry point. `explain` and `demo` are added in later phases."""

from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from harness.app import DEFAULT_DB_PATH, Harness
from harness.planning.llm import OpenAIClient, ReplayClient
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


def _llm_client_for_cli():
    if os.environ.get("OPENAI_API_KEY"):
        return OpenAIClient()
    from harness.app import DEFAULT_REPLAY_PATH

    if DEFAULT_REPLAY_PATH.exists():
        console.print(f"[yellow]No OPENAI_API_KEY set; replaying recorded responses from {DEFAULT_REPLAY_PATH}[/yellow]")
        return ReplayClient(DEFAULT_REPLAY_PATH)
    console.print("[red]No OPENAI_API_KEY set and no replay fixture available.[/red]")
    raise typer.Exit(code=1)


@app.command()
def tick(db: Path = DbOption) -> None:
    """Advance one simulated day: escalate overdue approvals, resume any
    crashed workflows, run detectors, and plan for anything new.
    """

    if not db.exists():
        console.print(f"[red]No database at {db}. Run 'reset' first.[/red]")
        raise typer.Exit(code=1)
    harness = Harness(db)
    llm_client = _llm_client_for_cli()
    result = harness.tick(llm_client)
    console.print(f"[bold]Tick for {result['today']}[/bold]")
    if result["escalated"]:
        console.print(f"  Escalated approvals: {result['escalated']}")
    if result["resumed_workflows"]:
        console.print(f"  Resumed workflow instances: {result['resumed_workflows']}")
    if result["new_items"]:
        console.print(f"  New attention items: {result['new_items']}")
        console.print(f"  Runs started: {result['runs']}")
    else:
        console.print("  No new attention items.")
    console.print(f"Today is now {harness.clock.today().isoformat()}.")


@app.command()
def approve(
    approval_id: str,
    decided_by: str = typer.Option(..., "--as", help="user_id of the approver deciding this"),
    db: Path = DbOption,
) -> None:
    """Approve a pending approval and, if it belongs to a workflow, execute it."""

    if not db.exists():
        console.print(f"[red]No database at {db}. Run 'reset' first.[/red]")
        raise typer.Exit(code=1)
    harness = Harness(db)
    llm_client = _llm_client_for_cli()
    harness.approve(approval_id, decided_by, llm_client)
    console.print(f"[green]Approved[/green] {approval_id} as {decided_by}.")


@app.command()
def reject(
    approval_id: str,
    decided_by: str = typer.Option(..., "--as", help="user_id of the approver deciding this"),
    db: Path = DbOption,
) -> None:
    """Reject a pending approval. Nothing is written; the run is closed."""

    if not db.exists():
        console.print(f"[red]No database at {db}. Run 'reset' first.[/red]")
        raise typer.Exit(code=1)
    harness = Harness(db)
    harness.reject(approval_id, decided_by)
    console.print(f"[yellow]Rejected[/yellow] {approval_id} as {decided_by}.")


if __name__ == "__main__":
    app()
