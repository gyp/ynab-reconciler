# Releasing a new version

## TL;DR

```bash
scripts/release.sh X.Y.Z              # full release
scripts/release.sh X.Y.Z --dry-run    # rehearse without uploading or pushing
```

The script runs every step in this document. It's idempotent — if it fails
partway through, fix the underlying issue and re-run; already-done steps
short-circuit. Read the rest of this file when the script gets stuck or when
you need to understand what it's doing.

Canonical files:

- Version: [pyproject.toml](../pyproject.toml) (`version = "..."`)
- Formula: [Formula/ynab-reconciler.rb](Formula/ynab-reconciler.rb) (canonical;
  the tap repo's copy is downstream)

Tap repo (deploy target, not a source of truth): <https://github.com/gyp/homebrew-tap>.

## Conventions used in this doc

The commands below reference two shell variables for local clone paths so
nothing here is tied to a particular machine. Export them once per shell
session (or add them to your shell rc file):

```bash
export YNAB_RECONCILER_LOCAL_CLONE=/path/to/your/ynab-reconciler
export HOMEBREW_TAP_LOCAL_CLONE=/path/to/your/homebrew-tap
```

`X.Y.Z` in commands is a placeholder for the version you're releasing
(e.g. `0.2.0`).

## Manual flow

The rest of this document describes the manual flow that `scripts/release.sh`
automates. Useful when the script fails and you need to resume by hand, or
when you want to understand what's happening under the hood.

## 1. Bump the version

Edit `version = "X.Y.Z"` in [pyproject.toml](../pyproject.toml). Use semver:
patch for bugfixes, minor for backwards-compatible features, major for breaking
CLI/config changes.

## 2. Build the package

From the repo root:

```bash
rm -rf dist/ build/ src/*.egg-info
.venv/bin/python -m build
.venv/bin/python -m twine check dist/*
```

Both files must report `PASSED`.

## 3. Smoke-test the wheel in a clean venv

```bash
TMPVENV=$(mktemp -d)/venv
python3 -m venv "$TMPVENV"
"$TMPVENV/bin/pip" install ./dist/ynab_reconciler-X.Y.Z-py3-none-any.whl
"$TMPVENV/bin/ynab-reconciler" --help
```

If `--help` prints, proceed.

## 4. Publish to PyPI

```bash
.venv/bin/python -m twine upload dist/*
```

PyPI does **not** allow re-uploading the same version. If anything's wrong,
bump to the next patch version and re-release rather than trying to overwrite.

Verify at <https://pypi.org/project/ynab-reconciler/X.Y.Z/> — check that the
README renders and the Project URLs are right.

## 5. Update the Homebrew formula

### 5a. Fetch the new sdist URL + sha256

```bash
.venv/bin/python <<'PY'
import json, urllib.request
v = "X.Y.Z"
with urllib.request.urlopen(f"https://pypi.org/pypi/ynab-reconciler/{v}/json") as r:
    data = json.load(r)
sdist = next(f for f in data["urls"] if f["packagetype"] == "sdist")
print("url:   ", sdist["url"])
print("sha256:", sdist["digests"]["sha256"])
PY
```

Update the `url` and `sha256` lines near the top of
[Formula/ynab-reconciler.rb](Formula/ynab-reconciler.rb).

### 5b. (Maybe) refresh `resource` blocks

Only needed if runtime deps in `pyproject.toml` changed since the previous
release. Quick check from the repo root, before tagging the new version:

```bash
prev=$(git describe --tags --abbrev=0)
diff \
  <(git show $prev:pyproject.toml | awk '/^dependencies = \[/,/^]/') \
  <(awk '/^dependencies = \[/,/^]/' pyproject.toml)
```

Empty diff → **skip to step 6**. Non-empty → continue below.

`brew update-python-resources` only operates on formulae inside a tapped
location. One-time tap setup (skip if already done):

```bash
brew tap gyp/tap   # clones github.com/gyp/homebrew-tap into brew's tap dir
```

This creates a *separate* clone at `$(brew --repository)/Library/Taps/gyp/homebrew-tap`
— independent of your `$HOMEBREW_TAP_LOCAL_CLONE`. Both have GitHub as origin
but don't share files; you'll bridge them with `cp`.

Copy the current formula into brew's clone and refresh from there:

```bash
cp packaging/Formula/ynab-reconciler.rb \
   "$(brew --repository)/Library/Taps/gyp/homebrew-tap/Formula/"
cd "$(brew --repository)/Library/Taps/gyp/homebrew-tap"
brew update-python-resources Formula/ynab-reconciler.rb
```

**24-hour PyPI freshness gate.** Brew passes
`--uploaded-prior-to=<now − 24h>` to pip
(hardcoded in `Library/Homebrew/utils/pypi.rb`, line ~498), so anything you
uploaded to PyPI today is invisible to this command. The failure looks like:

> ERROR: Could not find a version that satisfies the requirement ynab-reconciler==X.Y.Z

If you hit this, either wait a day and re-run, or edit the `resource` blocks
manually against `pip index versions <pkg>` output. There's no override flag
or env var.

When it succeeds, copy the refreshed formula back as canonical:

```bash
cp Formula/ynab-reconciler.rb $YNAB_RECONCILER_LOCAL_CLONE/packaging/Formula/
cd -
```

## 6. Test the formula locally

One-time tap setup (skip if already done in step 5b):

```bash
brew tap gyp/tap   # clones github.com/gyp/homebrew-tap into brew's tap dir
```

Brew installs from its own clone of the tap at
`$(brew --repository)/Library/Taps/gyp/homebrew-tap`, **not** your
`$HOMEBREW_TAP_LOCAL_CLONE`. To test the change before pushing, copy the
formula into brew's clone:

```bash
cp $YNAB_RECONCILER_LOCAL_CLONE/packaging/Formula/ynab-reconciler.rb \
   "$(brew --repository)/Library/Taps/gyp/homebrew-tap/Formula/"

brew uninstall ynab-reconciler 2>/dev/null
brew install --build-from-source gyp/tap/ynab-reconciler
brew test ynab-reconciler
ynab-reconciler --help
```

`brew install --build-from-source` reads the formula from disk, so an
uncommitted change in brew's clone is enough. The 24h freshness gate from
step 5b does **not** apply here — installs use the pinned `resource` URLs
in the formula, no PyPI resolution.

If anything fails, fix the canonical formula in
`$YNAB_RECONCILER_LOCAL_CLONE/packaging/Formula/`, re-copy to brew's clone,
re-run `brew install`.

## 7. Push the tap

Once the local install works, propagate to `$HOMEBREW_TAP_LOCAL_CLONE` and
push:

```bash
cp $YNAB_RECONCILER_LOCAL_CLONE/packaging/Formula/ynab-reconciler.rb \
   $HOMEBREW_TAP_LOCAL_CLONE/Formula/
cd $HOMEBREW_TAP_LOCAL_CLONE
git add Formula/ynab-reconciler.rb
git commit -m "Bump ynab-reconciler to X.Y.Z"
git push
```

After the push, end users running `brew install gyp/tap/ynab-reconciler` will
get the new version on their next `brew update`.

## 8. Commit + tag this repo

```bash
cd $YNAB_RECONCILER_LOCAL_CLONE
git add pyproject.toml packaging/Formula/ynab-reconciler.rb
git commit -m "Release X.Y.Z"
git tag vX.Y.Z
git push && git push --tags
```

## 9. Create a GitHub Release

```bash
cd $YNAB_RECONCILER_LOCAL_CLONE
gh release create vX.Y.Z --generate-notes
```

`--generate-notes` auto-populates the release page with PR titles and commits
since the previous tag. For a first release (or any time the auto-notes look
noisy), replace it with `--notes "short description"` or open the page on
GitHub afterwards and edit by hand.

No need to attach the wheel or sdist — they live on PyPI and duplicating them
here is just extra surface to keep in sync.

## Failure recovery

- **PyPI upload failed before the formula update:** bump version, restart from
  step 1.
- **PyPI upload succeeded but formula was wrong:** fix the formula, recommit in
  the tap, push. PyPI release stays as-is.
- **Formula works locally but `brew install gyp/tap/...` from GitHub fails:**
  make sure you pushed the tap (step 7) and that `brew update` ran on the
  installing machine.
