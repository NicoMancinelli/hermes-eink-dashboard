# Hermes E-Ink Dashboard

Standalone, publishable project. Do not import files, credentials, or state from sibling projects.

The Python package is `hermes_eink_dashboard`. The `hermes_kindle_dashboard`
import alias and `hermes-kindle-dashboard` console command were removed in
v1.0.0 after their deprecation cycle (see `CONSOLIDATION.md`).

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
