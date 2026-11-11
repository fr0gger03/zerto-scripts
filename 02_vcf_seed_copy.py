#!/usr/bin/env python3

import argparse
import json
from pathlib import Path
import ssl
import sys

import requests
from pyvim.connect import Disconnect, SmartConnect
from pyVmomi import vim
from rich.console import Console
from rich.prompt import Confirm

import config
from descriptor_cleanup import build_datastore_url, clean_source_descriptor
from migration_selection import choose_disks, create_plan, load_plan, save_plan, validate_disks

MANIFEST_FILE = config.MANIFEST_FILE
VERIFY_SSL = config.VERIFY_SSL

console = Console()


def wait_for_task(task):
    """Wait for a vSphere asynchronous task to complete."""
    while True:
        if task.info.state == "success":
            return True
        if task.info.state == "error":
            console.print(f"[bold red]Task Error:[/bold red] {task.info.error.msg}")
            return False


def main():
    console.rule("[bold cyan]vSphere Datastore Seed Copier")

    parser = argparse.ArgumentParser(description="Choose VPGs, clean source descriptors, and copy seed disks.")
    parser.add_argument("--migration-file", default=config.MIGRATION_FILE,
                        help="Migration selection JSON with cleanup and copy results.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Connect to vCenter and preview source cleanup/copies without remote writes.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--select-only", action="store_true", help="Save VPG selection without connecting to vCenter.")
    mode.add_argument("--reuse-selection", action="store_true", help="Copy pending/skipped/failed disks from a saved selection.")
    args = parser.parse_args()
    if args.select_only and args.dry_run:
        parser.error("--select-only cannot be combined with --dry-run")
    try:
        if Path(args.migration_file).resolve() == Path(MANIFEST_FILE).resolve():
            raise ValueError("Migration output must differ from the discovery manifest")
        if args.reuse_selection:
            plan = load_plan(args.migration_file)
        else:
            with open(MANIFEST_FILE) as stream:
                discovery = json.load(stream)
            validate_disks(discovery)
            if not discovery:
                console.print("[yellow]Discovery manifest is empty. Nothing to select.[/yellow]")
                return
            manifest = choose_disks(discovery)
            if manifest is None:
                console.print("[yellow]Selection cancelled. No files changed.[/yellow]")
                return
            plan = create_plan(manifest, MANIFEST_FILE)
            if Path(args.migration_file).exists() and not Confirm.ask(
                f"Replace existing migration selection {args.migration_file}?", default=False
            ):
                console.print("[yellow]Selection not saved. No copy started.[/yellow]")
                return
            save_plan(plan, args.migration_file)
        manifest = [disk for disk in plan["disks"] if disk["copy_status"] != "copied"]
        console.print(f"Migration selection: {args.migration_file} ({len(plan['disks'])} disk(s), "
                      f"{len(manifest)} remaining).")
        if args.select_only:
            console.print("Selection saved. Keep target disks when unprotecting selected VPGs, "
                          "then rerun with --reuse-selection.")
            return
        if not manifest:
            console.print("[yellow]No remaining disks to copy.[/yellow]")
            return
        env = config.require("VCENTER_IP", "VCENTER_USER", "VCENTER_PASSWORD", "VCENTER_DATACENTER")
    except (OSError, ValueError, config.ConfigError) as exc:
        console.print(f"Cannot prepare migration: {exc}", style="red", markup=False)
        sys.exit(1)

    VCENTER_IP = env["VCENTER_IP"]
    VCENTER_USER = env["VCENTER_USER"]
    VCENTER_PASSWORD = env["VCENTER_PASSWORD"]
    DATACENTER_NAME = env["VCENTER_DATACENTER"]

    if args.dry_run:
        console.print("[yellow]Dry run: reads source descriptors; no remote writes or copy tasks.[/yellow]")
    else:
        console.print(
            "[bold yellow]Source descriptors will be edited in place without separate backups. "
            "Original filter assignments are retained as comments.[/bold yellow]"
        )
        if not Confirm.ask(
            "Have you unprotected the selected workloads using Keep target disks, "
            "released their locks, and approved source cleanup?", default=False
        ):
            console.print("[yellow]No source cleanup or copy started.[/yellow]")
            return

    # 2. Connect to vCenter
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
        exit(1)

    session = requests.Session()
    try:
        session.headers.update({"Cookie": si._stub.cookie})
        content = si.RetrieveContent()
        file_mgr = content.fileManager
        vdisk_mgr = content.virtualDiskManager

        # Locate Datacenter Object
        datacenter = None
        for child in content.rootFolder.childEntity:
            if isinstance(child, vim.Datacenter) and child.name == DATACENTER_NAME:
                datacenter = child
                break

        if not datacenter:
            console.print(f"[bold red]Datacenter '{DATACENTER_NAME}' not found in vCenter![/bold red]")
            exit(1)

        # 3. Process Each Seed Copy
        for idx, item in enumerate(manifest, 1):
            ds_name = item["datastore"]
            vm_name = item["vm_name"]

            # Use the same normalized source for descriptor cleanup and copying.
            src_raw_path, dest_folder_path, dest_vmdk_path, _vmdk_filename = (
                config.compute_dest_paths(ds_name, vm_name, item["source_raw_path"])
            )

            console.print(f"\n[bold underline]Volume {idx} of {len(manifest)}:[/bold underline]")
            console.print(f"  [cyan]VM Name:[/cyan]           {vm_name}")
            console.print(f"  [cyan]Source Path:[/cyan]       {src_raw_path}")
            console.print(f"  [cyan]Target Folder Path:[/cyan] {dest_folder_path}")
            console.print(f"  [cyan]Destination Seed:[/cyan]  {dest_vmdk_path}")

            action = "Preview cleanup and copy" if args.dry_run else "Clean source descriptor and copy"
            if Confirm.ask(f"{action} for [bold green]{vm_name}[/bold green]?"):
                try:
                    url = build_datastore_url(
                        VCENTER_IP, DATACENTER_NAME, ds_name, src_raw_path
                    )
                    cleanup_status, changed = clean_source_descriptor(
                        session, url, VERIFY_SSL, dry_run=args.dry_run
                    )
                except (requests.RequestException, ValueError, UnicodeError) as exc:
                    console.print(f"Source cleanup failed for {vm_name}: {exc}", style="red", markup=False)
                    if not args.dry_run:
                        item["cleanup_status"] = "failed"
                        item["cleanup_error"] = str(exc)
                        item["copy_status"] = "failed"
                        save_plan(plan, args.migration_file)
                    continue
                console.print(f"  Source descriptor: {cleanup_status}; {len(changed)} assignment(s).")
                for line in changed:
                    console.print(f"    - {line}\n    + #{line}", markup=False)
                if args.dry_run:
                    console.print("  Would copy the cleaned source to the destination; no changes written.")
                    continue
                item["cleanup_status"] = cleanup_status
                item.pop("cleanup_error", None)
                save_plan(plan, args.migration_file)
                # --- STEP A: Create Folder using Key/Value Keyword Arguments ---
                console.print(f"  [dim]Creating folder: {dest_folder_path}[/dim]")
                try:
                    file_mgr.MakeDirectory(
                        name=dest_folder_path,
                        datacenter=datacenter,
                        createParentDirectories=True,
                    )
                    console.print("  [green]✔ Folder created successfully.[/green]")
                except Exception as ex:
                    # Ignore if directory already exists
                    if "already exists" in str(ex).lower():
                        console.print("  [dim yellow]Folder already exists.[/dim yellow]")
                    else:
                        console.print(f"  [yellow]MakeDirectory notice:[/yellow] {ex}")

                # --- STEP B: Copy Virtual Disk ---
                with console.status(f"[bold magenta]Copying Virtual Disk for {vm_name}..."):
                    try:
                        copy_task = vdisk_mgr.CopyVirtualDisk_Task(
                            sourceName=src_raw_path,
                            sourceDatacenter=datacenter,
                            destName=dest_vmdk_path,
                            destDatacenter=datacenter,
                            force=False,
                        )
                        success = wait_for_task(copy_task)
                    except Exception as exc:
                        console.print(f"Copy failed for {vm_name}: {exc}", style="red", markup=False)
                        success = False
                item["copy_status"] = "copied" if success else "failed"
                save_plan(plan, args.migration_file)

                if success:
                    console.print(f"[bold green]✔ Successfully copied seed VMDK for {vm_name}![/bold green]")
            else:
                if not args.dry_run:
                    item["copy_status"] = "skipped"
                    save_plan(plan, args.migration_file)
                console.print(f"[yellow]Skipped copy for {vm_name}.[/yellow]")

    finally:
        session.close()
        Disconnect(si)
        console.print("\n[bold blue]Disconnected from vCenter. Execution complete.[/bold blue]\n")


if __name__ == "__main__":
    main()
