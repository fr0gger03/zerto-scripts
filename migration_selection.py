"""Select complete VPGs and persist the shared copy/cleanup migration plan."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

import questionary


COPY_STATUSES = {"pending", "copied", "skipped", "failed"}


def validate_disks(disks: list) -> None:
    """Reject malformed disk lists before any vCenter operations."""
    if not isinstance(disks, list):
        raise ValueError("Expected a list of disk entries")
    for index, disk in enumerate(disks, 1):
        if not isinstance(disk, dict):
            raise ValueError(f"Disk {index} is not an object")
        for field in ("vpg_name", "vm_name", "datastore", "source_raw_path"):
            if not isinstance(disk.get(field), str) or not disk[field].strip():
                raise ValueError(f"Disk {index} has no valid {field}")


def vpg_key(disk: dict) -> str:
    """Use optional VPG IDs when present, otherwise the discovery VPG name."""
    identifier = disk.get("vpg_identifier")
    return f"id:{identifier}" if identifier else f"name:{disk['vpg_name']}"


def choose_disks(disks: list) -> list | None:
    """Prompt for all VPGs or a checkbox subset; None means cancellation."""
    validate_disks(disks)
    mode = questionary.select(
        "Which VPGs should be migrated?",
        choices=[questionary.Choice("Keep all VPGs", value="all"),
                 questionary.Choice("Select a subset of VPGs", value="subset")],
    ).ask()
    if mode is None:
        return None
    if mode == "all":
        return list(disks)
    groups = {}
    for disk in disks:
        groups.setdefault(vpg_key(disk), []).append(disk)
    choices = []
    for key, members in sorted(groups.items(), key=lambda item: item[1][0]["vpg_name"]):
        name = members[0]["vpg_name"]
        identifier = members[0].get("vpg_identifier")
        identity = f" [{identifier}]" if identifier else ""
        vm_count = len({d.get("vm_identifier") or d["vm_name"] for d in members})
        choices.append(questionary.Choice(
            f"{name}{identity} ({vm_count} VM(s), {len(members)} disk(s))", value=key))
    selected = questionary.checkbox(
        "Select VPGs (Space to toggle, Enter to accept):", choices=choices,
        validate=lambda values: bool(values) or "Select at least one VPG.",
    ).ask()
    if selected is None:
        return None
    if not selected:
        raise ValueError("No VPGs selected")
    return [disk for disk in disks if vpg_key(disk) in selected]


def create_plan(disks: list, source: str) -> dict:
    """Create a durable selection; copying updates each disk's status separately."""
    validate_disks(disks)
    groups = {}
    for disk in disks:
        groups.setdefault(vpg_key(disk), {
            "vpg_name": disk["vpg_name"], "vpg_identifier": disk.get("vpg_identifier")})
    return {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_manifest": str(Path(source).resolve()),
        "selected_vpgs": list(groups.values()),
        "disks": [{**disk, "copy_status": "pending"} for disk in disks],
    }


def load_plan(path: str) -> dict:
    """Load only a migration plan, never silently fall back to full discovery."""
    with open(path) as stream:
        plan = json.load(stream)
    if not isinstance(plan, dict) or plan.get("schema_version") != 1:
        raise ValueError("Expected a migration selection JSON created by script 02")
    validate_disks(plan.get("disks"))
    for disk in plan["disks"]:
        if disk.get("copy_status") not in COPY_STATUSES:
            raise ValueError("Migration disk has an invalid or missing copy_status")
    return plan


def save_plan(plan: dict, path: str) -> None:
    """Replace the plan atomically so interruption cannot leave partial JSON."""
    destination = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=destination.parent,
                                         prefix=f".{destination.name}.", delete=False) as stream:
            temporary = stream.name
            json.dump(plan, stream, indent=4)
            stream.write("\n")
        os.replace(temporary, destination)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
