#!/usr/bin/env bash
# Copies the shared library into each Cloud Function's own deploy
# directory. Cloud Functions' buildpack deploy uploads exactly the
# --source directory and nothing outside it (and always imports a file
# named main.py from within it), so src/webhook/ and src/reconciler/ each
# need their own physical copy of src/shared/ rather than reaching across
# directories. Run this before every `gcloud functions deploy` — see
# README.md and each function's own main.py docstring.
#
# The generated shared/ copies are gitignored: src/shared/ is the single
# source of truth, these are just deploy-time artifacts.
set -euo pipefail
cd "$(dirname "$0")/.."

for target in src/webhook src/reconciler; do
  rm -rf "$target/shared"
  cp -r src/shared "$target/shared"
  echo "Synced src/shared/ -> $target/shared/"
done
