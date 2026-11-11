# Repository Guidelines

## Project Structure & Module Organization

This repository migrates Zerto replica VMDKs into VMware Cloud Foundation or vSphere Replication seed disks. Python scripts live at the repository root and run in sequence:

- `01_zerto_discover_and_export.py`: discovers replicas and exports a JSON manifest.
- `02_vcf_seed_copy.py`: selects VPGs with Questionary, saves migration JSON, cleans selected source descriptors, and copies disks only after successful cleanup/verification.
- `descriptor_cleanup.py`: validates descriptor paths/content, comments filter assignments, and verifies source writes through vCenter datastore HTTP access.
- `config.py`: loads environment settings, validates required variables, and computes shared datastore paths.

`README.md` documents the workflow; `keycloak.md` covers authentication. Documentation screenshots belong in `images/`. There is no dedicated source package. `journal_stats.py` holds assessment calculations; `tests/` contains offline unit tests. See `seed-assessment.md` for interpretation.

## Build, Test, and Development Commands

Use Python 3.13+ as required by `pyproject.toml`.

```bash
uv venv --python 3.13                       # Once per checkout, if .venv is absent
uv pip sync --python .venv/bin/python --require-hashes requirements.txt --default-index https://pypi.org/simple  # Personal laptop
cp .env.example .env                       # Initialize local settings
uv run --no-sync python -m compileall -q config.py descriptor_cleanup.py migration_selection.py 0*.py tests  # Check syntax locally
uv run --no-sync 01_zerto_discover_and_export.py      # Discover and export
uv run --no-sync 02_vcf_seed_copy.py                  # Copy disks interactively
uv run --no-sync 02_vcf_seed_copy.py --reuse-selection --dry-run  # Preview source edits and copies
```

Populate `.env` before connecting. Run discovery while protection is active; release Zerto locks using **Keep target disks** before source cleanup and copying. Script 02 cleans each confirmed original descriptor before copying; no separate backup is made. Cleanup failure blocks that disk's copy but does not stop other selected disks. Its dry run still connects to vCenter but performs no remote writes. No build step is required.

On the corporate laptop, connect the VPN and replace the public index in the
installation command with `https://packages.vcfd.broadcom.net/artifactory/api/pypi/pypi-virtual/simple`.
Use `uv run --no-sync` for scripts and tests: plain `uv run`/`uv sync` would use
the corporate URLs embedded in `uv.lock`. Keep local index credentials/configuration
out of the repository. `--default-index` does not disable additional indexes.

`uv.lock` is the authoritative dependency resolution; `requirements.txt` is its
portable hash-pinned installation export. After dependency changes, run `uv lock`
on the VPN-connected corporate laptop, then
`uv export --frozen --no-emit-project --output-file requirements.txt`.
Update the dependency declaration, lock, and export together; do not hand-edit the
requirements or rewrite the lock just to switch indexes.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` functions and variables, and `UPPER_SNAKE_CASE` constants. Preserve numbered workflow filenames. Follow existing import grouping, add docstrings and type hints to reusable helpers, and centralize shared settings and path logic in `config.py`. Use Rich for terminal output. No formatter or linter is configured.

## Testing Guidelines

Run `uv run --no-sync python -m unittest discover -s tests -v` for offline tests using standard-library `unittest`. No coverage threshold is configured. Check syntax locally and validate infrastructure changes in a lab. Name tests `tests/test_*.py` and mock external APIs. Prioritize datastore paths, missing configuration, and descriptor cleanup idempotency. Confirm cleanup preserves unrelated descriptor content and never edits `-flat.vmdk` extents.

## Commit & Pull Request Guidelines

History uses short, descriptive messages such as `updated uv lock`; no strict prefix convention is established. Keep commits focused. PRs should describe behavior changes, affected workflow steps, and validation performed. Link relevant issues and include screenshots when terminal output or documentation visuals change. Update documentation and `uv.lock` alongside relevant changes.

## Security & Configuration

Never commit `.env`, credentials, or generated manifests containing infrastructure details. Keep `.env.example` limited to placeholders. Prefer `VERIFY_SSL=true` with trusted certificates, and preserve interactive confirmations and cleanup dry-run behavior.
