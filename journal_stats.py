"""Pure aggregation helpers for the advisory seed assessment."""

from collections import defaultdict


METRICS = ("journal_used_bytes", "journal_provisioned_bytes",
           "replica_used_bytes", "replica_provisioned_bytes")


def get_val(obj, key, default=None):
    """Read an API field irrespective of capitalization."""
    if not isinstance(obj, dict):
        return default
    return next((v for k, v in obj.items() if k.casefold() == key.casefold()), default)


def volume_type(volume) -> str:
    """Normalize documented textual and numeric volume types."""
    raw = str(get_val(volume, "volumeType", "")).casefold()
    return {"0": "protected", "1": "recovery", "2": "journal"}.get(raw, raw)


def vm_identifier(vm: dict) -> str | None:
    """Read the protected VM identity from supported inventory shapes."""
    return (get_val(vm, "vmIdentifier") or get_val(get_val(vm, "link"), "identifier")
            or get_val(vm, "identifier"))


def byte_value(value) -> int | None:
    """Reject absent, negative, fractional, and boolean byte measurements."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return int(value)
    return None


def recommendation(used: int | None, replica: int | None, threshold: float) -> tuple[float | None, str]:
    """Return the unrounded storage ratio and configured screening outcome."""
    if used is None or replica is None or replica <= 0:
        return None, "UNKNOWN"
    ratio = used / replica * 100
    return ratio, ("FULL SYNC RECOMMENDED" if ratio > threshold else "SEED CANDIDATE")


def volume_detail(volume: dict, kind: str) -> dict:
    """Retain the disk identity and measurements behind the aggregate totals."""
    protected_vm = get_val(volume, "protectedVm", {})
    vpg = get_val(volume, "vpg", {})
    datastore = get_val(volume, "datastore", {})
    path = get_val(volume, "path", {})
    size = get_val(volume, "size", {})
    datastore_name = get_val(datastore, "name")
    disk_path = get_val(path, "full") or get_val(path, "relative")
    if disk_path and datastore_name and not disk_path.startswith("["):
        disk_path = f"[{datastore_name}] {disk_path}"
    return {
        "vm_identifier": get_val(protected_vm, "identifier"),
        "vm_name": get_val(protected_vm, "name") or "Unknown_VM",
        "vpg_identifier": get_val(vpg, "identifier"),
        "vpg_name": get_val(vpg, "name") or "Unknown_VPG",
        "volume_type": "replica" if kind == "recovery" else "journal",
        "datastore": datastore_name,
        "vmdk_path": disk_path,
        "vmdk_filename": get_val(path, "fileName"),
        "used_bytes": byte_value(get_val(size, "usedInBytes")),
        "provisioned_bytes": byte_value(get_val(size, "provisionedInBytes")),
        "included_in_totals": False,
        "attribution_status": "unassigned",
    }


def assess(vms: list, volumes: list, threshold: float) -> dict:
    """Aggregate complete metrics by VM/VPG; never substitute zero for missing data.

    VM identifiers can recur across VPGs. Volume membership must resolve to
    exactly one inventory entry; ambiguous or unassigned data invalidates
    recommendations rather than silently understating totals.
    """
    rows = {}
    warnings = []
    for index, vm in enumerate(vms):
        identifier = vm_identifier(vm)
        vpg_id = get_val(vm, "vpgIdentifier")
        vpg_name = get_val(vm, "vpgName")
        key = (identifier or f"missing:{index}", vpg_id or vpg_name)
        rows.setdefault(key, {
            "vm_identifier": identifier, "vm_name": get_val(vm, "vmName", "Unknown_VM"),
            "vpg_identifier": vpg_id, "vpg_name": vpg_name or "Unknown_VPG",
            "issues": [] if identifier else ["Missing VM identifier"],
            "journal": [], "replica": [],
        })

    by_vm = defaultdict(list)
    for row in rows.values():
        by_vm[row["vm_identifier"]].append(row)

    seen = set()
    volume_details = []
    unassigned = False
    for volume in volumes:
        kind = volume_type(volume)
        if kind not in {"journal", "recovery"}:
            continue
        detail = volume_detail(volume, kind)
        volume_details.append(detail)
        # OwningVm can identify a VRA; never use it to attribute journal usage.
        identifier = get_val(get_val(volume, "protectedVm"), "identifier")
        vpg = get_val(volume, "vpg", {})
        vpg_id, vpg_name = get_val(vpg, "identifier"), get_val(vpg, "name")
        candidates = by_vm.get(identifier, []) if identifier else []
        if vpg_id:
            candidates = [r for r in candidates if r["vpg_identifier"] == vpg_id
                          or (not r["vpg_identifier"] and vpg_name and r["vpg_name"] == vpg_name)]
        elif vpg_name:
            candidates = [r for r in candidates if r["vpg_name"] == vpg_name]
        if len(candidates) != 1:
            unassigned = True
            warnings.append(f"Unassigned or ambiguous {kind} volume for VM {identifier or '(missing ID)'}")
            continue
        row = candidates[0]
        if not row["vpg_identifier"] and vpg_id:
            row["vpg_identifier"] = vpg_id
        if row["vpg_name"] == "Unknown_VPG" and vpg_name:
            row["vpg_name"] = vpg_name
        for field in ("vm_identifier", "vm_name", "vpg_identifier", "vpg_name"):
            detail[field] = row[field]
        detail["attribution_status"] = "matched"
        path = get_val(get_val(volume, "path"), "full")
        ds = get_val(volume, "datastore", {})
        # A repeated physical volume must not inflate the assessment.
        fingerprint = (kind, identifier, row["vpg_identifier"] or row["vpg_name"],
                       get_val(ds, "identifier") or get_val(ds, "name"), path)
        if path and fingerprint in seen:
            row["issues"].append("Duplicate volume record; verify API inventory")
            detail["attribution_status"] = "duplicate"
            continue
        if path:
            seen.add(fingerprint)
        detail["included_in_totals"] = True
        row["journal" if kind == "journal" else "replica"].append(get_val(volume, "size", {}))

    vm_rows = []
    for row in rows.values():
        for prefix in ("journal", "replica"):
            sizes = row.pop(prefix)
            row[f"{prefix}_volume_count"] = len(sizes)
            if not sizes:
                row["issues"].append(f"No {prefix} volumes returned")
            for field, api_field in (("used", "usedInBytes"), ("provisioned", "provisionedInBytes")):
                values = [byte_value(get_val(s, api_field)) for s in sizes]
                total = sum(values) if values and all(v is not None for v in values) else None
                row[f"{prefix}_{field}_bytes"] = total
                if sizes and total is None:
                    row["issues"].append(f"Incomplete {prefix} {field} bytes")
        row["journal_replica_used_percent"], row["recommendation"] = recommendation(
            row["journal_used_bytes"], row["replica_used_bytes"], threshold)
        if unassigned or row["issues"]:
            row["recommendation"] = "UNKNOWN"
        if not row["vpg_identifier"] and row["vpg_name"] == "Unknown_VPG":
            row["issues"].append("Missing VPG identity")
            row["recommendation"] = "UNKNOWN"
        vm_rows.append(row)

    groups = defaultdict(list)
    for row in vm_rows:
        groups[row["vpg_identifier"] or ("name", row["vpg_name"])].append(row)
    vpg_rows = []
    for members in groups.values():
        identifier, name = members[0]["vpg_identifier"], members[0]["vpg_name"]
        row = {"vpg_identifier": identifier, "vpg_name": name, "vm_count": len(members),
               "unknown_vm_count": sum(m["recommendation"] == "UNKNOWN" for m in members),
               "full_sync_vm_count": sum(m["recommendation"] == "FULL SYNC RECOMMENDED" for m in members)}
        for metric in METRICS:
            values = [m[metric] for m in members]
            row[metric] = sum(values) if all(v is not None for v in values) else None
        row["journal_replica_used_percent"], row["recommendation"] = recommendation(
            row["journal_used_bytes"], row["replica_used_bytes"], threshold)
        if row["unknown_vm_count"]:
            row["recommendation"] = "UNKNOWN"
        vpg_rows.append(row)
    volume_details.sort(key=lambda disk: (
        disk["vpg_name"], disk["vpg_identifier"] or "", disk["vm_name"],
        disk["vm_identifier"] or "", disk["volume_type"] != "replica",
        disk["vmdk_path"] or "",
    ))
    return {"vms": vm_rows, "vpgs": vpg_rows, "volume_details": volume_details,
            "warnings": sorted(set(warnings))}
