#!/usr/bin/env python3
"""Centralised configuration loader for the Zerto -> VCF seed migration scripts.

All connection details and credentials are read from environment variables,
which are loaded from a local ``.env`` file that is intentionally excluded from
version control. Copy ``.env.example`` to ``.env`` and fill in your own values.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Resolve .env relative to this file so the scripts work from any CWD.
REPO_ROOT = Path(__file__).resolve().parent
ENV_FILE = REPO_ROOT / ".env"

load_dotenv(dotenv_path=ENV_FILE)


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def get_env(name: str, default: str | None = None, *, required: bool = False) -> str:
    """Return an environment variable, optionally enforcing that it is set."""
    value = os.getenv(name, default)
    if required and not value:
        raise ConfigError(
            f"Required environment variable '{name}' is not set.\n"
            f"Copy .env.example to .env and populate it, or export {name} in your shell."
        )
    return value or ""


def get_bool_env(name: str, default: bool = False) -> bool:
    """Return an environment variable coerced to a boolean."""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def require(*names: str) -> dict[str, str]:
    """Fetch several required variables at once, reporting all missing ones."""
    values: dict[str, str] = {}
    missing: list[str] = []
    for name in names:
        value = os.getenv(name, "")
        if not value:
            missing.append(name)
        values[name] = value
    if missing:
        raise ConfigError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + f"\nExpected them in {ENV_FILE} (copy .env.example to .env) "
            "or exported in your shell."
        )
    return values


def fail(message: str) -> None:
    """Print a configuration error and exit non-zero."""
    print(f"Configuration error: {message}", file=sys.stderr)
    sys.exit(1)


# --- Shared settings -------------------------------------------------------
# Filename of the manifest handed off from script 01 to script 02.
MANIFEST_FILE = get_env("MANIFEST_FILE", "zerto_seeds_manifest.json")

# Selected migration disks and copy results shared by scripts 02 and 03.
MIGRATION_FILE = get_env("MIGRATION_FILE", "zerto_migration_selection.json")

# Set VERIFY_SSL=true once trusted certificates are installed on ZVM/vCenter.
VERIFY_SSL = get_bool_env("VERIFY_SSL", False)


def compute_dest_paths(
    ds_name: str, vm_name: str, source_raw_path: str
) -> tuple[str, str, str, str]:
    """Derive vSphere path strings for a Zerto seed disk copy.

    Given a manifest item's datastore name, VM name, and raw Zerto source
    path, returns a 4-tuple of:
        (normalized_src_path, dest_folder_path, dest_vmdk_path, vmdk_filename)

    This is the single source of truth for the `[Datastore] VM_Name/VMDK`
    naming convention used by 02_vcf_seed_copy.py (which performs the copy)
    and 03_vmdk_descriptor_cleanup.py (which must target the exact same
    destination file afterwards). Keeping this logic in one place prevents
    the two scripts from silently drifting apart.
    """
    normalized_src_path = source_raw_path
    if not normalized_src_path.startswith("["):
        normalized_src_path = f"[{ds_name}] {normalized_src_path}"

    dest_folder_path = f"[{ds_name}] {vm_name}"
    vmdk_filename = normalized_src_path.rsplit("/", 1)[-1]
    dest_vmdk_path = f"[{ds_name}] {vm_name}/{vmdk_filename}"

    return normalized_src_path, dest_folder_path, dest_vmdk_path, vmdk_filename
