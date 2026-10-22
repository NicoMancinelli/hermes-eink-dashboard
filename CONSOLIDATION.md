# Consolidation: `hermes-kindle-dashboard` → `hermes-eink-dashboard`

## Why

`hermes-eink-dashboard` and `hermes-kindle-dashboard` were never two
products — they are **one project and a frozen snapshot of its own first
commit**:

- Both repositories shared exactly one commit, `bf60eca`. The Kindle repo
  has *only* that commit; this repo has many more on top.
- This repo is a **strict superset** (adds `aggregators/`, `actions*.py`,
  `api.py`, `config.py`, `contract.py`, `scheduler.py`, the interactive
  client, more tests).
- Both shipped the **same Python package name** `hermes_kindle_dashboard`,
  so they could not coexist in one environment and every shared-file fix had
  to be hand-ported (`server.py` had already diverged by hundreds of lines).
- The name itself stopped matching the product: the dashboard serves a
  versioned, device-neutral JSON contract (`/dashboard-data`) and renders a
  PNG adapter for the Kindle. Kindle is one *adapter*, not the product.

Decision (as dev lead): this repository is the single canonical project,
Kindle stays a first-class adapter and build target, and the
`hermes-kindle-dashboard` name is retired.

## What changed

### Package

- The Python package was renamed `hermes_kindle_dashboard` →
  `hermes_eink_dashboard` (`git mv`).
- The deprecated `src/hermes_kindle_dashboard/` alias package and the
  `hermes-kindle-dashboard` console alias shipped through the promised
  one-release deprecation cycle and were **removed in v1.0.0**.
- `pyproject.toml`: distribution name → `hermes-eink-dashboard`; console
  commands `hermes-eink-dashboard` and `hermes-dashboard-pair`.

### Runtime paths and service

- `install.sh` (public one-liner), `scripts/install_host.sh`,
  `scripts/uninstall_host.sh` and the systemd unit now consistently use
  `~/.config/hermes-eink-dashboard`, `~/.local/share/hermes-eink-dashboard`
  and `hermes-eink-dashboard.service`. Previously the one-liner installer
  and the source installer disagreed about both paths and unit names.
- The one-liner installer now also generates a **control token** and wires
  both token files into `host.env`, so interactive controls work for
  one-liner installs too.
- `src/hermes_eink_dashboard/paths.py` centralises directory resolution:
  the new directory wins, but a pre-rename installation keeps working from
  `~/.config/hermes-kindle-dashboard` until it is migrated.

### Migration

`scripts/install_host.sh` and `install.sh` migrate an existing install:

1. Tokens (`token`, `control_token`), paired-device records
   (`devices.json`), action state (`actions/`, `actions.yaml`,
   `dismissed_alerts.json`, `context.json`) and `config.yaml` are copied to
   the new directory when they are not already there.
2. The old directory is **renamed, not deleted**:
   `~/.config/hermes-kindle-dashboard` → `~/.config/hermes-kindle-dashboard.migrated`.
3. The old `hermes-kindle-dashboard.service` unit is disabled, stopped and
   removed, so the pre-rename service cannot keep running with stale paths.

## Deliberately deferred

- The KUAL extension id in `kindle/hermes_dashboard/config.xml` stays
  `hermes-kindle-dashboard`: it is the on-device extension identity and
  changing it would orphan existing installs on the Kindle.
- Removal of the deprecated import and console aliases (one release cycle).
- Logger names were renamed to `hermes-eink-dashboard.*` for consistency;
  no behaviour depends on them.

## Testing

- `.venv/bin/python -m pytest` — full suite green.
- `tests/test_compat_alias.py` — the deprecated import path resolves to the
  same module objects and warns.
- `tests/test_paths.py` — runtime directory resolution, including the
  legacy fallback.
- `tests/test_ops.py` — installer/unit assertions for the new paths plus a
  migration test that pre-seeds a legacy config directory and checks that
  tokens and device records survive.

## Companion change

The `hermes-kindle-dashboard` repository gets a redirect README ahead of
GitHub archiving (separate change in that repository).
