#!/usr/bin/env bash
# Commit the files a run wrote and push them, surviving concurrent edits.
#
# The bot only owns state.json and alert_state.json; paper_trades.csv is shared
# with the user. Instead of a git rebase (which stops on a CSV conflict), each
# attempt starts from the latest remote branch, copies the bot's files over it
# and re-applies this run's exits to the user's current CSV (a row-by-row
# three-way merge against the CSV the run started from).
set -euo pipefail

BRANCH="${BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"
PYTHON="${PYTHON:-python}"
SAVE="$(mktemp -d)"
cp data/state.json data/alert_state.json data/paper_trades.csv "$SAVE"/
# the run never commits, so HEAD still holds the CSV it read
git show HEAD:data/paper_trades.csv > "$SAVE/base.csv" 2>/dev/null || : > "$SAVE/base.csv"

git config user.name "github-actions[bot]"
git config user.email "41898283+github-actions[bot]@users.noreply.github.com"

for attempt in 1 2 3 4 5; do
  git fetch --quiet origin "$BRANCH"
  git reset --quiet --hard "origin/$BRANCH"
  cp "$SAVE/state.json" "$SAVE/alert_state.json" data/
  "$PYTHON" -m src.trades_file reconcile "$SAVE/base.csv" "$SAVE/paper_trades.csv" \
    data/paper_trades.csv
  git add data/state.json data/alert_state.json data/paper_trades.csv
  if git diff --cached --quiet; then
    echo "Nothing to commit."
    exit 0
  fi
  git commit --quiet -m "Paper run $(date -u +%Y-%m-%dT%H:%MZ)"
  if git push --quiet origin "HEAD:$BRANCH"; then
    echo "Pushed on attempt $attempt."
    exit 0
  fi
  echo "Push rejected (attempt $attempt), retrying on the latest $BRANCH."
  sleep $((attempt * 3))
done
echo "Could not push after 5 attempts." >&2
exit 1
