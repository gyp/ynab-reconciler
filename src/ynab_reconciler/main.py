"""CLI entry point for ynab-reconciler."""
from __future__ import annotations

import readline  # noqa: F401 — enables arrow-key editing in click.prompt
import sys
from typing import Optional

import click
import requests

from . import config, ui
from .api.client import YnabClient, YnabError
from .api.models import Account, CategoryGroup, Payee, Plan
from .currency import convert_currency
from .reconciler import AccountReconciliationResult, milliunits, reconcile_account


def get_client() -> YnabClient:
    token = config.resolve_token()
    if not token:
        if not ui.is_interactive():
            raise click.UsageError(
                "No YNAB token configured. Run `ynab-reconciler auth login` "
                "(or `ynab-reconciler init`) to set one up."
            )
        token = _interactive_token_prompt_and_store()
    return YnabClient(token)


def _interactive_token_prompt_and_store() -> str:
    ui.show_first_run_banner()
    token = ui.prompt_password("YNAB personal access token")
    if not token:
        raise click.Abort()
    try:
        config.set_token_in_keyring(token)
        ui.show_info("Saved to OS keychain.")
    except config.KeyringUnavailable as e:
        ui.show_warning(
            f"Couldn't write to keychain ({e}). The token will only be used "
            "for this run."
        )
    return token


def _offer_save_default(key: str, value: str, label: str) -> None:
    """After an interactive pick, offer to save the choice as the default.

    Silent on non-TTY. Skips if the value is already the saved default.
    """
    if not ui.is_interactive():
        return
    cfg = config.load_config()
    if getattr(cfg, key) == value:
        return
    if ui.confirm_save_default(label):
        config.set_config_value(key, value)
        ui.show_saved_default(label)


def resolve_plan_id(client: YnabClient, plan_id: Optional[str]) -> str:
    """Return plan_id if given; otherwise list plans and prompt interactively."""
    if plan_id:
        return plan_id

    if not sys.stdin.isatty():
        raise click.UsageError(
            "No plan specified and not running interactively — "
            "pass --plan or run `ynab-reconciler config set plan_id <id>`."
        )

    with ui.spinner("Fetching plans…"):
        plans = client.get_plans()
    if not plans:
        raise click.UsageError("No plans found on this YNAB account.")

    if len(plans) == 1:
        only = plans[0]
        click.echo(f"Using the only available plan: {only.name}")
        return only.id

    chosen = ui.select_plan(plans)
    _offer_save_default("plan_id", chosen.id, "plan")
    return chosen.id


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
            "pass --category-group or run "
            "`ynab-reconciler config set category_group_id <id>`."
        )

    if len(visible) == 1:
        only = visible[0]
        click.echo(f"Using the only available category group: {only.name}")
        return only

    chosen = ui.select_category_group(visible)
    _offer_save_default("category_group_id", chosen.id, "category group")
    return chosen


def fmt_amount(amount: float) -> str:
    return f"{amount:,.0f}"


class GroupedGroup(click.Group):
    """A click.Group that renders --help with commands split into labelled sections."""

    SECTIONS: list[tuple[str, list[str]]] = [
        ("Main commands", ["init", "reconcile"]),
        ("Settings", ["auth", "config"]),
        ("Utilities", ["plans", "accounts", "payees", "category-groups"]),
    ]

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        listed: set[str] = {name for _, names in self.SECTIONS for name in names}

        def rows_for(names: list[str]) -> list[tuple[str, str]]:
            rows: list[tuple[str, str]] = []
            for name in names:
                cmd = self.get_command(ctx, name)
                if cmd is None or cmd.hidden:
                    continue
                rows.append((name, cmd.get_short_help_str(limit=60)))
            return rows

        for section, names in self.SECTIONS:
            rows = rows_for(names)
            if rows:
                with formatter.section(section):
                    formatter.write_dl(rows)

        leftover = [n for n in self.list_commands(ctx) if n not in listed]
        if leftover:
            rows = rows_for(leftover)
            if rows:
                with formatter.section("Other"):
                    formatter.write_dl(rows)


@click.group(cls=GroupedGroup, invoke_without_command=True)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Reconcile YNAB accounts from the command line.

    Computes the adjustment as (statement balance − YNAB cleared balance) and
    posts it back as a split transaction across the categories of a chosen
    category group, weighted by each category's current balance — useful for
    spreading the gains and losses of a long-term investment account across
    the saving goals it funds.

    Run without a subcommand to default to `reconcile`.
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(cmd_reconcile)


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

    cfg = config.load_config()
    default_plan_id = cfg.plan_id
    for plan in plans:
        marker = " (default)" if plan.id == default_plan_id else ""
        click.echo(f"{plan.id}  {plan.name}{marker}")


@cli.command("payees")
@click.option("--plan", "plan_id", default=None, help="Plan ID.")
def cmd_payees(plan_id: Optional[str]) -> None:
    """List payees in a plan."""
    try:
        cfg = config.load_config()
        plan_id = config.resolve_plan_id(plan_id, cfg)
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

    default_payee_id = cfg.payee_id
    for payee in sorted(visible, key=lambda p: p.name.lower()):
        marker = " (default)" if payee.id == default_payee_id else ""
        click.echo(f"{payee.id}  {payee.name}{marker}")


@cli.command("category-groups")
@click.option("--plan", "plan_id", default=None, help="Plan ID.")
def cmd_category_groups(plan_id: Optional[str]) -> None:
    """List category groups (and their categories) in a plan."""
    try:
        cfg = config.load_config()
        plan_id = config.resolve_plan_id(plan_id, cfg)
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

    default_group_id = cfg.category_group_id
    for group in visible:
        marker = " (default)" if group.id == default_group_id else ""
        click.echo(f"{group.id}  {group.name}{marker}")
        for cat in group.categories:
            if not cat.deleted and not cat.hidden:
                click.echo(f"    {cat.name}")


@cli.command("accounts")
@click.option("--plan", "plan_id", default=None, help="Plan ID.")
@click.option("--include-closed", is_flag=True, help="Include closed accounts.")
def cmd_accounts(plan_id: Optional[str], include_closed: bool) -> None:
    """List accounts in a plan."""
    try:
        cfg = config.load_config()
        plan_id = config.resolve_plan_id(plan_id, cfg)
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
@click.option("--plan", "plan_id", default=None, help="Plan ID.")
@click.option("--payee", "payee_id", default=None, help="Payee ID for adjustment transactions.")
@click.option("--category-group", "category_group_id", default=None, help="Category group ID whose categories are used for split adjustments.")
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
        cfg = config.load_config()
        plan_id = config.resolve_plan_id(plan_id, cfg)
        payee_id = config.resolve_payee_id(payee_id, cfg)
        category_group_id = config.resolve_category_group_id(category_group_id, cfg)
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


# ─── Auth & config subcommands ──────────────────────────────────────────────


@cli.group("auth")
def cmd_auth() -> None:
    """Manage authentication."""


@cmd_auth.command("login")
@click.option("--force", is_flag=True, help="Overwrite an existing stored token without prompting.")
def cmd_auth_login(force: bool) -> None:
    """Store a YNAB token in the OS keychain."""
    if not force and config.get_token_from_keyring():
        if not ui.is_interactive():
            click.echo(
                "A token is already stored. Re-run with --force to overwrite.",
                err=True,
            )
            sys.exit(1)
        if not ui.confirm("A token is already stored. Overwrite?", default=False):
            click.echo("Aborted.")
            return

    token = ui.prompt_password("YNAB personal access token")
    if not token:
        raise click.Abort()

    try:
        with ui.spinner("Verifying token…"):
            YnabClient(token).get_plans()
    except YnabError as e:
        ui.show_error(f"Token rejected by YNAB: {e}")
        sys.exit(1)
    except requests.RequestException as e:
        ui.show_error(f"Couldn't reach YNAB to verify the token: {e}")
        sys.exit(1)

    try:
        config.set_token_in_keyring(token)
    except config.KeyringUnavailable as e:
        ui.show_error(f"Couldn't write to keychain: {e}")
        sys.exit(1)
    ui.show_success("Token saved to OS keychain.")


@cmd_auth.command("logout")
def cmd_auth_logout() -> None:
    """Remove the stored token from the keychain."""
    if config.delete_token_from_keyring():
        ui.show_success("Token removed from keychain.")
    else:
        click.echo("No token was stored.")


@cmd_auth.command("status")
def cmd_auth_status() -> None:
    """Show whether a token is configured and which keyring backend is in use."""
    backend = config.keyring_backend_name()
    token = config.get_token_from_keyring()
    if token:
        click.echo(f"Token: stored in {backend}")
    else:
        click.echo(f"Token: not configured (keyring backend: {backend})")
        click.echo("Run `ynab-reconciler auth login` to set one up.")


@cli.group("config")
def cmd_config() -> None:
    """Manage non-secret defaults."""


@cmd_config.command("show")
def cmd_config_show() -> None:
    """Print the current config and where it's stored."""
    path = config.config_path()
    cfg = config.load_config()
    click.echo(f"Config file: {path}")
    click.echo(f"  exists: {'yes' if path.exists() else 'no'}")
    click.echo("")
    click.echo("Defaults:")
    for key in config.CONFIG_KEYS:
        value = getattr(cfg, key)
        display = value if value is not None else "(unset)"
        click.echo(f"  {key:<20} {display}")


@cmd_config.command("set")
@click.argument("key")
@click.argument("value")
def cmd_config_set(key: str, value: str) -> None:
    """Set a default. Valid keys: plan_id, payee_id, category_group_id."""
    try:
        config.set_config_value(key, value)
    except ValueError as e:
        ui.show_error(str(e))
        sys.exit(1)
    if value:
        ui.show_success(f"{key} = {value}")
    else:
        ui.show_success(f"{key} unset")


@cmd_config.command("unset")
@click.argument("key")
def cmd_config_unset(key: str) -> None:
    """Unset a default."""
    try:
        config.set_config_value(key, None)
    except ValueError as e:
        ui.show_error(str(e))
        sys.exit(1)
    ui.show_success(f"{key} unset")


@cmd_config.command("path")
def cmd_config_path() -> None:
    """Print the path to the config file."""
    click.echo(str(config.config_path()))


# ─── `init` — combined first-run wizard ─────────────────────────────────────


@cli.command("init")
def cmd_init() -> None:
    """First-run wizard: store a token and pick defaults."""
    if not ui.is_interactive():
        raise click.UsageError(
            "`init` is an interactive wizard. Use `auth login` and `config set` "
            "in non-interactive environments."
        )

    existing = config.get_token_from_keyring()
    if existing and not ui.confirm(
        "A token is already stored. Replace it?", default=False
    ):
        token = existing
    else:
        token = ui.prompt_password("YNAB personal access token")
        if not token:
            raise click.Abort()
        try:
            with ui.spinner("Verifying token…"):
                YnabClient(token).get_plans()
        except YnabError as e:
            ui.show_error(f"Token rejected by YNAB: {e}")
            sys.exit(1)
        except requests.RequestException as e:
            ui.show_error(f"Couldn't reach YNAB to verify the token: {e}")
            sys.exit(1)
        try:
            config.set_token_in_keyring(token)
            ui.show_success("Token saved to OS keychain.")
        except config.KeyringUnavailable as e:
            ui.show_error(f"Couldn't write to keychain: {e}")
            sys.exit(1)

    client = YnabClient(token)

    if not ui.confirm("Pick default plan / category group / payee now?", default=True):
        return

    try:
        plan = _pick_plan(client)
        if plan is None:
            return
        config.set_config_value("plan_id", plan.id)
        ui.show_saved_default("plan")

        group = _pick_category_group(client, plan.id)
        if group is not None:
            config.set_config_value("category_group_id", group.id)
            ui.show_saved_default("category group")

        payee = _pick_payee(client, plan.id)
        if payee is not None:
            config.set_config_value("payee_id", payee.id)
            ui.show_saved_default("payee")
    except YnabError as e:
        ui.show_error(str(e))
        sys.exit(1)

    ui.show_success("All set. Run `ynab-reconciler reconcile` to begin.")


def _pick_plan(client: YnabClient) -> Optional[Plan]:
    with ui.spinner("Fetching plans…"):
        plans = client.get_plans()
    if not plans:
        ui.show_warning("No plans found on this YNAB account.")
        return None
    if len(plans) == 1:
        ui.show_info(f"Using the only available plan: {plans[0].name}")
        return plans[0]
    return ui.select_plan(plans)


def _pick_category_group(client: YnabClient, plan_id: str) -> Optional[CategoryGroup]:
    with ui.spinner("Fetching category groups…"):
        groups = client.get_category_groups(plan_id)
    visible = [
        g for g in groups
        if not g.deleted and not g.hidden
        and any(not c.deleted and not c.hidden for c in g.categories)
    ]
    if not visible:
        ui.show_warning("No category groups with visible categories in this plan.")
        return None
    if len(visible) == 1:
        ui.show_info(f"Using the only available category group: {visible[0].name}")
        return visible[0]
    return ui.select_category_group(visible)


def _pick_payee(client: YnabClient, plan_id: str) -> Optional[Payee]:
    with ui.spinner("Fetching payees…"):
        payees = client.get_payees(plan_id)
    visible = sorted(
        (p for p in payees if not p.deleted),
        key=lambda p: p.name.lower(),
    )
    if not visible:
        ui.show_warning("No payees found in this plan.")
        return None
    return ui.select_payee(visible)
