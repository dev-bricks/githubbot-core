#!/bin/sh
set -eu
# No fixed runtime root, account, model, token path or full-access setting.
exec "${PYTHON:-python3}" -m githubbot_core multi-bot "$@"
