# ynab-reconciler

A small CLI for reconciling YNAB accounts — built around a workflow for
tracking long-term savings and investments inside your budget.

## Why this exists

[YNAB](https://www.ynab.com/) is great at planning month-to-month money, but
it has an awkward gap around long-term savings and investments:

- **Tracking accounts** sit *outside* the budget. Their value changes don't
  flow into your category balances at all, so the money you've earmarked for
  e.g. retirement never reflects what's actually in the account.
- **On-budget accounts** do flow into the budget, but every gain or loss lands
  in **Inflow: Ready to Assign**. For long-term investments that's noise —
  you'd have to manually re-allocate every fluctuation, and a bad market month
  shows up as "income" you suddenly need to deal with.

Neither fits the case where you want the *value* of your long-term savings to
reflect reality, *and* you want the categories you've earmarked for those
savings (retirement, college fund, new car, …) to grow and shrink with the
pot, *without* polluting daily inflow.

The workflow this tool supports:

1. Keep your long-term savings/investment account on-budget.
2. Group the saving goals it funds into a single **category group**
   (e.g. "Long-term savings").
3. Reconcile the account periodically against its real-world value.
4. Route the reconciliation adjustment **into the saving goals themselves**,
   distributed across them in proportion to each goal's current balance.

Net effect: the budget reflects real portfolio value, each goal grows or
shrinks with its share of the pot, and *Ready to Assign* stays clean.

## What the tool does

- Lists your YNAB plans, accounts, payees, and category groups so you can
  find the IDs you need.
- Walks you through reconciling each open account interactively.
- Computes the adjustment as `statement_balance − YNAB cleared balance`.
- Posts a single cleared, approved adjustment transaction back to YNAB.
- Optionally turns that transaction into a **split transaction** across the
  categories of a chosen category group, weighted by each category's current
  balance — this is the piece that drives the savings-goal workflow above.
- Accepts statement balances in another currency (e.g. `1000 EUR` against a
  USD budget) and converts via the free
  [Frankfurter](https://frankfurter.dev/) exchange-rate API.

Under the hood it talks to the official
[YNAB REST API v1](https://api.ynab.com/) (`https://api.ynab.com/v1`) using a
small `requests`-based client. All amounts are exchanged in YNAB's "milliunits"
(thousandths of a currency unit); the client handles that conversion for you.

## Install

Requires Python 3.11+.

```bash
git clone <this-repo> ynab-reconciler
cd ynab-reconciler
pip install -e .
```

## Configure

Copy the example env file and fill it in:

```bash
cp .env.example .env
```

| Variable                 | Required | Purpose                                                            |
| ------------------------ | -------- | ------------------------------------------------------------------ |
| `YNAB_TOKEN`             | yes      | YNAB personal access token.                                        |
| `YNAB_PLAN_ID`           | no       | Default budget (plan) ID, so you can omit `--plan`.                |
| `YNAB_PAYEE_ID`          | no       | Default payee for adjustment transactions.                         |
| `YNAB_CATEGORY_GROUP_ID` | no       | Default category group to split adjustments across.                |

If `YNAB_PLAN_ID` (or `--plan`) is not set, the tool lists your plans and
prompts you to pick one interactively. The same happens for the category
group — if `YNAB_CATEGORY_GROUP_ID` (or `--category-group`) is missing, or
points to a group that doesn't exist in the selected plan, you'll be asked to
choose one. When there's only one candidate, it's selected automatically.

To generate a personal access token, go to your YNAB account settings and
follow the instructions at <https://api.ynab.com/> — that page also documents
the underlying API this tool uses.

Once `YNAB_TOKEN` is set, you can discover the other IDs with the tool itself:

```bash
ynab-reconciler plans
ynab-reconciler payees           --plan <plan-id>
ynab-reconciler category-groups  --plan <plan-id>
ynab-reconciler accounts         --plan <plan-id>
```

## Usage

The CLI is `ynab-reconciler`. All commands take `--plan` (or fall back to
`YNAB_PLAN_ID`).

| Command                                  | What it does                                                  |
| ---------------------------------------- | ------------------------------------------------------------- |
| `plans`                                  | List your budgets.                                            |
| `payees --plan <id>`                     | List payees in a plan.                                        |
| `category-groups --plan <id>`            | List category groups and their categories.                    |
| `accounts --plan <id> [--include-closed]`| List accounts with cleared balance and last-reconciled date.  |
| `reconcile --plan <id> [...]`            | Interactive reconciliation (see below).                       |

### Reconciling

The recommended invocation for the savings-goal workflow:

```bash
ynab-reconciler reconcile \
  --plan           $YNAB_PLAN_ID \
  --payee          $YNAB_PAYEE_ID \
  --category-group $YNAB_CATEGORY_GROUP_ID
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

## Worked example

Suppose you have a "Long-term savings" category group with three goals, and
their current category balances are:

| Goal                  | Balance |
| --------------------- | ------- |
| Retirement savings    | €2,000  |
| New car               | €1,000  |
| College fund          | €1,000  |

Your investment account shows a YNAB cleared balance of **€4,000**, but the
broker says it's actually worth **€4,400**. You reconcile with
`--category-group` set to "Long-term savings". The tool posts a single split
adjustment of **+€400** distributed in proportion to the goal balances
(weights 2:1:1):

| Goal                  | Adjustment |
| --------------------- | ---------- |
| Retirement savings    | +€200      |
| New car               | +€100      |
| College fund          | +€100      |

Result: the account is back in sync with reality (€4,400), each goal grew in
proportion to its share of the pot, and *Ready to Assign* is untouched.

### How the split works

- Weights = the absolute values of each category's current balance.
- If at least one category is non-zero, each subtransaction is
  `round(total × weight / sum_of_weights)`; the last subtransaction absorbs
  any rounding remainder so the split sums exactly to the adjustment.
- If every category in the group is at zero, the split is divided evenly.

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
