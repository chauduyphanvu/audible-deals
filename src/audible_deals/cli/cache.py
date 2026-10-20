"""Inspect and clear the local databases and caches."""

from __future__ import annotations

import datetime
import json as json_mod
import time

import click

from audible_deals import cache_store, db, json_import, paths
from audible_deals.presentation.terminal import console, safe_markup


def _age(stored_at: float) -> str:
    seconds = max(0, int(time.time() - stored_at))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


def _report() -> dict:
    directory, why = paths.data_dir_with_reason()
    conn = db.state()
    counts = db.table_counts(conn, db.STATE_TABLES)
    session = conn.execute(
        "SELECT saved_at FROM result_sessions WHERE slot = 1"
    ).fetchone()
    cache_conn = db.cache()
    return {
        "data_dir": str(directory),
        "data_dir_reason": why,
        "state_db": {
            "path": str(paths.state_db_file()),
            "schema": conn.execute("PRAGMA user_version").fetchone()[0],
            "bytes": db.file_size(paths.state_db_file()),
            "rows": counts,
            "last_result_session_saved_at": session[0] if session else None,
            "json_import": json_import.summary(conn),
        },
        "cache_db": {
            "path": str(paths.cache_db_file()),
            "schema": cache_conn.execute("PRAGMA user_version").fetchone()[0],
            "bytes": db.file_size(paths.cache_db_file()),
            "namespaces": {
                name: {
                    "entries": count,
                    "newest": datetime.datetime.fromtimestamp(
                        newest, datetime.UTC
                    ).isoformat(timespec="seconds"),
                }
                for name, (count, newest) in cache_store.summary().items()
            },
        },
    }


@click.group(invoke_without_command=True)
@click.pass_context
def cache(ctx):
    """Inspect and clear local databases and caches.

    Your records (price history, seen and dismissed titles, the last result
    session, tracking state) live in state.db. Rebuildable data (the category
    list, the taste profile) lives in cache.db; 'deals cache clear' empties
    only that one.
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(cache_info)


@cache.command("info")
@click.option("--json", "json_flag", is_flag=True, help="Emit the report as JSON")
def cache_info(json_flag):
    """Show where data is kept and what is stored in each database."""
    report = _report()
    if json_flag:
        click.echo(
            json_mod.dumps(report, indent=2, ensure_ascii=False, allow_nan=False)
        )
        return
    state = report["state_db"]
    console.print(
        f"[bold]Data directory[/bold] {safe_markup(report['data_dir'])} "
        f"[dim]({safe_markup(report['data_dir_reason'])})[/dim]"
    )
    console.print(
        f"[bold]state.db[/bold] schema {state['schema']}, {state['bytes']:,} bytes "
        "[dim](your records; never cleared by 'deals cache clear')[/dim]"
    )
    for table, count in state["rows"].items():
        console.print(f"  {safe_markup(table):<22} {count:>8,}")
    if state["last_result_session_saved_at"]:
        console.print(
            f"  last results saved {safe_markup(state['last_result_session_saved_at'])}"
        )
    cache_db = report["cache_db"]
    console.print(
        f"[bold]cache.db[/bold] schema {cache_db['schema']}, {cache_db['bytes']:,} bytes "
        "[dim](disposable)[/dim]"
    )
    if not cache_db["namespaces"]:
        console.print("  [dim]empty[/dim]")
    for name, info in cache_db["namespaces"].items():
        console.print(
            f"  {safe_markup(name):<22} {info['entries']:>8,}  newest {safe_markup(info['newest'])}"
        )


@cache.command("clear")
def cache_clear():
    """Empty cache.db (category lists, taste profile). Your records are untouched."""
    cache_file = paths.cache_db_file()
    before = db.file_size(cache_file)
    removed = cache_store.clear()
    cache_store.compact()
    freed = max(0, before - db.file_size(cache_file))
    detail = f", freed {freed:,} bytes" if freed else ""
    if removed:
        console.print(f"[green]Cleared {removed} cached item(s){detail}.[/green]")
    elif freed:
        console.print(f"[dim]The cache was already empty; freed {freed:,} bytes.[/dim]")
    else:
        console.print("[dim]The cache was already empty.[/dim]")
