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

git pull

VERSION="$(git describe --tags --always --dirty)"
docker build -t "story-bible:$VERSION" -t story-bible:current .

echo
echo "Built story-bible:$VERSION (tagged story-bible:current)."
echo "Restart the Custom App from the TrueNAS UI to pick it up."
