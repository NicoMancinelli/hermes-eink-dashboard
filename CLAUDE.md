# Hermes E-Ink Dashboard

Standalone, publishable project — the single canonical repo for the Hermes
E-Ink dashboard (the former `hermes-kindle-dashboard` repo was consolidated
here; Kindle is one adapter). Do not import files, credentials, or state from
sibling projects.

The Python package is `hermes_eink_dashboard`. `hermes_kindle_dashboard` and
the `hermes-kindle-dashboard` console command remain as deprecated aliases.

## Commands

- Tests: `.venv/bin/python -m pytest`
- One render: `.venv/bin/hermes-eink-dashboard --render-once /tmp/dashboard.png --width 600 --height 800`
- Build KUAL ZIP: `.venv/bin/python scripts/build_kual_bundle.py --host <reachable-host>`
- Install host: `scripts/install_host.sh --bind <reachable-host>`

## Rules

- Keep committed configuration generic and secret-free.
- The collector is read-only against Hermes SQLite/files.
- Keep Kindle scripts POSIX `/bin/sh` compatible with BusyBox.
- Test both 600x800 and 1072x1448 render sizes.
