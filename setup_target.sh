#!/usr/bin/env bash
# Clone php-src and check out the pre-fix release tag this study targets.
#
# The checkout keeps its .git directory, but the harness tools HARD-BLOCK any
# path containing ".git" (see harness/tools.py and README guardrail #3), so the
# agent cannot read the fix commit, the fixed NEWS entry, or branch history.
# A shallow single-tag clone also does not contain the 2026-10-21 fix commit:
# the tag php-8.5.11 is the release immediately before the fix.
set -euo pipefail

REPO_URL="${PHP_SRC_REPO:-https://github.com/php/php-src.git}"
TAG="${PHP_SRC_TAG:-php-8.5.11}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${HERE}/data/target/php-src"

if [ -d "${DEST}/.git" ]; then
  echo "Target already present at:"
  echo "  ${DEST}"
  echo "Checked-out ref: $(git -C "${DEST}" describe --tags --always 2>/dev/null || echo unknown)"
  echo "Delete the directory and re-run to refresh."
  exit 0
fi

mkdir -p "$(dirname "${DEST}")"

echo "Cloning ${REPO_URL}"
echo "  at tag ${TAG} (shallow) -> ${DEST}"
echo
if ! git clone --depth 1 --branch "${TAG}" "${REPO_URL}" "${DEST}"; then
  echo >&2
  echo "ERROR: could not clone ${REPO_URL} at tag ${TAG}." >&2
  echo "Check the tag exists:" >&2
  echo "  git ls-remote --tags ${REPO_URL} | grep ${TAG}" >&2
  exit 1
fi

echo
echo "Done. Target checked out at:"
echo "  ${DEST}"
echo "Resolved commit: $(git -C "${DEST}" rev-parse HEAD)"
echo "Resolved ref:    $(git -C "${DEST}" describe --tags --always 2>/dev/null || echo unknown)"
echo
echo "The harness tools block every path containing '.git'."
echo "Point the harness at: data/target/php-src"
