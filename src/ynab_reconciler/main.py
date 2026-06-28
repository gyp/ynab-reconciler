"""CLI entry point for ynab-reconciler."""
from __future__ import annotations

import dataclasses
import json
import readline  # noqa: F401 — enables arrow-key editing in click.prompt
import sys
from typing import Optional

import click
import requests

from . import config, connections, ui
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


def _offer_save_default(
    key: str, value: str, label: str, plan_id: Optional[str] = None
) -> None:
    """After an interactive pick, offer to save the choice as the default.

    Silent on non-TTY. Skips if the value is already the saved default.
    For plan-scoped keys, `plan_id` selects the subtable to save under.
    """
    if not ui.is_interactive():
        return
    cfg = config.load_config()
    if key == "plan_id":
        current = cfg.plan_id
    else:
        current = getattr(cfg.defaults_for(plan_id), key)
    if current == value:
        return
    if ui.confirm_save_default(label):
        config.set_config_value(key, value, plan_id=plan_id)
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


def resolve_plan(client: YnabClient, plan_id: Optional[str]) -> Plan:
    """Return the full Plan dataclass (id, name, iso_code) for reconcile.

    Always fetches the `/plans` summary list (small payload, even for power
    users) rather than calling `/plans/{plan_id}`, whose full plan export
    downloads the entire budget. Use this when name + iso_code are needed;
    use resolve_plan_id when only the id is needed.
    """
    if plan_id is None and not sys.stdin.isatty():
        raise click.UsageError(
            "No plan specified and not running interactively — "
            "pass --plan or run `ynab-reconciler config set plan_id <id>`."
        )

    with ui.spinner("Fetching plans…"):
        plans = client.get_plans()
    if not plans:
        raise click.UsageError("No plans found on this YNAB account.")

    if plan_id:
        match = next((p for p in plans if p.id == plan_id), None)
        if match is None:
            raise click.UsageError(
                f"Plan {plan_id!r} not found on this YNAB account. "
                "Run `ynab-reconciler init` or check `config show`."
            )
        return match

    if len(plans) == 1:
        only = plans[0]
        click.echo(f"Using the only available plan: {only.name}")
        return only

    chosen = ui.select_plan(plans)
    _offer_save_default("plan_id", chosen.id, "plan")
    return chosen


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

    saved_default = config.load_config().defaults_for(plan_id).category_group_id
    chosen = ui.select_category_group(visible, default=saved_default)
    _offer_save_default("category_group_id", chosen.id, "category group", plan_id=plan_id)
    return chosen


def fmt_amount(amount: float) -> str:
    return f"{amount:,.0f}"


class GroupedGroup(click.Group):
    """A click.Group that renders --help with commands split into labelled sections."""

    SECTIONS: list[tuple[str, list[str]]] = [
        ("Main commands", ["init", "reconcile"]),
        ("Settings", ["auth", "config", "connections"]),
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


def _fetch_cached(
    name: str,
    conn: config.Connection,
    cache: dict[str, list[connections.FetchedBalance]],
) -> list[connections.FetchedBalance]:
    """Fetch a connection's balances, reusing the per-run cache.

    Flex data is end-of-day, so one fetch per connection per run is plenty —
    and it keeps the pacing limit comfortable when reconciling many accounts.
    """
    if name in cache:
        return cache[name]
    secret = config.get_connection_secret(name)
    if not secret:
        raise connections.ConnectionError(
            f"No stored credential for connection '{name}'. "
            "Run `ynab-reconciler connections add`."
        )
    with ui.spinner(f"Fetching balance from {name}…"):
        balances = connections.fetch_balances(conn, secret)
    cache[name] = balances
    return balances


def _connection_default(
    account: Account,
    balance_cache: dict[str, list[connections.FetchedBalance]],
) -> Optional[tuple[float, Optional[str], str]]:
    """Return (value, currency, source_label) to pre-fill the statement prompt
    if `account` is paired to a connection, else None.

    Any fetch problem is surfaced as a non-fatal warning and falls through to
    manual entry — a broker hiccup must never block reconciling by hand.
    """
    pairing = config.pairing_for(account.id)
    if pairing is None:
        return None
    conn = config.get_connection(pairing.connection)
    if conn is None:
        ui.show_warning(
            f"Account is paired to unknown connection '{pairing.connection}'."
        )
        return None
    try:
        balances = _fetch_cached(pairing.connection, conn, balance_cache)
    except (connections.ConnectionError, requests.RequestException) as e:
        ui.show_warning(f"Couldn't fetch from '{pairing.connection}': {e}")
        return None
    bal = connections.select_balance(balances, pairing)
    if bal is None:
        ui.show_warning(
            f"No '{pairing.field}' balance for account {pairing.ib_account_id} "
            f"in connection '{pairing.connection}'."
        )
        return None
    source = f"{pairing.connection} {pairing.field.replace('_', ' ')}"
    return (bal.value, bal.currency or None, source)


def _reconcile_one(
    client: YnabClient,
    plan_id: str,
    account: Account,
    native_iso: Optional[str],
    categories,
    payee_id: Optional[str],
    no_adjust: bool,
    balance_cache: dict[str, list[connections.FetchedBalance]],
) -> tuple[str, Optional[AccountReconciliationResult]]:
    """Reconcile a single account interactively.

    Returns ('done', result) | ('skip', None) | ('quit', None).
    'quit' means: return to menu (and stop all-mode early).
    """
    ui.show_account_header(account, native_iso)

    default = _connection_default(account, balance_cache)
    # When the pre-filled balance is in a different currency, also show it
    # converted to the budget currency in the prompt (the input still takes the
    # broker-currency amount). Cache the conversion so accepting it as-is doesn't
    # re-hit the FX API below.
    default_native: Optional[str] = None
    prefetched: Optional[tuple[float, str, float]] = None  # (amount, iso, converted)
    if default is not None:
        d_value, d_ccy, _ = default
        if d_ccy and native_iso and d_ccy != native_iso:
            try:
                with ui.spinner(f"Converting {d_ccy} → {native_iso}…"):
                    d_converted = convert_currency(d_value, d_ccy, native_iso)
                default_native = f"≈ {fmt_amount(d_converted)} {native_iso}"
                prefetched = (d_value, d_ccy, d_converted)
            except (ValueError, requests.RequestException):
                pass  # show the unconverted default; the post-entry path retries

    answer = ui.ask_statement_balance(account, default=default, default_native=default_native)
    if answer == "quit":
        return ("quit", None)
    if answer == "skip":
        ui.show_skipped()
        return ("skip", None)

    amount, input_iso = answer  # type: ignore[misc]
    memo = None
    if input_iso and native_iso and input_iso != native_iso:
        try:
            if prefetched is not None and prefetched[0] == amount and prefetched[1] == input_iso:
                converted = prefetched[2]
            else:
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
        client = get_client()
        plan = resolve_plan(client, plan_id)
        plan_id = plan.id
        payee_id = config.resolve_payee_id(payee_id, cfg, plan_id)
        category_group_id = config.resolve_category_group_id(category_group_id, cfg, plan_id)
        native_iso = plan.iso_code
        with ui.spinner("Loading accounts…"):
            accounts = client.get_accounts(plan_id)
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
    balance_cache: dict[str, list[connections.FetchedBalance]] = {}

    while True:
        choice = ui.select_account(candidates, statuses)

        if choice == "quit":
            break

        if choice == "refresh":
            try:
                with ui.spinner("Refreshing from YNAB…"):
                    # Accept the tiny risk that the plan's name or iso_code
                    # changed mid-session — re-fetching plan metadata every
                    # refresh would mean an extra round-trip for data that
                    # almost never changes.
                    accounts = client.get_accounts(plan_id)
            except YnabError as e:
                ui.show_error(str(e))
                continue
            candidates = [a for a in accounts if not a.deleted and not a.closed]
            by_id = {a.id: a for a in candidates}
            ui.show_success(f"Refreshed {len(candidates)} accounts from YNAB.")
            continue

        if choice == "all":
            for acct in candidates:
                if acct.id in statuses:
                    continue
                status, result = _reconcile_one(
                    client, plan_id, acct, native_iso, categories, payee_id,
                    no_adjust, balance_cache,
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
            client, plan_id, acct, native_iso, categories, payee_id,
            no_adjust, balance_cache,
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
    active = cfg.plan_id if cfg.plan_id is not None else "(unset)"
    click.echo(f"Active plan_id: {active}")

    plan_ids = sorted(cfg.plans)
    if cfg.plan_id and cfg.plan_id not in cfg.plans:
        plan_ids.append(cfg.plan_id)

    if not plan_ids:
        click.echo("  (no per-plan defaults saved)")
        return

    for pid in plan_ids:
        pd = cfg.defaults_for(pid)
        marker = " (active)" if pid == cfg.plan_id else ""
        click.echo("")
        click.echo(f"Plan {pid}{marker}:")
        for key in config.PLAN_SCOPED_KEYS:
            value = getattr(pd, key)
            display = value if value is not None else "(unset)"
            click.echo(f"  {key:<20} {display}")


def _resolve_set_plan_id(cli_plan: Optional[str], cfg: config.Config) -> str:
    plan_id = cli_plan or cfg.plan_id
    if not plan_id:
        raise click.UsageError(
            "No plan specified. Pass --plan <id> or set the active plan first "
            "with `config set plan_id <id>`."
        )
    return plan_id


@cmd_config.command("set")
@click.argument("key")
@click.argument("value")
@click.option("--plan", "plan_id", default=None, help="Plan ID for plan-scoped keys (payee_id, category_group_id). Defaults to the currently active plan.")
def cmd_config_set(key: str, value: str, plan_id: Optional[str]) -> None:
    """Set a default. Valid keys: plan_id, payee_id, category_group_id.

    `payee_id` and `category_group_id` are stored per plan.
    """
    try:
        if key in config.PLAN_SCOPED_KEYS:
            plan_id = _resolve_set_plan_id(plan_id, config.load_config())
            config.set_config_value(key, value, plan_id=plan_id)
            ui.show_success(f"[{plan_id}] {key} = {value}")
        else:
            config.set_config_value(key, value)
            if value:
                ui.show_success(f"{key} = {value}")
            else:
                ui.show_success(f"{key} unset")
    except (ValueError, click.UsageError) as e:
        ui.show_error(str(e))
        sys.exit(1)


@cmd_config.command("unset")
@click.argument("key")
@click.option("--plan", "plan_id", default=None, help="Plan ID for plan-scoped keys. Defaults to the currently active plan.")
def cmd_config_unset(key: str, plan_id: Optional[str]) -> None:
    """Unset a default."""
    try:
        if key in config.PLAN_SCOPED_KEYS:
            plan_id = _resolve_set_plan_id(plan_id, config.load_config())
            config.set_config_value(key, None, plan_id=plan_id)
            ui.show_success(f"[{plan_id}] {key} unset")
        else:
            config.set_config_value(key, None)
            ui.show_success(f"{key} unset")
    except (ValueError, click.UsageError) as e:
        ui.show_error(str(e))
        sys.exit(1)


@cmd_config.command("path")
def cmd_config_path() -> None:
    """Print the path to the config file."""
    click.echo(str(config.config_path()))


# ─── `connections` — external balance sources ───────────────────────────────


_REMOVE_PAIRING = object()  # sentinel: the "Remove pairing" entry in the broker picker


def _rate_to(from_ccy: str, to_iso: str, cache: dict[str, Optional[float]]) -> Optional[float]:
    """Cached FX rate from_ccy→to_iso (None if conversion is unavailable)."""
    if from_ccy not in cache:
        try:
            cache[from_ccy] = convert_currency(1.0, from_ccy, to_iso)
        except (ValueError, requests.RequestException):
            cache[from_ccy] = None
    return cache[from_ccy]


def _broker_account_choices(
    rep: dict[str, "connections.FetchedBalance"],
    ib_ids: list[str],
    native_iso: Optional[str],
    target_value: float,
) -> tuple[dict[str, str], Optional[str]]:
    """Build aligned picker labels for broker accounts and a suggested default.

    Each label shows the account (alias + id), its native value, and — when the
    currency differs from the budget — the value converted to the budget
    currency. The default is the account whose budget-currency value is closest
    to `target_value` (the YNAB account's cleared balance), but only when that
    closest match is within 10%; otherwise nothing is pre-selected.
    """
    rate_cache: dict[str, Optional[float]] = {}
    rows: dict[str, tuple[str, str, str, Optional[float]]] = {}
    for ib_id in ib_ids:
        b = rep[ib_id]
        orig = f"{b.value:,.0f} {b.currency}"
        conv = ""
        native_value: Optional[float] = None
        if native_iso and b.currency:
            if b.currency == native_iso:
                native_value = b.value
            else:
                rate = _rate_to(b.currency, native_iso, rate_cache)
                if rate is not None:
                    native_value = b.value * rate
                    conv = f"≈ {native_value:,.0f} {native_iso}"
        rows[ib_id] = (b.account_label(), orig, conv, native_value)

    acct_w = max(len(r[0]) for r in rows.values())
    orig_w = max(len(r[1]) for r in rows.values())
    labels: dict[str, str] = {}
    for ib_id, (acct_label, orig, conv, _) in rows.items():
        line = f"{acct_label:<{acct_w}}  ·  {orig:>{orig_w}}"
        if conv:
            line += f"  {conv}"
        labels[ib_id] = line

    default_ib: Optional[str] = None
    if abs(target_value) > 0.001:
        scored = [
            (ib_id, r[3]) for ib_id, r in rows.items() if r[3] is not None
        ]
        if scored:
            best_id, best_val = min(scored, key=lambda s: abs(s[1] - target_value))
            if abs(best_val - target_value) / abs(target_value) <= 0.10:
                default_ib = best_id
    return labels, default_ib


@cli.group("connections")
def cmd_connections() -> None:
    """Manage connections that fetch real-world balances.

    A connection is a read-only link to an external provider — currently
    Interactive Brokers via the Flex Web Service. Pair a connection to a YNAB
    account and `reconcile` pre-fills its statement balance from the fetched
    figure. Note: IBKR Flex data is end-of-day, not real-time.
    """


@cmd_connections.command("add")
@click.option("--name", default=None, help="Name for the connection.")
@click.option("--type", "conn_type", default=None, help="Connection type (e.g. ibkr-flex).")
@click.option("--query-id", default=None, help="The connection's report/query identifier (for ibkr-flex, the Flex query id).")
@click.option("--no-verify", is_flag=True, help="Skip the live test fetch before saving.")
def cmd_connections_add(
    name: Optional[str],
    conn_type: Optional[str],
    query_id: Optional[str],
    no_verify: bool,
) -> None:
    """Add a connection and store its access token in the OS keychain.

    The token is read with a hidden prompt and written only to the keychain —
    never to the config file, logs, or terminal output.
    """
    try:
        types = connections.connection_types()
        if conn_type is None:
            conn_type = types[0] if len(types) == 1 else ui.select_from(
                "Connection type", list(types), lambda t: t
            )
        if not connections.supports(conn_type):
            raise click.UsageError(
                f"Unsupported connection type: {conn_type!r}. "
                f"Known: {', '.join(types)}"
            )
        name = name or ui.prompt_text("Connection name", default="ibkr")
        if not name:
            raise click.Abort()
        if config.get_connection(name) is not None and ui.is_interactive():
            if not ui.confirm(f"Connection '{name}' exists. Overwrite?", default=False):
                click.echo("Aborted.")
                return
        id_label = connections.identifier_label(conn_type)
        query_id = query_id or ui.prompt_text(id_label)
        if not query_id:
            raise click.UsageError(f"A {id_label} is required.")
        token = ui.prompt_password(connections.secret_label(conn_type))
        if not token:
            raise click.Abort()

        conn = config.Connection(type=conn_type, query_id=query_id)
        if not no_verify:
            try:
                with ui.spinner("Verifying connection…"):
                    connections.fetch_balances(conn, token)
            except (connections.ConnectionError, requests.RequestException) as e:
                ui.show_error(f"Connection test failed: {e}")
                sys.exit(1)

        config.set_connection_secret(name, token)
        config.upsert_connection(name, conn)
    except (click.UsageError, config.KeyringUnavailable) as e:
        ui.show_error(str(e))
        sys.exit(1)
    ui.show_success(f"Connection '{name}' saved.")


@cmd_connections.command("list")
def cmd_connections_list() -> None:
    """List configured connections and their paired YNAB accounts."""
    cfg = config.load_config()
    if not cfg.connections:
        click.echo("No connections configured. Run `ynab-reconciler connections add`.")
        return
    for name, conn in sorted(cfg.connections.items()):
        cred = (
            "credential stored"
            if config.get_connection_secret(name)
            else "NO credential — run `connections add`"
        )
        click.echo(f"{name}  [{conn.type}]  query {conn.query_id}  ({cred})")
        paired = sorted(
            (aid, p) for aid, p in cfg.pairings.items() if p.connection == name
        )
        for account_id, p in paired:
            click.echo(f"    ↳ {account_id}  →  {p.ib_account_id} ({p.field})")


@cmd_connections.command("remove")
@click.argument("name")
def cmd_connections_remove(name: str) -> None:
    """Remove a connection, its stored token, and any pairings using it."""
    if config.get_connection(name) is None:
        ui.show_error(f"No connection named '{name}'.")
        sys.exit(1)
    if ui.is_interactive() and not ui.confirm(
        f"Remove connection '{name}' (and its pairings + stored token)?",
        default=False,
    ):
        click.echo("Aborted.")
        return
    config.remove_connection(name)
    ui.show_success(f"Connection '{name}' removed.")


@cmd_connections.command("fetch")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON for piping into other tools.")
def cmd_connections_fetch(name: str, as_json: bool) -> None:
    """Fetch and display current balances for a connection.

    IBKR Flex data is end-of-day, not real-time.
    """
    conn = config.get_connection(name)
    if conn is None:
        ui.show_error(f"No connection named '{name}'.")
        sys.exit(1)
    secret = config.get_connection_secret(name)
    if not secret:
        ui.show_error(
            f"No stored credential for '{name}'. Run `ynab-reconciler connections add`."
        )
        sys.exit(1)
    try:
        if as_json:
            balances = connections.fetch_balances(conn, secret)
        else:
            with ui.spinner(f"Fetching from {name}…"):
                balances = connections.fetch_balances(conn, secret)
    except connections.IbkrReportNotReady as e:
        ui.show_error(f"Report not ready yet (try again shortly): {e.detail}")
        sys.exit(1)
    except (connections.ConnectionError, requests.RequestException) as e:
        ui.show_error(str(e))
        sys.exit(1)

    if as_json:
        click.echo(json.dumps([dataclasses.asdict(b) for b in balances], indent=2))
        return
    if not balances:
        click.echo("No balances found in the report.")
        return
    for b in balances:
        tag = "" if b.summary else "   (per-currency)"
        click.echo(f"  {b.label()}{tag}")


@cmd_connections.command("pair")
@click.option("--plan", "plan_id", default=None, help="Plan ID.")
def cmd_connections_pair(plan_id: Optional[str]) -> None:
    """Pair (or unpair) YNAB accounts to connections' reported balances.

    Loops over a menu of the plan's accounts until you quit; already-paired
    accounts are marked. Picking one walks you through choosing a connection and
    a broker account — whose menu also offers a "Remove pairing" option.
    """
    cfg = config.load_config()
    if not cfg.connections:
        ui.show_error("No connections configured. Run `connections add` first.")
        sys.exit(1)
    try:
        plan_id = config.resolve_plan_id(plan_id, cfg)
        client = get_client()
        plan = resolve_plan(client, plan_id)
        plan_id = plan.id
        native_iso = plan.iso_code
        with ui.spinner("Loading accounts…"):
            accounts = client.get_accounts(plan_id)
    except (YnabError, click.UsageError) as e:
        ui.show_error(str(e))
        sys.exit(1)

    open_accts = [a for a in accounts if not a.deleted and not a.closed]
    if not open_accts:
        ui.show_error("No open accounts in this plan.")
        sys.exit(1)

    # Raw balances per connection are cached for the whole session (Flex data is
    # end-of-day; refetching per pairing would be wasteful and hit pacing limits).
    balance_cache: dict[str, list[connections.FetchedBalance]] = {}
    pairings: dict[str, config.Pairing] = {}

    def _broker_descr(p: config.Pairing) -> str:
        """Human-readable broker side of a pairing: the account alias if we've
        fetched it this session, otherwise the raw broker id."""
        for b in balance_cache.get(p.connection, []):
            if b.ib_account_id == p.ib_account_id:
                return b.account_label()
        return p.ib_account_id

    def _acct_label(a: Account) -> str:
        cleared = f"{fmt_amount(a.cleared_balance_in_units())} cleared"
        p = pairings.get(a.id)
        if p is None:
            return f"  {a.name}  ·  {cleared}"
        return f"✓ {a.name}  ·  {cleared}  → {p.connection} · {_broker_descr(p)}"

    # Pre-warm balances for connections referenced by existing pairings so the
    # menu can show human-readable broker identifiers (best-effort; on failure it
    # falls back to the raw broker id for that connection).
    for name in sorted({p.connection for p in config.load_config().pairings.values()}):
        conn0 = cfg.connections.get(name)
        secret0 = config.get_connection_secret(name)
        if conn0 is None or not secret0 or name in balance_cache:
            continue
        try:
            with ui.spinner(f"Fetching {name}…"):
                balance_cache[name] = connections.fetch_balances(conn0, secret0)
        except (connections.ConnectionError, requests.RequestException):
            pass

    while True:
        pairings.clear()
        pairings.update(config.load_config().pairings)

        acct = ui.select_from(
            "YNAB account to pair",
            open_accts,
            _acct_label,
            style_for=lambda a: "fg:#5fd75f bold" if a.id in pairings else "class:answer",
            cancel="Quit",
        )
        if acct is ui.CANCEL:
            return

        conn_names = sorted(cfg.connections)
        if len(conn_names) == 1:
            conn_name = conn_names[0]
        else:
            conn_name = ui.select_from(
                "Connection",
                conn_names,
                lambda n: f"{n} [{cfg.connections[n].type}]",
                cancel="Back",
            )
            if conn_name is ui.CANCEL:
                continue
        conn = cfg.connections[conn_name]
        secret = config.get_connection_secret(conn_name)
        if not secret:
            ui.show_error(f"No stored credential for '{conn_name}'.")
            continue
        try:
            balances = balance_cache.get(conn_name)
            if balances is None:
                with ui.spinner(f"Fetching accounts from {conn_name}…"):
                    balances = connections.fetch_balances(conn, secret)
                balance_cache[conn_name] = balances
        except (connections.ConnectionError, requests.RequestException) as e:
            ui.show_error(str(e))
            continue

        # Representative balance per broker account (prefer net liquidation) so
        # the picker can show alias + value instead of a bare 'U…' id.
        rep: dict[str, connections.FetchedBalance] = {}
        for b in balances:
            if b.ib_account_id not in rep or (
                b.field == connections.NET_LIQUIDATION and b.summary
            ):
                rep[b.ib_account_id] = b
        ib_ids = sorted(rep)
        if not ib_ids:
            ui.show_error("The connection reported no accounts.")
            continue

        # Offer "Remove pairing" only when this account is currently paired.
        extra = (
            [("✗ Remove pairing", _REMOVE_PAIRING)] if acct.id in pairings else None
        )
        if len(ib_ids) == 1 and not extra:
            ib_account_id = ib_ids[0]
        else:
            with ui.spinner("Converting balances…"):
                labels, default_ib = _broker_account_choices(
                    rep, ib_ids, native_iso, acct.cleared_balance_in_units()
                )
            ib_account_id = ui.select_from(
                "Broker account",
                ib_ids,
                lambda i: labels[i],
                default=default_ib,
                extra=extra,
                cancel="Back",
            )
            if ib_account_id is ui.CANCEL:
                continue
            if ib_account_id is _REMOVE_PAIRING:
                config.remove_pairing(acct.id)
                ui.show_success(f"Removed pairing for '{acct.name}'.")
                continue

        config.set_pairing(
            acct.id,
            config.Pairing(connection=conn_name, ib_account_id=ib_account_id),
        )
        ui.show_success(
            f"Paired '{acct.name}' → {conn_name}:{rep[ib_account_id].account_label()}."
        )


@cmd_connections.command("unpair")
def cmd_connections_unpair() -> None:
    """Remove a YNAB account ↔ connection pairing."""
    cfg = config.load_config()
    if not cfg.pairings:
        click.echo("No pairings configured.")
        return
    paired_ids = sorted(cfg.pairings)
    choice = ui.select_from(
        "Pairing to remove",
        paired_ids,
        lambda aid: f"{aid} → {cfg.pairings[aid].connection}:{cfg.pairings[aid].ib_account_id}",
    )
    config.remove_pairing(choice)
    ui.show_success("Pairing removed.")


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
        plan = _pick_plan(client, default_id=config.load_config().plan_id)
        if plan is None:
            return
        config.set_config_value("plan_id", plan.id)
        ui.show_saved_default("plan")

        plan_defaults = config.load_config().defaults_for(plan.id)

        group = _pick_category_group(
            client, plan.id, default_id=plan_defaults.category_group_id
        )
        if group is not None:
            config.set_config_value("category_group_id", group.id, plan_id=plan.id)
            ui.show_saved_default("category group")

        payee = _pick_payee(client, plan.id, default_id=plan_defaults.payee_id)
        if payee is not None:
            config.set_config_value("payee_id", payee.id, plan_id=plan.id)
            ui.show_saved_default("payee")
    except YnabError as e:
        ui.show_error(str(e))
        sys.exit(1)

    ui.show_success("All set. Run `ynab-reconciler reconcile` to begin.")


def _pick_plan(client: YnabClient, default_id: Optional[str] = None) -> Optional[Plan]:
    with ui.spinner("Fetching plans…"):
        plans = client.get_plans()
    if not plans:
        ui.show_warning("No plans found on this YNAB account.")
        return None
    if len(plans) == 1:
        ui.show_info(f"Using the only available plan: {plans[0].name}")
        return plans[0]
    return ui.select_plan(plans, default=default_id)


def _pick_category_group(
    client: YnabClient, plan_id: str, default_id: Optional[str] = None
) -> Optional[CategoryGroup]:
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
    return ui.select_category_group(visible, default=default_id)


def _pick_payee(
    client: YnabClient, plan_id: str, default_id: Optional[str] = None
) -> Optional[Payee]:
    with ui.spinner("Fetching payees…"):
        payees = client.get_payees(plan_id)
    visible = sorted(
        (p for p in payees if not p.deleted),
        key=lambda p: p.name.lower(),
    )
    if not visible:
        ui.show_warning("No payees found in this plan.")
        return None
    return ui.select_payee(visible, default=default_id)
