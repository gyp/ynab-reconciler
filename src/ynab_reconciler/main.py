"""CLI entry point for ynab-reconciler."""
from __future__ import annotations

import os
import sys
from typing import Optional

import click
import requests
from dotenv import load_dotenv

from .api.client import YnabClient, YnabError
from .api.models import Account
from .currency import convert_currency, parse_statement_input
from .reconciler import reconcile_account

load_dotenv()


def get_client() -> YnabClient:
    token = os.environ.get("YNAB_TOKEN")
    if not token:
        raise click.UsageError(
            "YNAB_TOKEN environment variable is not set. "
            "Set it in your shell or in a .env file."
        )
    return YnabClient(token)


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
@click.option("--plan", "plan_id", envvar="YNAB_PLAN_ID", required=True, help="Plan ID (overrides YNAB_PLAN_ID).")
def cmd_payees(plan_id: str) -> None:
    """List payees in a plan."""
    try:
        client = get_client()
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
@click.option("--plan", "plan_id", envvar="YNAB_PLAN_ID", required=True, help="Plan ID (overrides YNAB_PLAN_ID).")
def cmd_category_groups(plan_id: str) -> None:
    """List category groups (and their categories) in a plan."""
    try:
        client = get_client()
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
@click.option("--plan", "plan_id", envvar="YNAB_PLAN_ID", required=True, help="Plan ID (overrides YNAB_PLAN_ID).")
@click.option("--include-closed", is_flag=True, help="Include closed accounts.")
def cmd_accounts(plan_id: str, include_closed: bool) -> None:
    """List accounts in a plan."""
    try:
        client = get_client()
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


@cli.command("reconcile")
@click.option("--plan", "plan_id", envvar="YNAB_PLAN_ID", required=True, help="Plan ID (overrides YNAB_PLAN_ID).")
@click.option("--payee", "payee_id", envvar="YNAB_PAYEE_ID", default=None, help="Payee ID for adjustment transactions (overrides YNAB_PAYEE_ID).")
@click.option("--category-group", "category_group_id", envvar="YNAB_CATEGORY_GROUP_ID", default=None, help="Category group ID whose categories are used for split adjustments (overrides YNAB_CATEGORY_GROUP_ID).")
@click.option(
    "--no-adjust",
    is_flag=True,
    help="Show discrepancies but do not create adjustment transactions.",
)
def cmd_reconcile(plan_id: str, payee_id: Optional[str], category_group_id: Optional[str], no_adjust: bool) -> None:
    """Reconcile accounts in a plan.

    For each non-closed account, shows the current YNAB cleared balance and
    asks for the actual bank statement balance. If they differ, an adjustment
    transaction is created to bring YNAB in sync.

    Press Enter to accept the YNAB balance, or type the actual balance.
    Type 's' to skip an account, 'q' to quit.
    """
    try:
        client = get_client()
        accounts = client.get_accounts(plan_id)
        plan = client.get_plan(plan_id)
        native_iso = plan.iso_code
    except (YnabError, click.UsageError) as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    categories = None
    if category_group_id:
        try:
            groups = client.get_category_groups(plan_id)
        except YnabError as e:
            click.echo(f"Error fetching categories: {e}", err=True)
            sys.exit(1)
        match = next((g for g in groups if g.id == category_group_id), None)
        if match is None:
            click.echo(f"Error: category group '{category_group_id}' not found.", err=True)
            sys.exit(1)
        categories = [c for c in match.categories if not c.deleted and not c.hidden]
        if not categories:
            click.echo(f"Error: category group '{match.name}' has no visible categories.", err=True)
            sys.exit(1)

    candidates = [a for a in accounts if not a.deleted and not a.closed]
    if not candidates:
        click.echo("No open accounts found in this plan.")
        return

    results = []
    for account in candidates:
        click.echo(f"\n{'─' * 60}")
        click.echo(account.name)
        click.echo(f"  YNAB cleared balance: {fmt_amount(account.cleared_balance_in_units())}")
        if account.uncleared_balance != 0:
            click.echo(
                f"  Uncleared (pending):  {fmt_amount(account.uncleared_balance_in_units())}"
            )

        raw = click.prompt(
            "  Statement balance [Enter to accept, s=skip, q=quit]",
            default="",
            show_default=False,
        ).strip()

        if raw.lower() == "q":
            click.echo("Quitting.")
            break
        if raw.lower() == "s":
            click.echo("  Skipped.")
            continue

        memo = None
        if raw == "":
            statement_balance = account.cleared_balance_in_units()
        else:
            try:
                amount, input_iso = parse_statement_input(raw)
            except ValueError:
                click.echo(f"  Invalid amount '{raw}', skipping.", err=True)
                continue

            if input_iso and native_iso and input_iso != native_iso:
                try:
                    converted = convert_currency(amount, input_iso, native_iso)
                except (ValueError, requests.RequestException) as e:
                    click.echo(f"  Currency conversion error: {e}", err=True)
                    continue
                rate = converted / amount
                click.echo(
                    f"  Converting {amount:,.2f} {input_iso} → {fmt_amount(converted)} {native_iso}"
                    f" (rate: {rate:.4f})"
                )
                statement_balance = converted
                memo = f"Reconciliation adjustment ({amount:.0f} {input_iso} @ {rate:.0f})"
            else:
                statement_balance = amount

        try:
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
            click.echo(f"  Error: {e}", err=True)
            continue

        if result.matched:
            click.echo("  Balances match — nothing to do.")
        elif result.adjustment_created:
            sign = "+" if result.adjustment_in_units > 0 else ""
            click.echo(
                f"  Adjustment created: {sign}{fmt_amount(result.adjustment_in_units)}"
            )
        else:
            sign = "+" if result.adjustment_in_units > 0 else ""
            click.echo(
                f"  Discrepancy: {sign}{fmt_amount(result.adjustment_in_units)} (--no-adjust, skipped)"
            )

        results.append(result)

    if results:
        adjusted = [r for r in results if r.adjustment_created]
        matched = [r for r in results if r.matched]
        click.echo(f"\n{'─' * 60}")
        click.echo(
            f"Done. {len(matched)} matched, {len(adjusted)} adjusted, "
            f"{len(results) - len(matched) - len(adjusted)} discrepancies skipped."
        )
