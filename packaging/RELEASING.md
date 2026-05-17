# Releasing a new version

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

### 5b. Refresh `resource` blocks for any deps that moved

This is only needed if you changed runtime deps in `pyproject.toml` or want to
pin to currently-resolvable versions on PyPI. Easiest way:

```bash
# Copy current formula into the local clone of the tap
# (Homebrew tools only work on tapped formulae):
cp packaging/Formula/ynab-reconciler.rb $HOMEBREW_TAP_LOCAL_CLONE/Formula/

# From inside the tap:
cd $HOMEBREW_TAP_LOCAL_CLONE
brew update-python-resources Formula/ynab-reconciler.rb

# Copy the refreshed formula back as the canonical:
cp Formula/ynab-reconciler.rb $YNAB_RECONCILER_LOCAL_CLONE/packaging/Formula/
cd -
```

If runtime deps didn't change, you can skip this and just bump the main `url` +
`sha256`.

## 6. Test the formula locally

Make sure the tap points at the local clone (one-time setup; re-tap if needed):

```bash
brew untap gyp/tap 2>/dev/null
brew tap gyp/tap $HOMEBREW_TAP_LOCAL_CLONE
```

Then:

```bash
# Commit the updated formula in the tap so the local clone has a HEAD with it:
cd $HOMEBREW_TAP_LOCAL_CLONE
git add Formula/ynab-reconciler.rb
git commit -m "Bump ynab-reconciler to X.Y.Z"

# Install from source via the tap:
brew uninstall ynab-reconciler 2>/dev/null
brew install --build-from-source gyp/tap/ynab-reconciler
brew test ynab-reconciler
ynab-reconciler --help
```

If anything fails, fix the formula, amend the commit, re-run `brew install`.

## 7. Push the tap

```bash
cd $HOMEBREW_TAP_LOCAL_CLONE
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
