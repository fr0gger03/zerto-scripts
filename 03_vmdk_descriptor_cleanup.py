#!/usr/bin/env python3
"""Remove lingering Zerto I/O filter references from copied seed VMDKs.

Run this AFTER 02_vcf_seed_copy.py has copied seed disks into their
`[Datastore] VM_Name/VMDK` destination.

If the source Zerto replica had an I/O filter (e.g. vmwarelwd) attached for
replication, the copied disk descriptor may retain `ddb.iofilters` /
`ddb.sidecars` references that point at filter sidecar files which no longer
exist on the target. VMware Cloud Foundation Protection & Recovery / vSphere
Replication can then fail to power on the resulting VM with errors like:

    "Cannot open the disk '...' or one of the snapshot disks it depends on."

See: https://knowledge.broadcom.com/external/article/334555/

Per that KB, the fix is to comment out (not delete) the offending lines in
the disk descriptor file. This script edits ONLY the small text descriptor
(the `.vmdk` referenced in the migration selection) -- never the large `-flat.vmdk` data
extent -- by fetching it over vCenter's datastore HTTP file-access API,
reusing the existing pyVmomi session (no separate ESXi/SSH credentials
needed), commenting out the offending lines, and writing it back in place.

Note: no separate backup copy is made here -- 02_vcf_seed_copy.py already
copies the seed disk out from the original Zerto target, so the source disk
serves as the recovery point if something needs to be redone.
"""

import argparse
import ssl
import sys
from urllib.parse import quote, urlencode

import requests
from pyvim.connect import Disconnect, SmartConnect
from pyVmomi import vim
from rich.console import Console
from rich.prompt import Confirm

import config
from migration_selection import load_plan

# --- vCenter Configuration (loaded from .env — see .env.example) ---
# Reuses the same vCenter variables as 02_vcf_seed_copy.py.
try:
    _env = config.require(
        "VCENTER_IP",
        "VCENTER_USER",
        "VCENTER_PASSWORD",
        "VCENTER_DATACENTER",
    )
except config.ConfigError as exc:
    config.fail(str(exc))

VCENTER_IP = _env["VCENTER_IP"]
VCENTER_USER = _env["VCENTER_USER"]
VCENTER_PASSWORD = _env["VCENTER_PASSWORD"]
DATACENTER_NAME = _env["VCENTER_DATACENTER"]
MIGRATION_FILE = config.MIGRATION_FILE
VERIFY_SSL = config.VERIFY_SSL

# Descriptor keys known to cause power-on failures when they reference an
# I/O filter (e.g. Zerto's vmwarelwd) that is no longer attached/valid.
# See: https://knowledge.broadcom.com/external/article/334555/
TARGET_KEYS = ("ddb.iofilters", "ddb.sidecars")

console = Console()

if not VERIFY_SSL:
    requests.packages.urllib3.disable_warnings()


def build_datastore_url(ds_name: str, vm_name: str, vmdk_filename: str) -> str:
    """Build the vCenter datastore HTTP file-access URL for a descriptor file."""
    relative_path = quote(f"{vm_name}/{vmdk_filename}")
    query = urlencode({"dcPath": DATACENTER_NAME, "dsName": ds_name})
    return f"https://{VCENTER_IP}/folder/{relative_path}?{query}"


def scrub_descriptor(text: str) -> tuple[str, list[str]]:
    """Comment out lingering ddb.iofilters / ddb.sidecars lines.

    Returns the (possibly) modified text and the list of original lines that
    were changed. Lines already commented out are left untouched, so this is
    safe to re-run against the same descriptor.
    """
    changed: list[str] = []
    out_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        lower = stripped.lower()
        is_target = any(lower.startswith(key) for key in TARGET_KEYS)
        if is_target and not stripped.startswith("#"):
            out_lines.append(f"#{line}")
            changed.append(line)
        else:
            out_lines.append(line)
    new_text = "\n".join(out_lines)
    if text.endswith("\n"):
        new_text += "\n"
    return new_text, changed


def get_session_cookie(si) -> str:
    """Reuse the authenticated pyVmomi SOAP session for HTTP file access.

    This relies on pyVmomi's stub cookie, the same technique used by
    VMware's own pyvmomi-community-samples for uploading/downloading
    datastore files without a second, separate login.
    """
    return si._stub.cookie


def fetch_descriptor(session: requests.Session, url: str) -> str | None:
    resp = session.get(url, verify=VERIFY_SSL)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.text


def upload_descriptor(session: requests.Session, url: str, content: str) -> None:
    resp = session.put(
        url,
        data=content.encode("utf-8"),
        headers={"Content-Type": "application/octet-stream"},
        verify=VERIFY_SSL,
    )
    resp.raise_for_status()


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Remove lingering Zerto I/O filter references from copied seed "
            "VMDK descriptors (Broadcom KB 334555)."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without modifying any files.",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Apply changes without per-VM confirmation prompts.",
    )
    parser.add_argument(
        "--migration-file", default=MIGRATION_FILE,
        help="Migration selection JSON saved by script 02 (only copied disks are cleaned).",
    )
    args = parser.parse_args()

    console.rule("[bold cyan]VMDK Descriptor I/O Filter Cleanup")
    console.print(
        "[dim]Ref: https://knowledge.broadcom.com/external/article/334555/[/dim]\n"
    )

    # Read the same selection and copy results used by script 02.
    try:
        plan = load_plan(args.migration_file)
    except (OSError, ValueError) as exc:
        console.print(f"Cannot load migration selection {args.migration_file}: {exc}", style="red", markup=False)
        console.print("Run script 02 first to select VPGs and copy their disks.")
        sys.exit(1)
    manifest = [disk for disk in plan["disks"] if disk["copy_status"] == "copied"]
    skipped = len(plan["disks"]) - len(manifest)
    console.print(f"Migration selection: {args.migration_file}; {len(manifest)} copied disk(s), "
                  f"{skipped} pending/skipped/failed disk(s) excluded.")
    if not manifest:
        console.print("[yellow]No successfully copied disks to clean up. Run script 02 first.[/yellow]")
        return

    # 2. Connect to vCenter (also used to mint the HTTP session cookie)
    console.print(f"[bold green]Connecting to vCenter {VCENTER_IP}...[/bold green]")
    try:
        ssl_context = (
            ssl.create_default_context()
            if VERIFY_SSL
            else ssl._create_unverified_context()
        )
        si = SmartConnect(
            host=VCENTER_IP,
            user=VCENTER_USER,
            pwd=VCENTER_PASSWORD,
            sslContext=ssl_context,
        )
    except Exception as e:
        console.print(f"[bold red]Failed to connect to vCenter:[/bold red] {e}")
        sys.exit(1)

    try:
        vc_content = si.RetrieveContent()

        # Validate the datacenter exists (fail fast, matches script 2's behaviour).
        datacenter = None
        for child in vc_content.rootFolder.childEntity:
            if isinstance(child, vim.Datacenter) and child.name == DATACENTER_NAME:
                datacenter = child
                break
        if not datacenter:
            console.print(f"[bold red]Datacenter '{DATACENTER_NAME}' not found in vCenter![/bold red]")
            sys.exit(1)

        session = requests.Session()
        session.headers.update({"Cookie": get_session_cookie(si)})

        # 3. Process successfully copied disks from the migration selection
        for idx, item in enumerate(manifest, 1):
            ds_name = item["datastore"]
            vm_name = item["vm_name"]

            _src, _folder, _dest, vmdk_filename = config.compute_dest_paths(
                ds_name, vm_name, item["source_raw_path"]
            )

            console.print(f"\n[bold underline]Volume {idx} of {len(manifest)}:[/bold underline] {vm_name}")
            url = build_datastore_url(ds_name, vm_name, vmdk_filename)

            try:
                descriptor = fetch_descriptor(session, url)
            except requests.HTTPError as e:
                console.print(f"  [bold red]Failed to fetch descriptor:[/bold red] {e}")
                continue

            if descriptor is None:
                console.print(
                    "  [yellow]Descriptor not found at expected path "
                    "(was it copied by script 2?):[/yellow]"
                )
                console.print(f"  [dim]{url}[/dim]")
                continue

            new_text, changed_lines = scrub_descriptor(descriptor)

            if not changed_lines:
                console.print("  [green]✔ No lingering ddb.iofilters / ddb.sidecars references found.[/green]")
                continue

            console.print("  [bold yellow]Found lingering I/O filter reference(s):[/bold yellow]")
            for line in changed_lines:
                console.print(f"    [red]- {line.strip()}[/red]")
                console.print(f"    [green]+ #{line.strip()}[/green]")

            if args.dry_run:
                console.print("  [dim](dry run: no changes written)[/dim]")
                continue

            if not args.yes and not Confirm.ask(
                f"  Comment out these line(s) for [bold]{vm_name}[/bold]?"
            ):
                console.print("  [yellow]Skipped.[/yellow]")
                continue

            try:
                upload_descriptor(session, url, new_text)
            except requests.HTTPError as e:
                console.print(f"  [bold red]Failed to write descriptor:[/bold red] {e}")
                continue

            console.print(f"  [bold green]✔ Descriptor updated for {vm_name}.[/bold green]")

    finally:
        Disconnect(si)
        console.print("\n[bold blue]Disconnected from vCenter. Execution complete.[/bold blue]\n")


if __name__ == "__main__":
    main()
