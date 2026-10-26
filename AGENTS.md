# Repository Guidelines

## Project Structure & Module Organization

This repository migrates Zerto replica VMDKs into VMware Cloud Foundation or vSphere Replication seed disks. Python scripts live at the repository root and run in sequence:

- `01_zerto_discover_and_export.py`: discovers replicas and exports a JSON manifest.
- `02_vcf_seed_copy.py`: selects VPGs with Questionary, saves migration JSON, and copies selected disks.
- `03_vmdk_descriptor_cleanup.py`: reads the migration JSON and cleans successfully copied descriptors.
- `config.py`: loads environment settings, validates required variables, and computes shared datastore paths.

`README.md` documents the workflow; `keycloak.md` covers authentication. Documentation screenshots belong in `images/`. There is no dedicated source package. `journal_stats.py` holds assessment calculations; `tests/` contains offline unit tests. See `seed-assessment.md` for interpretation.

## Build, Test, and Development Commands

Use Python 3.13+ as required by `pyproject.toml`.

```bash
uv sync                                    # Install locked dependencies
cp .env.example .env                       # Initialize local settings
uv run python -m compileall -q config.py 0*.py  # Check syntax locally
uv run 01_zerto_discover_and_export.py      # Discover and export
uv run 02_vcf_seed_copy.py                  # Copy disks interactively
uv run 03_vmdk_descriptor_cleanup.py --dry-run  # Preview descriptor edits
```

Populate `.env` before connecting. Run discovery while protection is active; release Zerto locks using **Keep target disks** before copying. Cleanup follows copying. Its dry run still connects to vCenter. No build step is required.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` functions and variables, and `UPPER_SNAKE_CASE` constants. Preserve numbered workflow filenames. Follow existing import grouping, add docstrings and type hints to reusable helpers, and centralize shared settings and path logic in `config.py`. Use Rich for terminal output. No formatter or linter is configured.

## Testing Guidelines

Run `uv run python -m unittest discover -s tests -v` for offline tests using standard-library `unittest`. No coverage threshold is configured. Check syntax locally and validate infrastructure changes in a lab. Name tests `tests/test_*.py` and mock external APIs. Prioritize datastore paths, missing configuration, and descriptor cleanup idempotency. Confirm cleanup preserves unrelated descriptor content and never edits `-flat.vmdk` extents.

## Commit & Pull Request Guidelines

History uses short, descriptive messages such as `updated uv lock`; no strict prefix convention is established. Keep commits focused. PRs should describe behavior changes, affected workflow steps, and validation performed. Link relevant issues and include screenshots when terminal output or documentation visuals change. Update documentation and `uv.lock` alongside relevant changes.

## Security & Configuration

Never commit `.env`, credentials, or generated manifests containing infrastructure details. Keep `.env.example` limited to placeholders. Prefer `VERIFY_SSL=true` with trusted certificates, and preserve interactive confirmations and cleanup dry-run behavior.
