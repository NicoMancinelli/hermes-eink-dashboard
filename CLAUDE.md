# Hermes Kindle Dashboard

Standalone, publishable project. Do not import files, credentials, or state from sibling projects.

## Commands

- Tests: `.venv/bin/python -m pytest`
- One render: `.venv/bin/hermes-kindle-dashboard --render-once /tmp/dashboard.png --width 600 --height 800`
- Build KUAL ZIP: `.venv/bin/python scripts/build_kual_bundle.py --host <reachable-host>`
- Install host: `scripts/install_host.sh --bind <reachable-host>`

## Rules

- Keep committed configuration generic and secret-free.
- The collector is read-only against Hermes SQLite/files.
- Keep Kindle scripts POSIX `/bin/sh` compatible with BusyBox.
- Test both 600x800 and 1072x1448 render sizes.
