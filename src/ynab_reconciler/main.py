"""CLI entry point for ynab-reconciler."""
from __future__ import annotations

import os
import readline  # noqa: F401 — enables arrow-key editing in click.prompt
import sys
from typing import Optional

import click
import requests
from dotenv import load_dotenv

from . import ui
from .api.client import YnabClient, YnabError
from .api.models import Account, CategoryGroup
from .currency import convert_currency
from .reconciler import AccountReconciliationResult, milliunits, reconcile_account

load_dotenv()


def get_client() -> YnabClient:
    token = os.environ.get("YNAB_TOKEN")
    if not token:
        raise click.UsageError(
            "YNAB_TOKEN environment variable is not set. "
            "Set it in your shell or in a .env file."
        )
    return YnabClient(token)


def resolve_plan_id(client: YnabClient, plan_id: Optional[str]) -> str:
    """Return plan_id if given; otherwise list plans and prompt interactively."""
    if plan_id:
        return plan_id

    if not sys.stdin.isatty():
        raise click.UsageError(
            "No plan specified and not running interactively — "
            "set YNAB_PLAN_ID or pass --plan."
        )

    with ui.spinner("Fetching plans…"):
        plans = client.get_plans()
    if not plans:
        raise click.UsageError("No plans found on this YNAB account.")

    if len(plans) == 1:
        only = plans[0]
        click.echo(f"Using the only available plan: {only.name}")
        return only.id

    return ui.select_plan(plans).id


def resolve_category_group(
    client: YnabClient,
    plan_id: str,
    category_group_id: Optional[str],
) -> CategoryGroup:
    """Return a CategoryGroup; pick interactively if id is missing or not found in plan."""
    with ui.spinner("Fetching category groups…"):
        groups = client.get_category_groups(plan_id)
    visible = [
        g for g in groups
        if not g.deleted and not g.hidden
        and any(not c.deleted and not c.hidden for c in g.categories)
    ]
    if not visible:
        raise click.UsageError("No category groups with visible categories in this plan.")

    if category_group_id:
        match = next((g for g in visible if g.id == category_group_id), None)
        if match is not None:
            return match
        click.echo(
            f"Category group '{category_group_id}' not found in this plan.", err=True
        )

    if not sys.stdin.isatty():
        raise click.UsageError(
            "No (valid) category group specified and not running interactively — "
            "set YNAB_CATEGORY_GROUP_ID or pass --category-group."
        )

    if len(visible) == 1:
        only = visible[0]
        click.echo(f"Using the only available category group: {only.name}")
        return only

    return ui.select_category_group(visible)


def fmt_amount(amount: float) -> str:
    return f"{amount:,.0f}"


@click.group()
def cli() -> None:
    """Reconcile YNAB accounts from the command line."""


@cli.command("plans")
def cmd_plans() -> None:
    """List all plans (budgets)."""
    try:
        client = get_client()
        plans = client.get_plans()
    except (YnabError, click.UsageError) as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    if not plans:
        click.echo("No plans found.")
        return

    default_plan_id = os.environ.get("YNAB_PLAN_ID")
    for plan in plans:
        marker = " (default)" if plan.id == default_plan_id else ""
        click.echo(f"{plan.id}  {plan.name}{marker}")


@cli.command("payees")
@click.option("--plan", "plan_id", envvar="YNAB_PLAN_ID", default=None, help="Plan ID (overrides YNAB_PLAN_ID).")
def cmd_payees(plan_id: Optional[str]) -> None:
    """List payees in a plan."""
    try:
        client = get_client()
        plan_id = resolve_plan_id(client, plan_id)
        payees = client.get_payees(plan_id)
    except (YnabError, click.UsageError) as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    visible = [p for p in payees if not p.deleted]
    if not visible:
        click.echo("No payees found.")
        return

    default_payee_id = os.environ.get("YNAB_PAYEE_ID")
    for payee in sorted(visible, key=lambda p: p.name.lower()):
        marker = " (default)" if payee.id == default_payee_id else ""
        click.echo(f"{payee.id}  {payee.name}{marker}")


@cli.command("category-groups")
@click.option("--plan", "plan_id", envvar="YNAB_PLAN_ID", default=None, help="Plan ID (overrides YNAB_PLAN_ID).")
def cmd_category_groups(plan_id: Optional[str]) -> None:
    """List category groups (and their categories) in a plan."""
    try:
        client = get_client()
        plan_id = resolve_plan_id(client, plan_id)
        groups = client.get_category_groups(plan_id)
    except (YnabError, click.UsageError) as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    visible = [g for g in groups if not g.deleted and not g.hidden]
    if not visible:
        click.echo("No category groups found.")
        return

    default_group_id = os.environ.get("YNAB_CATEGORY_GROUP_ID")
    for group in visible:
        marker = " (default)" if group.id == default_group_id else ""
        click.echo(f"{group.id}  {group.name}{marker}")
        for cat in group.categories:
            if not cat.deleted and not cat.hidden:
                click.echo(f"    {cat.name}")


@cli.command("accounts")
@click.option("--plan", "plan_id", envvar="YNAB_PLAN_ID", default=None, help="Plan ID (overrides YNAB_PLAN_ID).")
@click.option("--include-closed", is_flag=True, help="Include closed accounts.")
def cmd_accounts(plan_id: Optional[str], include_closed: bool) -> None:
    """List accounts in a plan."""
    try:
        client = get_client()
        plan_id = resolve_plan_id(client, plan_id)
        accounts = client.get_accounts(plan_id)
    except (YnabError, click.UsageError) as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    visible = [a for a in accounts if not a.deleted]
    if not include_closed:
        visible = [a for a in visible if not a.closed]

    if not visible:
        click.echo("No accounts found.")
        return

    for account in visible:
        last_rec = account.last_reconciled_at or "never"
        click.echo(
            f"{account.id}  {account.name:<30}  "
            f"cleared: {fmt_amount(account.cleared_balance_in_units()):>12}  "
            f"last reconciled: {last_rec}"
        )


def _reconcile_one(
    client: YnabClient,
    plan_id: str,
    account: Account,
    native_iso: Optional[str],
    categories,
    payee_id: Optional[str],
    no_adjust: bool,
) -> tuple[str, Optional[AccountReconciliationResult]]:
    """Reconcile a single account interactively.

    Returns ('done', result) | ('skip', None) | ('quit', None).
    'quit' means: return to menu (and stop all-mode early).
    """
    ui.show_account_header(account, native_iso)

    answer = ui.ask_statement_balance(account)
    if answer == "quit":
        return ("quit", None)
    if answer == "skip":
        ui.show_skipped()
        return ("skip", None)

    amount, input_iso = answer  # type: ignore[misc]
    memo = None
    if input_iso and native_iso and input_iso != native_iso:
        try:
            with ui.spinner(f"Converting {input_iso} → {native_iso}…"):
                converted = convert_currency(amount, input_iso, native_iso)
        except (ValueError, requests.RequestException) as e:
            ui.show_error(f"Currency conversion error: {e}")
            return ("skip", None)
        rate = converted / amount
        ui.show_conversion(amount, input_iso, converted, native_iso, rate)
        statement_balance = converted
        memo = f"Reconciliation adjustment ({amount:.0f} {input_iso} @ {rate:.0f})"
    else:
        statement_balance = amount

    current = account.cleared_balance_in_units()
    adjustment = statement_balance - current
    if not no_adjust and abs(current) > 0 and abs(adjustment) / abs(current) > 0.30:
        if not ui.confirm_large_adjustment(adjustment, current):
            ui.show_skipped()
            return ("skip", None)

    try:
        with ui.spinner("Posting adjustment…" if abs(adjustment) >= 0.001 else "Reconciling…"):
            result = reconcile_account(
                client,
                plan_id,
                account,
                statement_balance,
                create_adjustment=not no_adjust,
                payee_id=payee_id,
                categories=categories,
                memo=memo,
            )
    except YnabError as e:
        ui.show_error(str(e))
        return ("skip", None)

    if result.adjustment_created:
        delta = milliunits(result.adjustment_in_units)
        account.cleared_balance += delta
        account.balance += delta

    ui.show_account_result(result, no_adjust)
    return ("done", result)


@cli.command("reconcile")
@click.option("--plan", "plan_id", envvar="YNAB_PLAN_ID", default=None, help="Plan ID (overrides YNAB_PLAN_ID).")
@click.option("--payee", "payee_id", envvar="YNAB_PAYEE_ID", default=None, help="Payee ID for adjustment transactions (overrides YNAB_PAYEE_ID).")
@click.option("--category-group", "category_group_id", envvar="YNAB_CATEGORY_GROUP_ID", default=None, help="Category group ID whose categories are used for split adjustments (overrides YNAB_CATEGORY_GROUP_ID).")
@click.option(
    "--no-adjust",
    is_flag=True,
    help="Show discrepancies but do not create adjustment transactions.",
)
def cmd_reconcile(plan_id: Optional[str], payee_id: Optional[str], category_group_id: Optional[str], no_adjust: bool) -> None:
    """Reconcile accounts in a plan.

    Lists the plan's open accounts and lets you pick which to reconcile:
    enter the account's number to do just that one, press Enter (or 'a')
    to walk through all remaining accounts, or 'q' to quit. Already-reconciled
    accounts are marked '[x]' in the menu alongside the adjustment outcome;
    picking one again re-runs the prompt.
    """
    try:
        client = get_client()
        plan_id = resolve_plan_id(client, plan_id)
        with ui.spinner("Loading plan…"):
            accounts = client.get_accounts(plan_id)
            plan = client.get_plan(plan_id)
        native_iso = plan.iso_code
    except (YnabError, click.UsageError) as e:
        ui.show_error(str(e))
        sys.exit(1)

    try:
        group = resolve_category_group(client, plan_id, category_group_id)
    except (YnabError, click.UsageError) as e:
        ui.show_error(str(e))
        sys.exit(1)
    categories = [c for c in group.categories if not c.deleted and not c.hidden]

    candidates = [a for a in accounts if not a.deleted and not a.closed]
    if not candidates:
        click.echo("No open accounts found in this plan.")
        return

    ui.banner(plan.name, native_iso)

    statuses: dict[str, AccountReconciliationResult] = {}
    by_id = {a.id: a for a in candidates}

    while True:
        choice = ui.select_account(candidates, statuses)

        if choice == "quit":
            break

        if choice == "all":
            for acct in candidates:
                if acct.id in statuses:
                    continue
                status, result = _reconcile_one(
                    client, plan_id, acct, native_iso, categories, payee_id, no_adjust
                )
                if status == "done" and result is not None:
                    statuses[acct.id] = result
                if status == "quit":
                    break
            continue

        acct = by_id.get(choice)
        if acct is None:
            continue
        status, result = _reconcile_one(
            client, plan_id, acct, native_iso, categories, payee_id, no_adjust
        )
        if status == "done" and result is not None:
            statuses[acct.id] = result

    ui.show_run_summary(list(statuses.values()))
