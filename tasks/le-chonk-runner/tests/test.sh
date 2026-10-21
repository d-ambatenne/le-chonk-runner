#!/usr/bin/env bash
# Harbor verifier for Le Chonk Runner. Harbor copies this folder to /tests and runs this script after the
# agent has finished; it writes /logs/verifier/reward.json (see score.py).
set -u
mkdir -p /logs/verifier

if ! python3 /tests/score.py --project /app --out /logs/verifier; then
  echo "score.py failed: writing a zero reward" >&2
  [ -s /logs/verifier/reward.json ] || echo '{"reward": 0}' > /logs/verifier/reward.json
fi
