# Repo consolidation: `hermes-kindle-dashboard` → `hermes-eink-dashboard`

## Decision

The two repositories are consolidated into **`hermes-eink-dashboard`**, which
is now the single canonical home for the project. `hermes-kindle-dashboard` is
retired (pointer README + GitHub archive). Kindle remains a first-class
**adapter/build target** inside this repo, not a separate product.

## Why (the evidence)

The repos were never two parallel products — they are one project and a frozen
snapshot of its own first commit:

- **Shared ancestry.** Both repos contain the exact commit
  `bf60eca` ("add Hermes Kindle E-Ink dashboard"). `hermes-kindle-dashboard`
  has *only* that commit; `hermes-eink-dashboard` has 9 more on top.
- **Superset, not sibling.** Every file in the kindle repo exists here. This
  repo additionally has `aggregators/`, `actions*.py`, `api.py`, `config.py`,
  `contract.py`, `scheduler.py`, the interactive client, and more tests.
  LOC: ~1,187 (kindle) vs ~5,194 (eink).
- **Same Python package name.** Both shipped `src/hermes_kindle_dashboard/`,
  so they could not even be installed into the same environment, and every
  shared-file fix had to be hand-ported. `server.py` had already diverged by
  ~280 lines and `render.py` by ~157.
- **Design intent.** This repo's README already frames a *multi-device* E-Ink
  gateway with a versioned device-neutral `/dashboard-data` contract and the
  Kindle PNG route as a "compatibility adapter".

"Develop both simultaneously" would mean permanently reconciling two copies of
the same package by hand. Since the split, the kindle repo has received zero
commits — parallel development already failed in practice.

## What this change did

1. **Renamed the Python package** `hermes_kindle_dashboard` →
   `hermes_eink_dashboard` (`git mv` of `src/`), updating all internal tests.
2. **Compatibility shim** at `src/hermes_kindle_dashboard/__init__.py`:
   `import hermes_kindle_dashboard[...]` still resolves to the *same* module
   objects as the new package, and emits a `DeprecationWarning`. Covered by
   `tests/test_compat_alias.py`.
3. **Distribution + console scripts** in `pyproject.toml`:
   - dist name `hermes-kindle-dashboard` → `hermes-eink-dashboard`
   - primary console command `hermes-eink-dashboard`
   - `hermes-kindle-dashboard` kept as a deprecated console alias (so
     `install_host.sh` and its tests keep working).
4. **Docs** (`README.md`, `CLAUDE.md`) updated to the canonical name with a
   consolidation note; `install.sh` invokes the primary console command.
5. **Kindle stays an adapter target** — `kindle/`, the `/dashboard.png`
   compatibility route, and the KUAL bundle are unchanged.

Full test suite: **114 passed**. The single failing test
(`test_ops.py::test_host_installer_generates_control_token`) fails only
because the sandbox blocks the real `pip install` the installer performs
(self-signed proxy cert) — unrelated to this change and pre-existing.

## Deliberately deferred (follow-ups, not done here)

These were left out on purpose to avoid breaking existing installs and to keep
this change reviewable. Each is a separate, opt-in migration:

- **Runtime paths & unit name.** `install_host.sh` still writes
  `~/.config/hermes-kindle-dashboard/`, `~/.local/share/hermes-kindle-dashboard/`
  and the `hermes-kindle-dashboard` service. (Note: `install.sh` already uses
  `hermes-eink-dashboard` dirs — the two installers are inconsistent and should
  be reconciled.) Renaming runtime dirs needs a migration that moves existing
  users' tokens/config; do it with a shim, then update `tests/test_ops.py`.
- **Logger names.** Internal loggers still log as `hermes-kindle-dashboard.*`
  (cosmetic).
- **Remove the deprecated aliases.** After a deprecation window, drop
  `src/hermes_kindle_dashboard/` and the `hermes-kindle-dashboard` console
  entry point (target: next minor release that bumps the deprecation).

## Retire `hermes-kindle-dashboard`

1. Replace its README with a pointer to this repo (done on the consolidation
   branch there).
2. **Archive** the repo on GitHub (Settings → Archive) — do not delete, so the
   published `install.sh` one-liner and existing clones/links keep resolving.
3. Redirect open issues here.
