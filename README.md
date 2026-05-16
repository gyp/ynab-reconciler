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

Requires Python 3.11+.

```bash
git clone <this-repo> ynab-reconciler
cd ynab-reconciler
pip install -e .
```

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
