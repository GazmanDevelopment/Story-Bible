#!/bin/sh
# Run this ON the TrueNAS box, from the repo clone (e.g.
# /mnt/tank/apps/storybible-src - see deploy/README.md). Pulls the latest
# main, rebuilds the image locally, and tags it :current.
#
# Rollback: `docker tag story-bible:<old-tag> story-bible:current`, then
# restart the app from the TrueNAS UI (or `docker compose up -d
# --force-recreate` if you're running it that way instead) - `docker images
# story-bible` lists what's available to roll back to, since every build
# here is tagged with its version as well as :current.
set -eu

cd "$(dirname "$0")/.."

# One-time migration (#38): deploy/compose.yaml used to be tracked, and the
# pre-fix instructions had you edit real values directly into it. This pull
# renames that tracked path away (to compose.yaml.example) - which git
# refuses to do while your local edits to the old path are uncommitted
# ("local changes would be overwritten by merge"), the exact error that
# prompted this fix in the first place. Detect that situation, back up your
# edited file, and reset it so the pull can proceed; a no-op on every run
# after the first, once deploy/compose.yaml is no longer a tracked path.
if git ls-files --error-unmatch deploy/compose.yaml >/dev/null 2>&1; then
  # HEAD, not a bare `git diff --quiet` (which only compares the working
  # tree to the index): a `git add`-ed but uncommitted edit is clean
  # against the index and would otherwise slip past this check, hitting
  # the same "local changes would be overwritten by merge" pull failure.
  if ! git diff --quiet HEAD -- deploy/compose.yaml; then
    # The file may have been deleted rather than edited - nothing to back up then.
    [ -f deploy/compose.yaml ] && cp deploy/compose.yaml deploy/compose.yaml.bak
    # HEAD, not a bare `git checkout --` (which restores from the index -
    # a no-op if the edit was staged, leaving it right where it started).
    git checkout HEAD -- deploy/compose.yaml
    echo "Backed up your existing deploy/compose.yaml (with local edits) to"
    echo "deploy/compose.yaml.bak - copy your real values from it into the"
    echo "new deploy/compose.yaml this script creates below."
  fi
fi

git pull

VERSION="$(git describe --tags --always --dirty)"
docker build -t "story-bible:$VERSION" -t story-bible:current .

# deploy/compose.yaml (real values, gitignored - #38) vs. the tracked
# deploy/compose.yaml.example template: first run on a fresh checkout,
# not yet found.
if [ ! -f deploy/compose.yaml ]; then
  cp deploy/compose.yaml.example deploy/compose.yaml
  echo
  echo "Created deploy/compose.yaml from the template - edit it with real"
  echo "values (STORYBIBLE_TOKEN, etc.) before using it - see deploy/README.md."
fi

echo
echo "Built story-bible:$VERSION (tagged story-bible:current)."
echo "Restart the Custom App from the TrueNAS UI to pick it up."
