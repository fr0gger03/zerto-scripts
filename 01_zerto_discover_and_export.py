#!/usr/bin/env python3
"""Discover recovery disks and report advisory journal storage assessments."""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys

import requests
from rich.console import Console
from rich.table import Table

import config
from journal_stats import assess, get_val, vm_identifier, volume_type

console = Console()


class ZertoClient:
    """Keep appliance bearer and legacy session authentication separate."""

    def __init__(self, host: str, port: int, mode: str, verify: bool, timeout: float):
        self.base_url = f"https://{host}:{port}"
        self.mode = mode
        self.session = requests.Session()
        self.session.verify = verify
        self.timeout = timeout
        self.session.headers.update({"Accept": "application/json"})

    def authenticate(self) -> None:
        if self.mode == "oauth":
            env = config.require("ZVM_CLIENT_ID", "ZVM_CLIENT_SECRET")
            response = self.session.post(
                f"{self.base_url}/auth/realms/zerto/protocol/openid-connect/token",
                data={"grant_type": "client_credentials", "client_id": env["ZVM_CLIENT_ID"],
                      "client_secret": env["ZVM_CLIENT_SECRET"]}, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
            token = payload.get("access_token") if isinstance(payload, dict) else None
            if not isinstance(token, str) or not token:
                raise ValueError("OAuth response did not include an access token")
            self.session.headers["Authorization"] = f"Bearer {token}"
        else:
            env = config.require("ZVM_USER", "ZVM_PASSWORD")
            response = self.session.post(
                f"{self.base_url}/v1/session/add", auth=(env["ZVM_USER"], env["ZVM_PASSWORD"]),
                json={"AuthenticationMethod": 1}, timeout=self.timeout)
            response.raise_for_status()
            token = response.headers.get("x-zerto-session")
            if not token:
                raise ValueError("Legacy response did not include x-zerto-session")
            self.session.headers["x-zerto-session"] = token

    def get_list(self, endpoint: str) -> list[dict]:
        response = self.session.get(f"{self.base_url}/v1/{endpoint}", timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise ValueError(f"{endpoint}: expected an array of objects")
        return data

    def close(self) -> None:
        try:
            if "x-zerto-session" in self.session.headers:
                response = self.session.delete(f"{self.base_url}/v1/session", timeout=self.timeout)
                response.raise_for_status()
        except requests.RequestException:
            console.print("[yellow]Could not end the legacy API session; it will expire.[/yellow]")
        finally:
            self.session.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discover seed disks and assess reported journal storage.")
    parser.add_argument("--auth-mode", choices=("oauth", "legacy"),
                        default=config.get_env("ZVM_AUTH_MODE", "oauth"))
    parser.add_argument("--port", type=int, default=config.get_env("ZVM_PORT") or None)
    parser.add_argument("--insecure", action="store_true", help="Bypass TLS certificate verification.")
    parser.add_argument("--threshold", type=float,
                        default=config.get_env("JOURNAL_THRESHOLD_PERCENT", "30"),
                        help="Context-only journal-used / replica-used threshold (default: 30 percent).")
    parser.add_argument("--units", choices=("MiB", "GiB"), default="GiB")
    parser.add_argument("--show-volume-details", action="store_true",
                        help="Print the optional replica and journal VMDK table.")
    parser.add_argument("--assessment-file", default=config.get_env("ASSESSMENT_FILE", "zerto_seed_assessment.json"))
    parser.add_argument("--timeout", type=float, default=30, help="HTTP timeout in seconds (default: 30).")
    args = parser.parse_args()
    if args.auth_mode not in {"oauth", "legacy"}:
        parser.error("ZVM_AUTH_MODE must be oauth or legacy")
    if not math.isfinite(args.threshold) or args.threshold < 0:
        parser.error("threshold must be a finite, nonnegative percentage")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("timeout must be finite and positive")
    if args.port is None:
        args.port = 443 if args.auth_mode == "oauth" else 9669
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    return args


def print_assessment(report: dict, units: str, *, show_volume_details: bool = False) -> None:
    divisor = 1024 ** (2 if units == "MiB" else 3)

    def size(value):
        return "UNKNOWN" if value is None else f"{value / divisor:,.2f}"

    if show_volume_details:
        disks = Table(title=f"Replica and journal VMDKs ({units})", header_style="bold magenta")
        for label in ("VPG", "VM", "Type", "Current VMDK path"):
            disks.add_column(label, overflow="fold")
        disks.add_column("Used / prov.", justify="right", overflow="fold")
        disks.add_column("Attribution", overflow="fold")
        for row in report.get("volume_details", []):
            disks.add_row(
                row["vpg_name"], row["vm_name"], row["volume_type"],
                row["vmdk_path"] or row["vmdk_filename"] or "UNKNOWN",
                f"{size(row['used_bytes'])} / {size(row['provisioned_bytes'])}",
                row["attribution_status"],
            )
        console.print(disks)

    for group, title in (("vms", "Per-VM seed assessment"), ("vpgs", "Per-VPG seed assessment")):
        table = Table(title=f"{title} ({units})", header_style="bold magenta")
        table.add_column("VPG", overflow="fold")
        table.add_column("VM" if group == "vms" else "VMs / Above / ?", overflow="fold")
        for label in ("Journal\nused / prov.", "Replica\nused / prov.", "J/R %"):
            table.add_column(label, justify="right", overflow="fold")
        table.add_column("Journal ratio flag", overflow="fold")
        for row in report[group]:
            percent = row["journal_replica_used_percent"]
            detail = (row["vm_name"] if group == "vms" else
                      f"{row['vm_count']} / {row['above_threshold_vm_count']} / {row['unknown_vm_count']}")
            table.add_row(row["vpg_name"], detail,
                          f"{size(row['journal_used_bytes'])} / {size(row['journal_provisioned_bytes'])}",
                          f"{size(row['replica_used_bytes'])} / {size(row['replica_provisioned_bytes'])}",
                          "UNKNOWN" if percent is None else f"{percent:.2f}", row["journal_ratio_flag"])
        console.print(table)
    console.print(f"J/R = journal used / replica used. Threshold: {report['threshold_percent']:g}% (context only).")
    console.print("Journal storage is not unique changed bytes; compression and repeated writes affect the ratio.")
    console.print("Copied replicas omit newer journal writes. Also consider copy time, disk reads, network speed, and ongoing writes.")
    console.print("UNKNOWN = incomplete/ambiguous data. VPG totals can mask individual VMs; Above / ? counts highlight them.")
    affected = sum(bool(row["issues"]) for row in report["vms"])
    if affected or report["warnings"]:
        console.print(f"[yellow]Data quality: {affected} VM(s) with issues; {len(report['warnings'])} attribution warning(s). See assessment JSON.[/yellow]")
    console.print("Flags do not predict the faster method. Compare seed-copy, seeded-sync, fresh-sync times and bytes transferred.")
    console.print("Without comparable benchmarks, treat thresholds as context only. See seed-assessment.md before selecting VPGs.")


def main():
    args = parse_args()
    manifest_path = Path(config.MANIFEST_FILE)
    assessment_path = Path(args.assessment_file)
    if manifest_path.resolve() == assessment_path.resolve():
        raise ValueError("Manifest and assessment must use different output files")
    host = config.require("ZVM_IP")["ZVM_IP"]
    verify = config.VERIFY_SSL and not args.insecure
    if not verify:
        requests.packages.urllib3.disable_warnings()
    console.rule("[bold cyan]Zerto Seed Discovery & Journal Assessment")
    if not verify:
        console.print("[yellow]TLS certificate verification disabled.[/yellow]")
    client = ZertoClient(host, args.port, args.auth_mode, verify, args.timeout)
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        with console.status("Authenticating and querying VRAs, VMs, and volumes..."):
            client.authenticate()
            vras_data = client.get_list("vras")
            vms_data = client.get_list("vms")
            # One unfiltered collection supplies both recovery and journal metrics.
            volumes_data = client.get_list("volumes")
    finally:
        client.close()
    report = assess(vms_data, volumes_data, args.threshold)
    report.update({"schema_version": 3, "collection_started_at": started_at,
                   "collection_finished_at": datetime.now(timezone.utc).isoformat(),
                   "threshold_percent": args.threshold, "byte_units": "bytes",
                   "ratio_basis": "journal_used_bytes / replica_used_bytes * 100",
                   "interpretation": "Context-only journal ratio flags; not unique changed bytes, a transfer estimate, or a migration-method recommendation. Comparable workload benchmarks are required to support a performance decision."})
    print_assessment(report, args.units, show_volume_details=args.show_volume_details)

    # 3. Build Lookup Maps
    host_to_vra = {}
    for vra in vras_data:
        h_id = get_val(vra, "hostIdentifier")
        h_name = get_val(vra, "hostDisplayName")
        vra_name = get_val(vra, "vraName", "Unknown_VRA")
        if h_id:
            host_to_vra[h_id] = vra_name
        if h_name:
            host_to_vra[h_name] = vra_name

    vm_map = {}
    for vm in vms_data:
        vm_id = vm_identifier(vm)
        if vm_id:
            vm_map.setdefault(vm_id, []).append({
                "vm_name": get_val(vm, "vmName", "Unknown_VM"),
                "vpg_name": get_val(vm, "vpgName", "Unknown_VPG"),
                "vpg_identifier": get_val(vm, "vpgIdentifier"),
                "recovery_host_id": get_val(vm, "recoveryHostIdentifier"),
                "recovery_host_name": get_val(vm, "recoveryHostName"),
            })

    # 4. Process Recovery Volumes & Format Output
    manifest = []
    table = Table(
        title="Discovered Zerto Seed Replicas",
        show_lines=True,
        header_style="bold magenta",
    )
    table.add_column("VPG", style="cyan", no_wrap=True)
    table.add_column("VM Name", style="green")
    table.add_column("Target Datastore", style="yellow")
    table.add_column("Current Path (vCenter Display)", style="dim white")
    table.add_column("Destination Seed Path (vSphere Spec)", style="bold blue")

    for vol in volumes_data:
        if volume_type(vol) == "recovery":
            prot_vm = (
                get_val(vol, "protectedVm") or get_val(vol, "owningVm") or {}
            )
            vm_id = get_val(prot_vm, "identifier")
            candidates = vm_map.get(vm_id, [])
            volume_vpg = get_val(vol, "vpg", {})
            if len(candidates) > 1:
                vpg_id = get_val(volume_vpg, "identifier")
                vpg_name = get_val(volume_vpg, "name")
                candidates = [vm for vm in candidates if
                              (vpg_id and vm["vpg_identifier"] == vpg_id) or
                              (not vpg_id and vpg_name and vm["vpg_name"] == vpg_name)]
            vm_info = candidates[0] if len(candidates) == 1 else {}

            vm_name = vm_info.get("vm_name") or get_val(
                prot_vm, "name", "Unknown_VM"
            )
            vpg_name = vm_info.get("vpg_name") or get_val(
                get_val(vol, "vpg"), "name", "Unknown_VPG"
            )

            ds = get_val(vol, "datastore") or {}
            ds_name = get_val(ds, "name", "Unknown_DS")
            path = get_val(vol, "path") or {}
            raw_full = get_val(path, "full", "")
            raw_rel = get_val(path, "relative", "")
            vmdk_file = get_val(path, "fileName", "")

            # Ensure raw_full starts with [datastore]
            if raw_full and not raw_full.startswith("["):
                raw_full = f"[{ds_name}] {raw_full}"

            # Resolve VRA Appliance Name for Display
            rec_host_id = vm_info.get("recovery_host_id")
            rec_host_name = vm_info.get("recovery_host_name")
            vra_name = (
                host_to_vra.get(rec_host_id)
                or host_to_vra.get(rec_host_name)
                or "Z-VRA"
            )

            vsan_uuid = raw_rel.split("/")[0] if "/" in raw_rel else ""
            ui_display_path = (
                raw_full.replace(vsan_uuid, vra_name)
                if vsan_uuid
                else raw_full
            )

            # Strictly formatted vSphere seed path: [Datastore] VM_Name/VMDK_File
            seed_path = f"[{ds_name}] {vm_name}/{vmdk_file}"

            table.add_row(
                vpg_name, vm_name, ds_name, ui_display_path, seed_path
            )

            manifest.append(
                {
                    "vpg_name": vpg_name,
                    "vm_name": vm_name,
                    "datastore": ds_name,
                    "source_raw_path": raw_full,
                    "ui_display_path": ui_display_path,
                    "destination_seed_path": seed_path,
                }
            )

    console.print(table)

    # 5. Export Manifest JSON
    with manifest_path.open("w") as f:
        json.dump(manifest, f, indent=4)

    with assessment_path.open("w") as f:
        json.dump(report, f, indent=4)

    console.print(
        f"\n[bold green]✔ Manifest exported successfully to:[/bold green] [bold white]{manifest_path}[/bold white]"
    )
    console.print(
        f"Assessment saved to {assessment_path}. Review storage context before selecting or unprotecting any VPG.\n"
        "For selected workloads only: keep target disks when unprotecting, then run scripts 02–03."
    )


if __name__ == "__main__":
    try:
        main()
    except config.ConfigError as exc:
        config.fail(str(exc))
    except requests.RequestException as exc:
        status = exc.response.status_code if exc.response is not None else "unavailable"
        console.print(f"[red]API request failed ({type(exc).__name__}, HTTP {status}). "
                      "Check host, port, credentials, API permissions, TLS, and connectivity.[/red]")
        sys.exit(1)
    except (ValueError, OSError) as exc:
        console.print(f"Discovery failed: {exc}", style="red", markup=False)
        sys.exit(1)
