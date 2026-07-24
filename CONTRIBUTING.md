# Contributing

1. Create a branch from `main`.
2. Keep committed configs free of hostnames, IPs, tokens, and personal paths.
3. Add a failing test before behavior changes.
4. Run:

   ```sh
   python3 -m venv .venv
   .venv/bin/pip install -e '.[dev]'
   .venv/bin/pytest
   shellcheck -s sh kindle/hermes_dashboard/bin/*.sh scripts/*.sh
   ```

5. Test shell changes with BusyBox `sh` on a Kindle when possible.

Changes to the state collector must remain read-only and degrade cleanly when a Hermes table or file is absent.
