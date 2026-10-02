#!/bin/sh
# Image start command. The ledger guard must exit 0 before the server binds.
# A tree without src.tools.publication_ledger_guard fails closed here.
set -eu
python -c 'from src.tools.publication_ledger_guard import main; raise SystemExit(main())'
exec uvicorn runner_api:app --host 0.0.0.0 --port "${PORT:-8000}"
