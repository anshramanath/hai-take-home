"""CLI entry point."""

from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from harness.app import DEFAULT_DB_PATH, Harness
from harness.audit.explain import explain as render_explain
from harness.demo import run_demo
from harness.planning.llm import OpenAIClient, ReplayClient
from harness.world.receipts import record_receipt
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
    if result["fired_tasks"]:
        console.print(f"  Fired scheduled tasks: {result['fired_tasks']}")
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


@app.command()
def receive(
    po_id: str,
    qty: int,
    db: Path = DbOption,
) -> None:
    """Record a receipt against a PO (an external-world event: the
    warehouse scanning in a shipment, not an agent action).
    """

    if not db.exists():
        console.print(f"[red]No database at {db}. Run 'reset' first.[/red]")
        raise typer.Exit(code=1)
    harness = Harness(db)
    receipt_id = record_receipt(harness.conn, harness.clock, po_id=po_id, qty=qty)
    console.print(f"[green]Recorded[/green] receipt {receipt_id}: {qty} units against {po_id}.")


@app.command()
def explain(
    run: str = typer.Option(None, "--run", help="Only show this run_id's events"),
    db: Path = DbOption,
) -> None:
    """Print the audit log as a human-readable narrative, in order."""

    if not db.exists():
        console.print(f"[red]No database at {db}. Run 'reset' first.[/red]")
        raise typer.Exit(code=1)
    harness = Harness(db)
    for line in render_explain(harness.conn, run):
        console.print(line)


@app.command()
def demo(
    interactive: bool = typer.Option(False, "--interactive", help="Pause at approvals for you to decide"),
) -> None:
    """Run Scenario A through approval, escalation, execution, and the
    follow-up; then Scenario B; then the failure cases; then explain.
    Uses the real API if OPENAI_API_KEY is set, otherwise replays a
    recorded run.
    """

    llm_client = _llm_client_for_cli()
    run_demo(console, llm_client, interactive=interactive)


if __name__ == "__main__":
    app()
