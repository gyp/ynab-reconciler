#!/usr/bin/env bash
#
# Release ynab-reconciler to PyPI and bump the gyp/tap Homebrew formula.
#
# Usage:
#   scripts/release.sh X.Y.Z              # full release
#   scripts/release.sh X.Y.Z --dry-run    # rehearse without uploading or pushing
#
# See packaging/RELEASING.md for the manual fallback if any step fails.

set -euo pipefail

# ── plumbing ────────────────────────────────────────────────────────────────

PROJECT="ynab-reconciler"
TAP_NAME="gyp/tap"
TAP_REPO="github.com/gyp/homebrew-tap"

C_BLUE=$'\033[1;34m'
C_YELLOW=$'\033[1;33m'
C_RED=$'\033[1;31m'
C_GREEN=$'\033[1;32m'
C_OFF=$'\033[0m'

step() { printf '\n%s==>%s %s\n' "$C_BLUE" "$C_OFF" "$*"; }
warn() { printf '%sWARN:%s %s\n' "$C_YELLOW" "$C_OFF" "$*" >&2; }
err()  { printf '%sERROR:%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; }
ok()   { printf '%s✓%s %s\n' "$C_GREEN" "$C_OFF" "$*"; }
dry()  { printf '%sDRY:%s would %s\n' "$C_YELLOW" "$C_OFF" "$*"; }

usage() {
  sed -n '3,9p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-2}"
}

VERSION=""
DRY_RUN=false
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    -h|--help) usage 0 ;;
    -*) err "unknown flag: $arg"; usage ;;
    *)
      if [[ -z "$VERSION" ]]; then VERSION="$arg"
      else err "unexpected arg: $arg"; usage
      fi
      ;;
  esac
done

[[ -n "$VERSION" ]] || { err "version arg required"; usage; }
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
  || { err "bad version '$VERSION'; expected X.Y.Z"; exit 2; }

TAG="v$VERSION"
$DRY_RUN && warn "DRY-RUN — no uploads, no pushes, no GH release"

# ── 1. preflight ────────────────────────────────────────────────────────────

step "1/14 Preflight checks"

REPO="$(git rev-parse --show-toplevel 2>/dev/null || true)"
[[ -n "$REPO" && -f "$REPO/pyproject.toml" ]] \
  || { err "must run from inside the $PROJECT repo"; exit 1; }
grep -q "^name = \"$PROJECT\"" "$REPO/pyproject.toml" \
  || { err "$REPO/pyproject.toml is not for $PROJECT"; exit 1; }
cd "$REPO"

for cmd in brew gh python3 curl awk sed; do
  command -v "$cmd" >/dev/null \
    || { err "$cmd not found in PATH"; exit 1; }
done

PY="$REPO/.venv/bin/python"
[[ -x "$PY" ]] || { err "no .venv/bin/python — run 'python3 -m venv .venv && .venv/bin/pip install -e \".[dev]\" build twine'"; exit 1; }
"$PY" -m build --help >/dev/null 2>&1 \
  || { err "'build' not installed in .venv — run '$PY -m pip install build'"; exit 1; }
"$PY" -m twine --help >/dev/null 2>&1 \
  || { err "'twine' not installed in .venv — run '$PY -m pip install twine'"; exit 1; }

gh auth status >/dev/null 2>&1 \
  || { err "gh not authenticated — run 'gh auth login'"; exit 1; }

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[[ "$BRANCH" == "main" ]] \
  || { err "must be on main branch (currently on '$BRANCH')"; exit 1; }

# Allow uncommitted pyproject.toml / formula (this script may have edited them
# on a previous interrupted run) but nothing else.
DIRTY="$(git status --porcelain | grep -Ev '^\s*M (pyproject\.toml|packaging/Formula/ynab-reconciler\.rb)$' || true)"
if [[ -n "$DIRTY" ]]; then
  err "working tree has untracked or unrelated modifications:"
  echo "$DIRTY" >&2
  exit 1
fi

ok "preflight clean"

# ── 2. tap setup ────────────────────────────────────────────────────────────

step "2/14 Ensure $TAP_NAME is tapped"

if brew tap | grep -qx "$TAP_NAME"; then
  ok "$TAP_NAME already tapped"
else
  brew tap "$TAP_NAME"
fi

TAP_DIR="$(brew --repository)/Library/Taps/gyp/homebrew-tap"
[[ -d "$TAP_DIR/.git" ]] || { err "$TAP_DIR is not a git repo"; exit 1; }

# ── 3. bump version ─────────────────────────────────────────────────────────

step "3/14 Bump pyproject.toml version → $VERSION"

CURRENT="$(awk -F'"' '/^version = /{print $2; exit}' pyproject.toml)"
if [[ "$CURRENT" == "$VERSION" ]]; then
  ok "pyproject.toml already at $VERSION"
else
  tmp="$(mktemp)"
  awk -v v="$VERSION" '
    BEGIN { done=0 }
    !done && /^version = / { print "version = \"" v "\""; done=1; next }
    { print }
  ' pyproject.toml > "$tmp" && mv "$tmp" pyproject.toml
  ok "bumped $CURRENT → $VERSION"
fi

# ── 4. build ────────────────────────────────────────────────────────────────

step "4/14 Build wheel + sdist"

rm -rf dist/ build/ src/*.egg-info
"$PY" -m build >/dev/null
"$PY" -m twine check dist/* | tee /tmp/twine-check.log
grep -q FAILED /tmp/twine-check.log && { err "twine check FAILED"; exit 1; }
ok "build + twine check passed"

WHEEL="$(ls dist/${PROJECT//-/_}-${VERSION}-py3-none-any.whl 2>/dev/null || true)"
SDIST="$(ls dist/${PROJECT//-/_}-${VERSION}.tar.gz 2>/dev/null || true)"
[[ -n "$WHEEL" && -n "$SDIST" ]] \
  || { err "wheel or sdist for $VERSION missing from dist/"; exit 1; }

# ── 5. smoke test ───────────────────────────────────────────────────────────

step "5/14 Smoke-test the wheel in a clean venv"

TMPVENV="$(mktemp -d)/venv"
python3 -m venv "$TMPVENV"
"$TMPVENV/bin/pip" install -q "$WHEEL"
"$TMPVENV/bin/$PROJECT" --help >/dev/null
rm -rf "$(dirname "$TMPVENV")"
ok "smoke test passed"

# ── 6. PyPI upload ──────────────────────────────────────────────────────────

step "6/14 Upload to PyPI"

PYPI_JSON="https://pypi.org/pypi/$PROJECT/$VERSION/json"
already_on_pypi() { curl -fsSL -o /dev/null "$PYPI_JSON"; }

if already_on_pypi; then
  ok "PyPI already has $PROJECT $VERSION — skipping upload"
elif $DRY_RUN; then
  dry "twine upload dist/* (skipping in dry-run)"
  warn "later steps that depend on the published sdist will be skipped"
else
  read -r -p "$(printf '%sUpload %s %s to PyPI? [y/N] %s' "$C_YELLOW" "$PROJECT" "$VERSION" "$C_OFF")" yn
  case "$yn" in
    [yY]|[yY][eE][sS]) ;;
    *) err "aborted by user"; exit 1 ;;
  esac
  "$PY" -m twine upload dist/*
  printf "Waiting for PyPI to serve %s" "$VERSION"
  for _ in $(seq 1 30); do
    if already_on_pypi; then echo; ok "PyPI serving $VERSION"; break; fi
    printf '.'; sleep 2
  done
  already_on_pypi || { echo; err "PyPI did not serve $VERSION within 60s"; exit 1; }
fi

# If we're in dry-run and the version isn't published, we can't continue:
# steps 7–14 all need the real PyPI metadata or the published sdist.
if $DRY_RUN && ! already_on_pypi; then
  warn "Stopping after step 6: remaining steps need the published sdist."
  warn "Re-run without --dry-run, or on an already-published version, to rehearse them."
  exit 0
fi

# ── 7. fetch real sdist url + sha256 ────────────────────────────────────────

step "7/14 Fetch sdist url + sha256 from PyPI"

read -r NEW_URL NEW_SHA256 <<<"$(
  "$PY" - <<PY
import json, urllib.request
with urllib.request.urlopen("$PYPI_JSON") as r:
    data = json.load(r)
sdist = next(f for f in data["urls"] if f["packagetype"] == "sdist")
print(sdist["url"], sdist["digests"]["sha256"])
PY
)"
[[ -n "$NEW_URL" && -n "$NEW_SHA256" ]] \
  || { err "could not parse sdist metadata from PyPI"; exit 1; }
ok "url    = $NEW_URL"
ok "sha256 = $NEW_SHA256"

# ── 8. update canonical formula ─────────────────────────────────────────────

step "8/14 Update canonical formula in packaging/Formula/"

FORMULA="$REPO/packaging/Formula/ynab-reconciler.rb"
tmp="$(mktemp)"
awk -v url="$NEW_URL" -v sha="$NEW_SHA256" '
  /^  url "/   && !u { print "  url \"" url "\""; u=1; next }
  /^  sha256 "/ && !s { print "  sha256 \"" sha "\""; s=1; next }
  { print }
' "$FORMULA" > "$tmp" && mv "$tmp" "$FORMULA"
ok "formula updated"

# ── 9. dep-change check ─────────────────────────────────────────────────────

step "9/14 Check whether runtime deps changed since previous tag"

PREV="$(git describe --tags --abbrev=0 --exclude="$TAG" 2>/dev/null || true)"
if [[ -z "$PREV" ]]; then
  warn "no previous tag — skipping dep-change check"
else
  DEPDIFF="$(
    diff \
      <(git show "$PREV:pyproject.toml" | awk '/^dependencies = \[/,/^]/') \
      <(awk '/^dependencies = \[/,/^]/' pyproject.toml) || true
  )"
  if [[ -z "$DEPDIFF" ]]; then
    ok "runtime deps unchanged since $PREV — no resource refresh needed"
  else
    warn "runtime deps changed since $PREV:"
    echo "$DEPDIFF" >&2
    warn "you should refresh resource blocks via 'brew update-python-resources'"
    warn "(brew rejects PyPI uploads <24h old, so this likely needs a follow-up"
    warn " release tomorrow — see packaging/RELEASING.md step 5b)"
  fi
fi

# ── 10. stage formula into brew's tap clone ─────────────────────────────────

step "10/14 Stage formula into brew's tap clone"

git -C "$TAP_DIR" pull --ff-only
cp "$FORMULA" "$TAP_DIR/Formula/ynab-reconciler.rb"
ok "formula staged at $TAP_DIR/Formula/ynab-reconciler.rb"

# ── 11. brew install test ───────────────────────────────────────────────────

step "11/14 Test brew install (downloads from PyPI)"

brew uninstall "$PROJECT" >/dev/null 2>&1 || true
brew install --build-from-source "$TAP_NAME/$PROJECT"
brew test "$PROJECT"
"$PROJECT" --help >/dev/null
ok "brew install + test passed"

# ── 12. commit + push tap ───────────────────────────────────────────────────

step "12/14 Commit + push tap"

if git -C "$TAP_DIR" diff --quiet -- Formula/ynab-reconciler.rb; then
  ok "tap formula already committed at HEAD"
else
  git -C "$TAP_DIR" add Formula/ynab-reconciler.rb
  git -C "$TAP_DIR" commit -m "Bump $PROJECT to $VERSION"
fi
if $DRY_RUN; then
  dry "git -C $TAP_DIR push"
else
  git -C "$TAP_DIR" push
  ok "tap pushed to $TAP_REPO"
fi

# ── 13. commit + tag main repo ──────────────────────────────────────────────

step "13/14 Commit + tag $REPO"

if git diff --quiet pyproject.toml packaging/Formula/ynab-reconciler.rb && \
   git diff --quiet --cached pyproject.toml packaging/Formula/ynab-reconciler.rb; then
  ok "no changes to commit (already committed?)"
else
  git add pyproject.toml packaging/Formula/ynab-reconciler.rb
  git commit -m "Release $VERSION"
fi

if git tag --list "$TAG" | grep -qx "$TAG"; then
  ok "tag $TAG already exists"
else
  git tag "$TAG"
fi

if $DRY_RUN; then
  dry "git push && git push --tags"
else
  git push
  git push --tags
  ok "main repo pushed with tag $TAG"
fi

# ── 14. GitHub release ──────────────────────────────────────────────────────

step "14/14 Create GitHub release $TAG"

if gh release view "$TAG" >/dev/null 2>&1; then
  ok "release $TAG already exists"
elif $DRY_RUN; then
  dry "gh release create $TAG --generate-notes"
else
  gh release create "$TAG" --generate-notes
  ok "GitHub release $TAG created"
fi

printf '\n%s✓ Done.%s %s %s released.\n' "$C_GREEN" "$C_OFF" "$PROJECT" "$VERSION"
