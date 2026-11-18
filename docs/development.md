# Development

Working on the code. For deploying, see [`operations.md`](operations.md).


## Python environment

Scripts and tests need this repo's `.venv`, built from Python 3.12 to match the deployed runtime
(`.venv/bin/python -V` to check). A bare `python` fails with `No module named 'google'`.

```
brew install python@3.12
rm -rf .venv
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```


## Lint, format and test

Run before every commit ([Ruff](https://docs.astral.sh/ruff/) settings are in `pyproject.toml`):

```
.venv/bin/ruff format .
.venv/bin/ruff check .        # --fix applies safe fixes
.venv/bin/python -m pytest -q
```


## Source layout

```
src/
  shared/       constants.py (building address, room colours), activities.py, locations.py,
                amilia_client.py, calendar_client.py, event_store.py, …  (used by everything)
  webhook/      main.py (amilia_webhook, process_webhook_task), handlers.py, webhook_queue.py,
                reconcile_queue.py, retry_policy.py
  reconciler/   main.py (reconcile_activity)
scripts/        build.sh, backfill.py, bulk_delete.py, check_access.py, check_storage_access.py
test/
docs/           see docs/README.md
```

`src/webhook/shared/` and `src/reconciler/shared/` are copies made by `scripts/build.sh` before
each deploy. Edit `src/shared/`, never the copies.
