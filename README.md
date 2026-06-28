# ynab-reconciler

A small CLI for reconciling YNAB accounts — built around a workflow for
tracking long-term savings and investments inside your budget.

## Why this exists

[YNAB](https://www.ynab.com/) is great at planning month-to-month money, but
it has an awkward gap around long-term savings and investments:

- **Tracking accounts** sit *outside* the budget. Their value changes don't
  flow into your category balances at all, so the money you've earmarked for
  e.g. retirement never reflects what's actually in the account.
- **On-budget accounts** *do* flow into the budget, so you can track gains and
  losses via reconciliation — but by default the adjustment lands in
  **Inflow: Ready to Assign**, where it's noise. A market swing isn't income
  you want to spend, and a bad month isn't a loss you need to recoup from this
  month's categories.

Neither fits the case where you want the *value* of your long-term savings to
reflect reality, *and* you want the categories you've earmarked for those
savings (retirement, college fund, new car, …) to grow and shrink with the
pot, *without* polluting daily inflow.

The workflow this tool supports:

1. Keep your long-term savings/investment accounts on-budget.
2. Group the saving goals they fund into a single **category group**
   (e.g. "Long-term savings").
3. Reconcile these accounts periodically against their real-world value.
4. Route the reconciliation adjustment **into the saving goals themselves**,
   distributed across them in proportion to each goal's current balance.

Net effect: the budget reflects real portfolio value, each goal grows or
shrinks with its share of the pot, and *Ready to Assign* stays clean.

## What the tool does

- Picks the plan, account, payee, and category group interactively (with
  type-to-search), so you don't need to look up YNAB IDs by hand.
- Walks you through reconciling the relevant accounts interactively.
- Computes the adjustment as `statement_balance − YNAB cleared balance`.
- Posts the adjustment back to YNAB as a single cleared, approved **split
  transaction** across the categories of a chosen category group, weighted by
  each category's current balance — this is the piece that drives the
  savings-goal workflow above.
- Accepts statement balances in another currency (e.g. `1000 EUR` against a
  USD budget) and converts via the free
  [Frankfurter](https://frankfurter.dev/) exchange-rate API.

Under the hood it talks to the official
[YNAB REST API v1](https://api.ynab.com/) (`https://api.ynab.com/v1`) using a
small `requests`-based client.

### Example

Suppose you have a "Long-term savings" category group with three goals, and
their current category balances are:

| Goal                  | Balance |
| --------------------- | ------- |
| Retirement savings    | €2,000  |
| New car               | €1,000  |
| College fund          | €1,000  |

Your investment account shows a YNAB cleared balance of **€4,000**, but the
broker says it's actually worth **€4,400**. You reconcile with
the saving category group set to "Long-term savings". The tool posts a single split
adjustment of **+€400** distributed in proportion to the goal balances
(weights 2:1:1):

| Goal                  | Adjustment |
| --------------------- | ---------- |
| Retirement savings    | +€200      |
| New car               | +€100      |
| College fund          | +€100      |

Result: the account is back in sync with reality (€4,400), each goal grew in
proportion to its share of the pot.

#### How the split works

- Weights = the absolute values of each category's current balance.
- If at least one category is non-zero, each subtransaction is
  `round(total × weight / sum_of_weights)`; the last subtransaction absorbs
  any rounding remainder so the split sums exactly to the adjustment.
- If every category in the group is at zero, the split is divided evenly.

## Install

### macOS (Homebrew)

```bash
brew install gyp/tap/ynab-reconciler
```

### Windows, Linux, or anywhere with Python

```bash
pipx install ynab-reconciler
```

[`pipx`](https://pipx.pypa.io/) installs the tool into its own isolated
environment and puts the `ynab-reconciler` command on your `PATH` — no need to
think about virtualenvs.

If you don't have `pipx` yet:

- **macOS without Homebrew:** `python3 -m pip install --user pipx && python3 -m pipx ensurepath`
- **Windows:** install Python from [python.org](https://www.python.org/downloads/),
  then in a new terminal run `python -m pip install --user pipx` followed by
  `python -m pipx ensurepath`. Close and reopen the terminal once.
- **Linux:** most distros package it as `pipx` or `python3-pipx`; otherwise the
  `python3 -m pip install --user pipx` command above works too.

[`uv`](https://docs.astral.sh/uv/) users can install it the same way:
`uv tool install ynab-reconciler`.

Requires Python 3.11+ under the hood; `pipx` and Homebrew will handle that for
you.

## Configure

The first time you run the tool, set things up interactively:

```bash
ynab-reconciler init
```

This walks you through entering a YNAB personal access token (stored in your
OS keychain — macOS Keychain, Linux Secret Service, or Windows Credential
Manager) and optionally picking a default plan, category group, and payee.

To generate a personal access token, go to your YNAB account settings and
follow the instructions at <https://api.ynab.com/> — that page also documents
the underlying API this tool uses.

After setup, just run `ynab-reconciler` (shorthand for `ynab-reconciler
reconcile`) — defaults are read automatically.

### Auth & config commands

| Command                                  | What it does                                                  |
| ---------------------------------------- | ------------------------------------------------------------- |
| `auth login`                             | Store a token in the OS keychain (verified against the API).  |
| `auth logout`                            | Remove the stored token.                                      |
| `auth status`                            | Show whether a token is stored and which backend is in use.   |
| `config show`                            | Print the saved defaults and config file path.                |
| `config set <key> <value>`               | Set a default. Keys: `plan_id`, `payee_id`, `category_group_id`. |
| `config unset <key>`                     | Remove a default.                                              |
| `config path`                            | Print the config file path.                                    |

When you pick a plan or category group interactively during `reconcile`, the
tool offers to save it as the new default. Say yes once and the next run
skips the prompt.

### Where things live

- Defaults: `~/.config/ynab-reconciler/config.toml`
  (override with `$XDG_CONFIG_HOME`).
- Token: OS keychain, service `ynab-reconciler`, account `default`.
  - macOS: `security find-generic-password -s ynab-reconciler`
  - Linux: `secret-tool lookup service ynab-reconciler username default`
- Connection tokens: same keychain service, account `connection:<name>`.

### Precedence

For any value: **CLI flag → config file → keyring (token only) → interactive
prompt**. Pass `--plan <id>` to override the saved default for one run
without changing it.

You can discover IDs from the CLI itself once a token is configured:

```bash
ynab-reconciler plans
ynab-reconciler payees           --plan <plan-id>
ynab-reconciler category-groups  --plan <plan-id>
ynab-reconciler accounts         --plan <plan-id>
```

## Connections — fetch balances automatically

Instead of typing a statement balance for an account you keep on-budget, you can
**pair it with a connection** that fetches the balance for you. The first
supported provider is **Interactive Brokers** via the read-only **Flex Web
Service**.

Why Flex (and not the TWS/Gateway API): a Flex token can *only* retrieve a
pre-defined report — it can't place orders, move funds, or change anything. It's
plain HTTPS, so the tool stays a one-shot CLI (cron/CI-friendly) with no daemon
and no IBKR username/password. The data is **end-of-day**, not real-time — fine
for budgeting.

**Set up the report in IBKR first:** in Client Portal go to *Settings →
Reporting → Flex Web Service*, enable it (you get a **token**), and build an
**Activity Flex Query** that includes the Cash Report / Net Asset Value section.
Note the query's **query ID**.

Then, in the tool:

```bash
ynab-reconciler connections add        # name it, paste token (→ keychain) + query id
ynab-reconciler connections fetch ibkr # see the balances it reports (--json to pipe)
ynab-reconciler connections pair       # link a YNAB account → a broker account + figure
```

| Command                      | What it does                                                          |
| ---------------------------- | -------------------------------------------------------------------- |
| `connections add`            | Add a connection; token goes to the OS keychain, never the config.   |
| `connections list`           | Show configured connections and their paired YNAB accounts.          |
| `connections remove <name>`  | Delete a connection, its stored token, and any pairings using it.    |
| `connections fetch <name>`   | Fetch and print current balances (`--json` for piping).              |
| `connections pair`           | Interactive loop: pick an account (paired ones are marked ✓), choose a connection + broker account, or remove its pairing; repeats until you quit. |
| `connections unpair`         | Remove a pairing directly (handy for scripts).                        |

The default figure is **net liquidation** (total account value); choose **cash**
at pair time instead if you only keep the cash portion on-budget. Once paired,
`reconcile` fetches the connection once per run and pre-fills that account's
statement prompt — press **Enter** to accept, or type over it as usual. If the
fetch fails, you just get a warning and can enter the balance by hand.

## Usage

Once defaults are saved (via `init` or `config set`), just run:

```bash
ynab-reconciler
```

(which is shorthand for `ynab-reconciler reconcile`).

To override a saved default for one run, pass a flag:

```bash
ynab-reconciler reconcile --plan <other-plan-id>
```

For each open account, the tool prints the YNAB cleared balance and prompts
for the actual statement balance. At the prompt:

- Press **Enter** to accept the YNAB balance (no adjustment).
- Type a number to set the new balance, e.g. `1200` or `1200.50`.
- Append a currency code to convert, e.g. `1000 EUR` (uses live Frankfurter
  rates against the plan's native currency).
- Type `s` to skip an account, `q` to quit.
- For accounts paired with a [connection](#connections--fetch-balances-automatically),
  the prompt is pre-filled with the fetched balance — Enter accepts it.

Useful flags:

- `--payee <id>` — payee to attach to adjustment transactions.
- `--category-group <id>` — split adjustments across this group's categories,
  weighted by current balance (this is the savings-goal feature).
- `--no-adjust` — show discrepancies but don't post any transactions
  (dry-run-style report).

The tool has a few additional utility functions, check `ynab-reconciler --help`
for the full list.



## Development

```bash
git clone https://github.com/gyp/ynab-reconciler
cd ynab-reconciler
pip install -e ".[dev]"
pytest
```

The source lives under `src/ynab_reconciler/`:

- `main.py` — Click CLI.
- `reconciler.py` — adjustment math and proportional split.
- `currency.py` — statement-input parsing and Frankfurter conversion.
- `api/client.py` — thin YNAB v1 HTTP client.
- `api/models.py` — dataclasses for YNAB entities.

## License

Released into the public domain under the [Unlicense](LICENSE).
