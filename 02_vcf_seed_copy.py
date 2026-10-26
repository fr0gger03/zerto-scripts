#!/usr/bin/env python3

import argparse
import json
from pathlib import Path
import ssl
import sys

from pyvim.connect import Disconnect, SmartConnect
from pyVmomi import vim
from rich.console import Console
from rich.prompt import Confirm

import config
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

    parser = argparse.ArgumentParser(description="Choose VPGs, save a migration plan, and copy seed disks.")
    parser.add_argument("--migration-file", default=config.MIGRATION_FILE,
                        help="Shared migration selection JSON used by scripts 02 and 03.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--select-only", action="store_true", help="Save VPG selection without connecting to vCenter.")
    mode.add_argument("--reuse-selection", action="store_true", help="Copy pending/skipped/failed disks from a saved selection.")
    args = parser.parse_args()
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
                      f"{len(manifest)} remaining). Script 03 uses this same file.")
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

    try:
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

            # Shared with 03_vmdk_descriptor_cleanup.py so both scripts agree
            # on exactly where the seed disk ends up.
            src_raw_path, dest_folder_path, dest_vmdk_path, vmdk_filename = (
                config.compute_dest_paths(ds_name, vm_name, item["source_raw_path"])
            )

            console.print(f"\n[bold underline]Volume {idx} of {len(manifest)}:[/bold underline]")
            console.print(f"  [cyan]VM Name:[/cyan]           {vm_name}")
            console.print(f"  [cyan]Source Path:[/cyan]       {src_raw_path}")
            console.print(f"  [cyan]Target Folder Path:[/cyan] {dest_folder_path}")
            console.print(f"  [cyan]Destination Seed:[/cyan]  {dest_vmdk_path}")

            if Confirm.ask(f"Copy seed VMDK for [bold green]{vm_name}[/bold green]?"):
                
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
                item["copy_status"] = "skipped"
                save_plan(plan, args.migration_file)
                console.print(f"[yellow]Skipped copy for {vm_name}.[/yellow]")

    finally:
        Disconnect(si)
        console.print("\n[bold blue]Disconnected from vCenter. Execution complete.[/bold blue]\n")


if __name__ == "__main__":
    main()
