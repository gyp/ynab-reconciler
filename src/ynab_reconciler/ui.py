"""Terminal presentation primitives for the interactive reconcile flow.

Centralises Rich + questionary use behind a small vocabulary so main.py
stays readable, and so the TTY-fallback path lives in exactly one place.
When stdin/stdout aren't a TTY, every interactive function falls back to
the original plain click.prompt / click.echo behaviour so existing scripts
and CI keep working byte-identically.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Iterator, Optional, Union

import click
import questionary
from questionary import Style as QStyle
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text
from rich.theme import Theme

from .api.models import Account, CategoryGroup, Payee, Plan
from .currency import parse_statement_input
from .reconciler import AccountReconciliationResult

THEME = Theme(
    {
        "accent": "bold cyan",
        "dim": "grey50",
        "success": "green",
        "warning": "yellow",
        "danger": "bold red",
        "money_pos": "green",
        "money_neg": "red",
        "muted_money": "grey62",
        "header": "bold white",
        "badge_ok": "bold green",
        "badge_warn": "bold yellow",
        "badge_bad": "bold red",
    }
)

console = Console(theme=THEME, soft_wrap=True, highlight=False)

QUESTIONARY_STYLE = QStyle(
    [
        ("qmark", "fg:#00afff bold"),
        ("question", "bold"),
        ("answer", "fg:#5fafff bold"),
        ("pointer", "fg:#00afff bold"),
        ("highlighted", "fg:#00afff bold"),
        ("selected", "fg:#5fd75f"),
        ("instruction", "fg:#808080 italic"),
    ]
)


def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


# ─── Money / status formatting ──────────────────────────────────────────────


def fmt_amount(amount: float) -> str:
    return f"{amount:,.0f}"


def money(units: float, *, signed: bool = False) -> Text:
    sign = "+" if signed and units > 0 else ""
    style = "money_pos" if units >= 0 else "money_neg"
    return Text(f"{sign}{fmt_amount(units)}", style=style)


def status_badge(r: AccountReconciliationResult) -> Text:
    if r.matched:
        return Text("✓ matched", style="badge_ok")
    if r.adjustment_created:
        sign = "+" if r.adjustment_in_units > 0 else ""
        return Text(
            f"± adjusted {sign}{fmt_amount(r.adjustment_in_units)}",
            style="badge_warn",
        )
    sign = "+" if r.adjustment_in_units > 0 else ""
    return Text(
        f"! discrepancy {sign}{fmt_amount(r.adjustment_in_units)}",
        style="badge_bad",
    )


# ─── High-level chrome ──────────────────────────────────────────────────────


def banner(plan_name: str, iso_code: Optional[str]) -> None:
    if not is_interactive():
        return
    subtitle = Text()
    subtitle.append("plan: ", style="dim")
    subtitle.append(plan_name, style="accent")
    if iso_code:
        subtitle.append("  ·  ", style="dim")
        subtitle.append(iso_code, style="accent")
    console.print()
    console.print(Rule(Text("YNAB Reconciler", style="header"), style="accent"))
    console.print(subtitle, justify="center")
    console.print()


def section(title: str) -> None:
    if not is_interactive():
        click.echo(f"\n{'─' * 60}")
        click.echo(f"{title}:")
        return
    console.print()
    console.print(Rule(Text(title, style="header"), style="dim", align="left"))


# ─── Pickers ────────────────────────────────────────────────────────────────


def _fallback_pick(items, label_fn, kind: str):
    """Original numbered-prompt picker, used when not on a TTY."""
    click.echo(f"\n{'─' * 60}")
    click.echo(f"{kind}:")
    name_width = max(len(label_fn(x)) for x in items)
    for i, item in enumerate(items, 1):
        click.echo(f"  {i:>2}. {label_fn(item):<{name_width}}  {item.id}")
    click.echo("")
    while True:
        raw = click.prompt(f"Select {kind.lower().rstrip('s')}", default="1").strip()
        try:
            idx = int(raw)
        except ValueError:
            click.echo(f"  Invalid selection: {raw}", err=True)
            continue
        if not 1 <= idx <= len(items):
            click.echo(f"  Out of range: {idx}", err=True)
            continue
        return items[idx - 1]


def select_plan(plans: list[Plan]) -> Plan:
    if not is_interactive():
        return _fallback_pick(plans, lambda p: p.name, "Plans")

    choices = [
        questionary.Choice(
            title=[("class:answer", p.name), ("class:instruction", f"  {p.id}")],
            value=p,
        )
        for p in plans
    ]
    answer = questionary.select(
        "Select a plan",
        choices=choices,
        style=QUESTIONARY_STYLE,
        qmark="❯",
        use_shortcuts=False,
        instruction="(↑/↓ to move, Enter to pick)",
    ).ask()
    if answer is None:
        raise click.Abort()
    return answer


def select_payee(payees: list[Payee]) -> Optional[Payee]:
    """Pick a payee, or return None if the user picks 'Skip'."""
    if not is_interactive():
        return _fallback_pick(payees, lambda p: p.name, "Payees")

    choices: list[Union[questionary.Choice, questionary.Separator]] = [
        questionary.Choice(
            title=_SearchableTitle(
                [("class:answer", p.name), ("class:instruction", f"  {p.id}")]
            ),
            value=p,
        )
        for p in payees
    ]
    choices.append(questionary.Separator("  ─────"))
    choices.append(
        questionary.Choice(
            title=_SearchableTitle(
                [("class:instruction", "  Skip — don't set a default payee")]
            ),
            value=None,
        )
    )
    answer = questionary.select(
        "Select a payee",
        choices=choices,
        style=QUESTIONARY_STYLE,
        qmark="❯",
        instruction="(↑/↓ move · Enter pick · type to search)",
        use_search_filter=True,
        use_jk_keys=False,
    ).ask()
    return answer  # may be None (user picked Skip) or the Payee


def select_category_group(groups: list[CategoryGroup]) -> CategoryGroup:
    if not is_interactive():
        return _fallback_pick(groups, lambda g: g.name, "Category groups")

    choices = [
        questionary.Choice(
            title=[
                ("class:answer", g.name),
                ("class:instruction", f"  ({len(g.categories)} categories)"),
            ],
            value=g,
        )
        for g in groups
    ]
    answer = questionary.select(
        "Select a category group",
        choices=choices,
        style=QUESTIONARY_STYLE,
        qmark="❯",
        instruction="(↑/↓ to move, Enter to pick)",
    ).ask()
    if answer is None:
        raise click.Abort()
    return answer


class _SearchableTitle(list):
    """A FormattedText list with a `.lower()` returning the joined plain text.

    questionary's search filter does `c.title.lower()` (common.py:388), which
    blows up on plain lists. Subclassing list keeps `isinstance(title, list)`
    checks in questionary's renderer happy while adding the one method the
    filter needs.
    """

    def lower(self) -> str:
        return "".join(token[1] for token in self).lower()


def _account_choice_title(
    acct: Account, result: Optional[AccountReconciliationResult], name_width: int
) -> _SearchableTitle:
    if result is None:
        glyph = ("class:instruction", "○ ")
        tail: list[tuple[str, str]] = []
    elif result.matched:
        glyph = ("fg:#5fd75f bold", "● ")
        tail = [("fg:#5fd75f", "  ✓ matched")]
    elif result.adjustment_created:
        glyph = ("fg:#ffd75f bold", "● ")
        sign = "+" if result.adjustment_in_units > 0 else ""
        tail = [
            (
                "fg:#ffd75f",
                f"  ± adjusted {sign}{fmt_amount(result.adjustment_in_units)}",
            )
        ]
    else:
        glyph = ("fg:#ff5f5f bold", "⚠ ")
        sign = "+" if result.adjustment_in_units > 0 else ""
        tail = [
            (
                "fg:#ff5f5f",
                f"  ! discrepancy {sign}{fmt_amount(result.adjustment_in_units)}",
            )
        ]

    cleared = acct.cleared_balance_in_units()
    cleared_style = "fg:#5fd75f" if cleared >= 0 else "fg:#ff5f5f"
    return _SearchableTitle(
        [
            glyph,
            ("class:answer", f"{acct.name:<{name_width}}"),
            ("class:instruction", "   cleared "),
            (cleared_style, f"{fmt_amount(cleared):>12}"),
            *tail,
        ]
    )


def select_account(
    candidates: list[Account],
    statuses: dict[str, AccountReconciliationResult],
) -> str:
    """Return account_id, or sentinel 'all' / 'quit'."""
    if not is_interactive():
        return _fallback_select_account(candidates, statuses)

    name_width = max(len(a.name) for a in candidates)
    choices: list[Union[questionary.Choice, questionary.Separator]] = [
        questionary.Choice(
            title=_account_choice_title(a, statuses.get(a.id), name_width),
            value=a.id,
        )
        for a in candidates
    ]
    choices.append(questionary.Separator("  ─────"))
    choices.append(
        questionary.Choice(
            title=_SearchableTitle(
                [
                    ("class:pointer", "» "),
                    ("class:answer", "Reconcile all remaining"),
                ]
            ),
            value="all",
        )
    )
    choices.append(
        questionary.Choice(
            title=_SearchableTitle(
                [
                    ("class:instruction", "  Refresh from YNAB"),
                ]
            ),
            value="refresh",
        )
    )
    choices.append(
        questionary.Choice(
            title=_SearchableTitle([("class:instruction", "  Quit")]),
            value="quit",
        )
    )

    answer = questionary.select(
        "Accounts",
        choices=choices,
        style=QUESTIONARY_STYLE,
        qmark="❯",
        instruction="(↑/↓ move · Enter pick · type to search)",
        default="all",
        use_search_filter=True,
        use_jk_keys=False,
    ).ask()
    if answer is None:
        return "quit"
    return answer


def _fallback_select_account(
    candidates: list[Account],
    statuses: dict[str, AccountReconciliationResult],
) -> str:
    name_width = max(len(a.name) for a in candidates)
    click.echo(f"\n{'─' * 60}")
    click.echo("Accounts:")
    for i, acct in enumerate(candidates, 1):
        res = statuses.get(acct.id)
        cleared = fmt_amount(acct.cleared_balance_in_units())
        mark = "[x]" if res is not None else "[ ]"
        line = f"  {i:>2}. {mark} {acct.name:<{name_width}}  cleared: {cleared:>12}"
        if res is not None:
            line += f"  → {_plain_status_label(res)}"
        click.echo(line)
    click.echo("")
    click.echo("  a) Reconcile all remaining  [default — press Enter]")
    click.echo("  r) Refresh from YNAB")
    click.echo("  q) Quit")

    while True:
        raw = click.prompt("Select", default="a", show_default=False).strip().lower()
        if raw in ("q", "quit"):
            return "quit"
        if raw in ("r", "refresh"):
            return "refresh"
        if raw in ("", "a", "all"):
            return "all"
        try:
            idx = int(raw)
        except ValueError:
            click.echo(f"  Invalid selection: {raw}", err=True)
            continue
        if not 1 <= idx <= len(candidates):
            click.echo(f"  Out of range: {idx}", err=True)
            continue
        return candidates[idx - 1].id


def _plain_status_label(r: AccountReconciliationResult) -> str:
    if r.matched:
        return "matched"
    sign = "+" if r.adjustment_in_units > 0 else ""
    if r.adjustment_created:
        return f"adjusted {sign}{fmt_amount(r.adjustment_in_units)}"
    return f"discrepancy {sign}{fmt_amount(r.adjustment_in_units)} (not adjusted)"


# ─── Per-account view ───────────────────────────────────────────────────────


def show_account_header(account: Account, native_iso: Optional[str]) -> None:
    cleared = account.cleared_balance_in_units()
    uncleared = account.uncleared_balance_in_units()

    if not is_interactive():
        click.echo(f"\n{'─' * 60}")
        click.echo(account.name)
        click.echo(f"  YNAB cleared balance: {fmt_amount(cleared)}")
        if account.uncleared_balance != 0:
            click.echo(f"  Uncleared (pending):  {fmt_amount(uncleared)}")
        return

    iso = native_iso or ""
    body = Text()
    body.append("Cleared  ", style="dim")
    body.append_text(money(cleared))
    if iso:
        body.append(f" {iso}", style="dim")
    if account.uncleared_balance != 0:
        body.append("\n")
        body.append("Pending  ", style="dim")
        body.append_text(Text(fmt_amount(uncleared), style="muted_money"))
        if iso:
            body.append(f" {iso}", style="dim")

    console.print()
    console.print(
        Panel(
            body,
            title=Text(account.name, style="accent"),
            title_align="left",
            border_style="dim",
            padding=(0, 2),
        )
    )


# ─── Statement balance input ────────────────────────────────────────────────


StatementAnswer = Union[str, tuple[float, Optional[str]]]


def ask_statement_balance(account: Account) -> StatementAnswer:
    """Returns 'skip' | 'quit' | (amount_in_units, iso_or_None).

    Empty input means "accept the cleared balance as-is".
    """
    prompt_label = "Statement balance"
    instruction = "(Enter = accept · s = skip · q = back to menu)"

    if not is_interactive():
        raw = click.prompt(
            f"  {prompt_label} [Enter to accept, s=skip, q=back to menu]",
            default="",
            show_default=False,
        ).strip()
        return _interpret_balance_input(raw, account)

    while True:
        raw = questionary.text(
            prompt_label,
            instruction=instruction,
            style=QUESTIONARY_STYLE,
            qmark="❯",
        ).ask()
        if raw is None:
            return "quit"
        raw = raw.strip()
        result = _interpret_balance_input(raw, account)
        if result == "__invalid__":
            console.print(
                Text(f"  Couldn't parse '{raw}'. Try e.g. 1,234.56 or 1000 EUR.",
                     style="danger")
            )
            continue
        return result


def _interpret_balance_input(raw: str, account: Account) -> StatementAnswer:
    if raw.lower() == "q":
        return "quit"
    if raw.lower() == "s":
        return "skip"
    if raw == "":
        return (account.cleared_balance_in_units(), None)
    try:
        amount, iso = parse_statement_input(raw)
    except ValueError:
        if is_interactive():
            return "__invalid__"  # type: ignore[return-value]
        # non-TTY: preserve original behaviour — emit error and skip
        click.echo(f"  Invalid amount '{raw}', skipping.", err=True)
        return "skip"
    return (amount, iso)


# ─── Inline messages ────────────────────────────────────────────────────────


def show_conversion(
    amount: float, from_iso: str, converted: float, to_iso: str, rate: float
) -> None:
    if not is_interactive():
        click.echo(
            f"  Converting {amount:,.2f} {from_iso} → {fmt_amount(converted)} {to_iso}"
            f" (rate: {rate:.4f})"
        )
        return
    line = Text()
    line.append("  ⇄ ", style="accent")
    line.append(f"{amount:,.2f} {from_iso}")
    line.append("  →  ", style="dim")
    line.append(f"{fmt_amount(converted)} {to_iso}")
    line.append(f"   rate {rate:.4f}", style="dim")
    console.print(line)


def confirm_large_adjustment(adjustment: float, current: float) -> bool:
    pct = abs(adjustment) / abs(current) * 100
    sign = "+" if adjustment > 0 else ""

    if not is_interactive():
        click.echo(
            f"  ⚠ Large adjustment: {sign}{fmt_amount(adjustment)} "
            f"({pct:.0f}% of current balance {fmt_amount(current)})"
        )
        return click.confirm("  Proceed with this adjustment?", default=False)

    body = Text()
    body.append("Adjustment  ", style="dim")
    body.append(f"{sign}{fmt_amount(adjustment)}", style="badge_warn")
    body.append(f"   ({pct:.0f}% of current balance {fmt_amount(current)})",
                style="dim")
    console.print()
    console.print(
        Panel(
            body,
            title=Text("⚠  Large adjustment", style="danger"),
            title_align="left",
            border_style="danger",
            padding=(0, 2),
        )
    )
    answer = questionary.confirm(
        "Proceed with this adjustment?",
        default=False,
        style=QUESTIONARY_STYLE,
        qmark="❯",
    ).ask()
    return bool(answer)


def show_error(message: str) -> None:
    if not is_interactive():
        click.echo(f"  Error: {message}", err=True)
        return
    console.print(Text(f"  ✗ {message}", style="danger"))


def show_account_result(result: AccountReconciliationResult, no_adjust: bool) -> None:
    if not is_interactive():
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
                f"  Discrepancy: {sign}{fmt_amount(result.adjustment_in_units)}"
                " (--no-adjust, skipped)"
            )
        return

    line = Text("  ")
    if result.matched:
        line.append("✓ ", style="badge_ok")
        line.append("Balances match", style="success")
    elif result.adjustment_created:
        sign = "+" if result.adjustment_in_units > 0 else ""
        line.append("± ", style="badge_warn")
        line.append("Adjustment created: ", style="warning")
        line.append(f"{sign}{fmt_amount(result.adjustment_in_units)}",
                    style="badge_warn")
    else:
        sign = "+" if result.adjustment_in_units > 0 else ""
        line.append("! ", style="badge_bad")
        line.append("Discrepancy: ", style="danger")
        line.append(f"{sign}{fmt_amount(result.adjustment_in_units)}",
                    style="badge_bad")
        line.append("  (--no-adjust)" if no_adjust else "", style="dim")
    console.print(line)


def show_skipped() -> None:
    if not is_interactive():
        click.echo("  Skipped.")
        return
    console.print(Text("  · skipped", style="dim"))


def show_run_summary(results: list[AccountReconciliationResult]) -> None:
    if not results:
        return
    adjusted = [r for r in results if r.adjustment_created]
    matched = [r for r in results if r.matched]
    skipped = len(results) - len(matched) - len(adjusted)

    if not is_interactive():
        click.echo(f"\n{'─' * 60}")
        click.echo(
            f"Done. {len(matched)} matched, {len(adjusted)} adjusted, "
            f"{skipped} discrepancies skipped."
        )
        return

    body = Text()
    body.append(f"{len(matched)}", style="badge_ok")
    body.append(" matched", style="success")
    body.append("   ·   ", style="dim")
    body.append(f"{len(adjusted)}", style="badge_warn")
    body.append(" adjusted", style="warning")
    body.append("   ·   ", style="dim")
    body.append(f"{skipped}", style="badge_bad" if skipped else "dim")
    body.append(" skipped", style="danger" if skipped else "dim")

    console.print()
    console.print(
        Panel(
            body,
            title=Text("Done", style="accent"),
            title_align="left",
            border_style="accent",
            padding=(0, 2),
        )
    )


# ─── Spinner ────────────────────────────────────────────────────────────────


@contextmanager
def spinner(message: str) -> Iterator[None]:
    if not is_interactive():
        yield
        return
    with console.status(Text(message, style="dim"), spinner="dots"):
        yield


# ─── Generic prompts / messages (used by auth & config flows) ──────────────


def prompt_password(label: str) -> str:
    """Hidden-input prompt for a secret. Falls back to click on non-TTY."""
    if not is_interactive():
        return click.prompt(label, hide_input=True, default="", show_default=False)
    answer = questionary.password(
        label,
        style=QUESTIONARY_STYLE,
        qmark="❯",
    ).ask()
    if answer is None:
        raise click.Abort()
    return answer


def confirm(question: str, *, default: bool = True) -> bool:
    if not is_interactive():
        return click.confirm(question, default=default)
    answer = questionary.confirm(
        question,
        default=default,
        style=QUESTIONARY_STYLE,
        qmark="❯",
    ).ask()
    if answer is None:
        raise click.Abort()
    return bool(answer)


def confirm_save_default(label: str) -> bool:
    return confirm(f"Save as default {label}?", default=True)


def show_saved_default(label: str) -> None:
    if not is_interactive():
        click.echo(f"  Saved as default {label}.")
        return
    console.print(Text(f"  ✓ Saved as default {label}", style="success"))


def show_first_run_banner() -> None:
    if not is_interactive():
        click.echo("No YNAB token configured — let's set one up.")
        click.echo(
            "Generate a personal access token at "
            "https://app.ynab.com/settings/developer"
        )
        return
    body = Text()
    body.append(
        "No YNAB token configured — let's set one up.\n", style="header"
    )
    body.append(
        "Generate a personal access token at\n", style="dim"
    )
    body.append("https://app.ynab.com/settings/developer", style="accent")
    console.print()
    console.print(
        Panel(
            body,
            title=Text("Welcome", style="accent"),
            title_align="left",
            border_style="accent",
            padding=(0, 2),
        )
    )


def show_info(msg: str) -> None:
    if not is_interactive():
        click.echo(msg)
        return
    console.print(Text(f"  {msg}", style="dim"))


def show_warning(msg: str) -> None:
    if not is_interactive():
        click.echo(f"Warning: {msg}", err=True)
        return
    console.print(Text(f"  ⚠ {msg}", style="warning"))


def show_success(msg: str) -> None:
    if not is_interactive():
        click.echo(msg)
        return
    console.print(Text(f"  ✓ {msg}", style="success"))


# Re-export for main.py
__all__ = [
    "is_interactive",
    "fmt_amount",
    "banner",
    "section",
    "select_plan",
    "select_payee",
    "select_category_group",
    "select_account",
    "show_account_header",
    "ask_statement_balance",
    "show_conversion",
    "confirm_large_adjustment",
    "show_error",
    "show_account_result",
    "show_skipped",
    "show_run_summary",
    "spinner",
    "prompt_password",
    "confirm",
    "confirm_save_default",
    "show_saved_default",
    "show_first_run_banner",
    "show_info",
    "show_warning",
    "show_success",
]
