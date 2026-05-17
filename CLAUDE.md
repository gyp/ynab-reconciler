# CLAUDE.md

Project notes for AI assistants working in this repo.

## What this is

`ynab-reconciler` is a small Python CLI for reconciling [YNAB](https://www.ynab.com/)
accounts against real-world statement balances, with one distinguishing feature:
the adjustment is posted as a **single split transaction across the categories of
a chosen category group, weighted by each category's current balance**.

The point of the weighted split: when you keep long-term savings / investment
accounts on-budget, market gains and losses would normally land in *Inflow:
Ready to Assign* (noise). Distributing the adjustment across the saving goals
the account funds keeps the budget honest *and* keeps Ready to Assign clean.
See [README.md](README.md) for the full motivation and an example.

## Tech stack

- Python ≥ 3.11 (uses `tomllib`).
- [`click`](https://click.palletsprojects.com/) — CLI framework.
- [`rich`](https://rich.readthedocs.io/) — colored output, panels, spinners.
- [`questionary`](https://questionary.readthedocs.io/) — interactive selects with type-to-search.
- [`keyring`](https://pypi.org/project/keyring/) — token in OS keychain (macOS Keychain / Secret Service / Windows Credential Manager).
- [`platformdirs`](https://pypi.org/project/platformdirs/), `tomli-w` — config file plumbing.
- [`requests`](https://requests.readthedocs.io/) — HTTP client for YNAB API v1 and the Frankfurter exchange-rate API.
- Tests: `pytest`, `pytest-mock`.

No async, no DB, no framework — it's a flat script-shaped CLI with a thin
HTTP client.

## Layout

```
src/ynab_reconciler/
  main.py        — Click CLI (commands, prompts, control flow)
  reconciler.py  — adjustment math + proportional split (_build_subtransactions)
  currency.py    — statement-input parsing + Frankfurter conversion
  config.py      — TOML config + OS keychain (single boundary for keyring/tomllib/platformdirs/tomli_w)
  ui.py          — Rich + questionary primitives, with TTY-fallback path
  api/client.py  — YnabClient (requests-based, BASE_URL = https://api.ynab.com/v1)
  api/models.py  — dataclasses for Plan / Account / Payee / Category / CategoryGroup / Transaction
tests/           — one test file per source module
packaging/
  Formula/ynab-reconciler.rb  — canonical Homebrew formula (tap repo is downstream)
  RELEASING.md                — step-by-step release process
  forum-post.md               — draft announcement
api-1.json       — YNAB OpenAPI spec, vendored for reference
```

## Key concepts

- **Milliunits.** YNAB stores money as integer thousandths of a currency unit.
  Convert with `reconciler.milliunits(units)` / `Account.cleared_balance_in_units()`.
  Adjustments are computed in float units and converted once when posting.
- **Adjustment math.** `statement_balance − YNAB cleared balance`. Anything
  with `abs(...) < 0.001` is considered matched and produces no transaction.
- **Weighted split** ([reconciler.py:30](src/ynab_reconciler/reconciler.py#L30)):
  weights are the *absolute values* of each category's current balance. The
  last subtransaction absorbs the rounding remainder so subtransactions sum
  exactly to the total. If every category is at zero, the split is even.
- **Currency input** ([currency.py](src/ynab_reconciler/currency.py)):
  `parse_statement_input` handles `"1,000,000.00"`, `"1 234,56"`,
  `"1000 EUR"`, `"500 Ft"` (Ft → HUF), etc. Conversion goes through Frankfurter
  (`https://api.frankfurter.dev/v2/rate/<from>/<to>`).
- **Resolution precedence** (any value): CLI flag → config file → keyring
  (token only) → interactive prompt. `config.resolve_*` functions implement this.
- **`config.py` is the single boundary** for `keyring`, `tomllib`, `tomli_w`,
  and `platformdirs`. Nothing else in the codebase should import them — see the
  module docstring.

## CLI surface

Top-level commands are grouped in `--help` via `GroupedGroup` in
[main.py:132](src/ynab_reconciler/main.py#L132):

- **Main:** `init`, `reconcile`
- **Settings:** `auth {login,logout,status}`, `config {show,set,unset,path}`
- **Utilities:** `plans`, `accounts`, `payees`, `category-groups`

Running with no subcommand invokes `reconcile` (see
[main.py:180](src/ynab_reconciler/main.py#L180)).

Config keys: `plan_id`, `payee_id`, `category_group_id`. Stored under
`~/.config/ynab-reconciler/config.toml` (honours `$XDG_CONFIG_HOME` on every
platform; falls back to platformdirs on Windows when XDG is unset).

## Tests & dev loop

```bash
pip install -e ".[dev]"
pytest
```

Test files mirror source files 1:1. Tests use `MagicMock` for the YNAB client
— there's no recording/replay fixture and no live-API integration test. Keep it
that way unless asked.

## Releasing

Full procedure: [packaging/RELEASING.md](packaging/RELEASING.md).
Short version: bump `version` in [pyproject.toml](pyproject.toml) → `python -m build` →
`twine upload` → refresh `url` + `sha256` (and optionally `resource` blocks via
`brew update-python-resources`) in [packaging/Formula/ynab-reconciler.rb](packaging/Formula/ynab-reconciler.rb) →
commit, tag `vX.Y.Z`, push, `gh release create`. The Homebrew tap at
`gyp/homebrew-tap` is the deploy target, not a source of truth — the formula
in this repo is canonical.

## Conventions worth keeping

- Errors surface via `ui.show_error(...)` then `sys.exit(1)` in command
  handlers — don't let `YnabError` or `click.UsageError` propagate raw.
- Every interactive helper in `ui.py` has a non-TTY fallback so scripts and
  CI keep working byte-identically. Preserve that when adding prompts.
- The `--help` text is part of the UX; commands have docstrings that double
  as help. When changing behavior, update the docstring.
- The `reconcile` flow currently asks for confirmation on adjustments larger
  than 30% of the current cleared balance ([main.py:331](src/ynab_reconciler/main.py#L331)).
- When a questionary picker uses `use_search_filter=True`, its `Choice.title`
  values **must** be wrapped in `_SearchableTitle` (in [ui.py](src/ynab_reconciler/ui.py)) —
  questionary's filter does `c.title.lower()`, which blows up on a plain list
  of `(style, text)` tuples mid-keystroke. See `select_account` /
  `select_payee` for the pattern.

## What NOT to do

- Don't add a database, ORM, or async runtime — this is a one-shot CLI.
- Don't import `keyring`, `tomllib`, `tomli_w`, or `platformdirs` outside
  `config.py`.
- Don't reintroduce env-var config (`YNAB_TOKEN` etc.), `.env` / `python-dotenv`
  loading, or a `--token` CLI flag. Tokens live in the keychain, defaults live
  in the TOML file — this was a deliberate cleanup, not an oversight. A
  `--token` flag in particular leaks through shell history and `ps`.
- Don't change the YNAB milliunits convention internally — convert at the
  boundary (`milliunits()` when posting, `*_in_units()` when reading).
- Don't add features beyond what the user asked for. The TODO list is for
  ideas, not a backlog to grind through.
- Don't propose alternative distribution channels (PyInstaller binaries, Docker
  images, MSI installers, Snap, code signing, etc.) unprompted. Distribution is
  intentionally PyPI + a Homebrew tap (`gyp/tap`) only — chosen for low
  maintenance over user friction. See [packaging/RELEASING.md](packaging/RELEASING.md).
