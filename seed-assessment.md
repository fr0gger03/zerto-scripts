# Journal storage and seed suitability

Script 01 helps decide whether to copy Zerto recovery disks with scripts 02–03 or
start vSphere Replication with a fresh initial sync. Its recommendations are
**storage screening heuristics**, not transfer estimates or measured completion times.

## Reading the report

Script 01 prints per-VM and per-VPG tables alongside the existing disk inventory.
With `--show-volume-details`, the optional **Replica and journal VMDKs** table lists each current disk path, its VM/VPG,
and its used/provisioned sizes in the selected units. Multiple journal volumes
appear as separate rows: their sizes add up to the VM's journal totals. These are
current Zerto disk paths, not the destination paths created by script 02.

| Column | Meaning |
| --- | --- |
| Journal used | Sum of reported `Size.UsedInBytes` for the VM's journal volumes |
| Journal prov. | Sum of `Size.ProvisionedInBytes` for those volumes; not the configured journal hard limit |
| Replica used / prov. | Corresponding sums for recovery volumes; excludes journal, protected/source, and scratch volumes |
| J/R % | `journal used bytes / replica used bytes × 100` |
| Recommendation | Policy outcome using the configured threshold |

The default threshold is **30%**: above it produces `FULL SYNC RECOMMENDED`;
at or below it produces `SEED CANDIDATE`. The comparison uses unrounded values.
A displayed 30.00% can therefore exceed the threshold slightly.

`UNKNOWN` means the measurements or attribution are insufficient. Missing volumes,
missing/invalid byte fields, duplicate records, and a zero replica denominator
prevent a recommendation. A measured zero journal usage is different from missing
journal volumes. If any volume cannot be attributed to exactly one inventory VM/VPG,
all recommendations become UNKNOWN: silently excluding its bytes could mislead.
Complete individual metrics remain visible; incomplete totals are null in JSON.

VPG ratios use **summed bytes**, not averaged VM percentages. `VMs / Full / ?`
shows total VMs, VMs above the threshold, and VMs with unknown assessments. Review
individual VMs even when the VPG aggregate is a seed candidate. Identifiers, rather
than VM display names, distinguish workloads; VPG identifiers distinguish multiple
protection relationships for the same VM.

## What journal usage tells you

Each VM has a journal comprising one or more volumes. Recent writes and checkpoints
remain there; older writes are promoted to recovery disks as history expires.
Copying the base recovery VMDKs alone omits the newer writes still in the journal.
Scripts 02–03 copy disks and clean descriptor references; they do not replay journals
or produce a current, recoverable VM image. vSphere Replication must reconcile seeds
against the source.

Occupied journal bytes are **not precise unique changed blocks**. Repeated writes,
journal compression, and storage allocation affect the relationship. Zerto also
distinguishes hypervisor-reported occupied storage from VRA internal allocations.
Replica used bytes measure reported storage allocation, not guest filesystem usage;
thick provisioning can make the denominator especially misleading. Provisioned
capacity is displayed separately and is never silently substituted for used bytes.
The report is not an upper or lower bound on transfer size.

The 30% policy is configurable and is not a Zerto or VMware performance guarantee.
Consider available bandwidth, source/target read and checksum speed, server-side
copy duration, ongoing writes, and the time between discovery and starting the new
replication. Re-run discovery near the decision point. Collection spans several API
calls, not an atomic snapshot. The endpoints used do not provide a reliable journal
history metric across versions; check configured and actual history in Zerto when
interpreting the footprint.

## Usage and authentication

```bash
uv run 01_zerto_discover_and_export.py
uv run 01_zerto_discover_and_export.py --threshold 40 --units MiB
uv run 01_zerto_discover_and_export.py --show-volume-details --units MiB
uv run 01_zerto_discover_and_export.py --auth-mode legacy --port 9669
```

Appliances default to OAuth client credentials on port 443, retaining the existing
Keycloak flow. Legacy mode uses `ZVM_USER` / `ZVM_PASSWORD`, Basic authentication to
`POST /v1/session/add` with `AuthenticationMethod: 1`, and the returned
`x-zerto-session` header for subsequent calls. Legacy sessions are ended on exit.
Override `ZVM_PORT` or `--port` for your deployment; authentication mode, not version
number alone, determines the default. Credentials remain in environment variables.

Set `VERIFY_SSL=true` for certificate verification. The existing default remains
false; `--insecure` explicitly disables verification for this invocation. Each HTTP
request has a 30-second timeout, adjustable with `--timeout`.

Script 01 reads `/v1/vms`, `/v1/vras`, and an unfiltered `/v1/volumes` collection so
recovery and journal records come from the same response. It handles capitalization
variants and textual/numeric volume types (`Recovery`/1, `Journal`/2). Journal
attribution uses `ProtectedVm.Identifier`, never a VRA's `OwningVm` identifier.
Confirm endpoint availability and permissions in your deployed 9.x/10.x Swagger;
this change has offline tests, not certification against every Zerto release.

## Outputs and workflow

- `zerto_seeds_manifest.json`: unchanged disk-list format consumed by scripts 02–03.
- `zerto_seed_assessment.json`: raw byte totals, VM/VPG identifiers, ratios,
  recommendations, data-quality issues, threshold, and UTC collection timestamps.
  Disk details are always exported, even without `--show-volume-details`.
  Schema version 2 adds `volume_details`, the disk table as structured JSON rows:
  VM/VPG identifiers and names, `volume_type` (`replica` or `journal`), datastore,
  VMDK path/filename, and `used_bytes` / `provisioned_bytes`. The terminal formats
  these byte values as MiB or GiB. Missing sizes or paths remain null, not zero.
  `attribution_status` is `matched`, `duplicate`, or `unassigned`;
  `included_in_totals` identifies the records used in aggregation. Duplicate and
  unassigned records remain visible for troubleshooting but do not add to totals.
  A matched disk with a missing byte metric makes that aggregate metric unknown.

`MANIFEST_FILE` and `ASSESSMENT_FILE` override the defaults; `--assessment-file`
overrides the assessment path. Paths are relative to the working directory.
Display units are GiB (`bytes / 1024³`) or MiB (`bytes / 1024²`); JSON retains bytes.
Default artifacts are git-ignored; exclude custom output names too.

Recommendations **never filter the manifest**. Before unprotecting workloads,
review the report and retain only the selected disk entries in the manifest used
by scripts 02–03. Those scripts still process every entry supplied. Keep target
disks when unprotecting, then copy and preview descriptor cleanup as documented in
the [README](README.md#step-by-step-usage).

## Verification and references

Run offline tests without contacting Zerto or vCenter:

```bash
uv run python -m unittest discover -s tests -v
```

- [Zerto journal overview, sizing, and best practice](https://help.zerto.com/bundle/BP.Journal.Sizing.HTML/page/Journal_Overview_Sizing_and_Best_Practice.htm)
- [Accessible earlier edition of the journal guide](https://s3.amazonaws.com/zertodownload_docs/7.5_Latest/Zerto%20Virtual%20Replication%20Zerto%20Cloud%20Manager%20%28ZCM%29%20Online%20Help/content/journal_overview_and_best_practice/journal_overview_sizing_and_best_practice.htm) — describes promotion, compression, and allocation behavior; numeric defaults can differ by release.
- [Zerto 10.9 resources report](https://support.hpe.com/hpesc/public/docDisplay?docId=Admin.VC.HTML.10.9_en_us&docLocale=en_US&page=Resources_Report.html) — explains reported journal storage versus internal allocations.
- [Zerto appliance API quickstart](https://github.com/ZertoPublic/zerto-api-quickstart) — bearer authentication examples.
