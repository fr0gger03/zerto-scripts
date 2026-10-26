# Journal storage and seed suitability

Script 01 provides storage context when evaluating Zerto recovery disks as seeds
for vSphere Replication. It does **not recommend a synchronization method** or
predict which method is faster. Journal ratio flags describe measurements relative
to a configured threshold; they are not transfer estimates or completion times.

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
| Journal ratio flag | Context-only comparison with the configured threshold |

The default threshold is **30%**: above it produces `ABOVE THRESHOLD`;
at or below it produces `WITHIN THRESHOLD`. Neither flag selects a migration method. The comparison uses unrounded values.
A displayed 30.00% can therefore exceed the threshold slightly.

`UNKNOWN` means the measurements or attribution are insufficient. Missing volumes,
missing/invalid byte fields, duplicate records, and a zero replica denominator
prevent a threshold classification. A measured zero journal usage is different from missing
journal volumes. If any volume cannot be attributed to exactly one inventory VM/VPG,
all flags become UNKNOWN: silently excluding its bytes could mislead.
Complete individual metrics remain visible; incomplete totals are null in JSON.

VPG ratios use **summed bytes**, not averaged VM percentages. `VMs / Above / ?`
shows total VMs, VMs above the threshold, and VMs with unknown assessments. Review
individual VMs even when the VPG aggregate is within the threshold. Identifiers, rather
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

The 30% threshold is configurable and is not a Zerto or VMware performance crossover.
Consider available bandwidth, source/target read and checksum speed, server-side
copy duration, ongoing writes, and the time between discovery and starting the new
replication. Re-run discovery near the decision point. Collection spans several API
calls, not an atomic snapshot. The endpoints used do not provide a reliable journal
history metric across versions; check configured and actual history in Zerto when
interpreting the footprint.

## Benchmark before recommending a method

**If the customer has not already captured comparable workload measurements,
thresholds and any recommendations derived from them are for context only.**
They must be examined against the customer's environment, not treated as evidence
that seeding or fresh synchronization will finish sooner. A low ratio does not
prove seeding is worthwhile; a high ratio does not prove fresh sync is preferable.

Before making a performance recommendation, capture:

- Seed-copy time, including preparation and descriptor cleanup.
- Seeded synchronization time, including the comparison/checksum phase.
- Fresh initial synchronization time.
- Bytes transferred for each method, using the same measurement boundary.

Compare representative workloads under similar disk sizes and contents, source
write rates, storage load, network bandwidth, compression settings, concurrency,
and elapsed time between seed capture and synchronization. Record both end-to-end
time and network traffic: the fastest method and the method using the least WAN
bandwidth need not be the same. Document differences between trials before applying
results to other workloads.

This tool does not capture those performance benchmarks or verify that they exist.
Even when the customer has them, the flags remain storage comparisons; the
administrator must interpret the benchmarks alongside operational constraints and
validate any environment-specific threshold. No threshold automatically selects or
excludes VPGs in script 02.

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

- `zerto_seeds_manifest.json`: full discovery disk list, used as input by script 02.
- `zerto_migration_selection.json`: selected VPGs/disks and per-disk copy status,
  shared by scripts 02–03. `MIGRATION_FILE` or `--migration-file` sets its path.
- `zerto_seed_assessment.json`: raw byte totals, VM/VPG identifiers, ratios,
  journal ratio flags, data-quality issues, threshold, and UTC collection timestamps.
  Disk details are always exported, even without `--show-volume-details`.
  Schema version 2 adds `volume_details`, the disk table as structured JSON rows:
  VM/VPG identifiers and names, `volume_type` (`replica` or `journal`), datastore,
  VMDK path/filename, and `used_bytes` / `provisioned_bytes`. The terminal formats
  these byte values as MiB or GiB. Missing sizes or paths remain null, not zero.
  `attribution_status` is `matched`, `duplicate`, or `unassigned`;
  `included_in_totals` identifies the records used in aggregation. Duplicate and
  unassigned records remain visible for troubleshooting but do not add to totals.
  A matched disk with a missing byte metric makes that aggregate metric unknown.

Schema version 3 replaces the previous `recommendation` field with
`journal_ratio_flag` and `full_sync_vm_count` with `above_threshold_vm_count`.
The supported flags are `ABOVE THRESHOLD`, `WITHIN THRESHOLD`, and `UNKNOWN`.
Existing reports retain their old labels until script 01 is rerun; consumers of
the JSON should check `schema_version` and update their field references.

`MANIFEST_FILE` and `ASSESSMENT_FILE` override the defaults; `--assessment-file`
overrides the assessment path. Paths are relative to the working directory.
Display units are GiB (`bytes / 1024³`) or MiB (`bytes / 1024²`); JSON retains bytes.
Default artifacts are git-ignored; exclude custom output names too.

Journal ratio flags **never automatically select VPGs**. Use script 02's Questionary
menu to keep all VPGs or choose a subset. All disks in each chosen VPG are retained;
VPG identifiers are used when present, otherwise VPG names. With existing discovery
files, VPG names must distinguish the groups you want to select.

Use `uv run 02_vcf_seed_copy.py --select-only` to save the selection before
unprotecting. Keep target disks for the chosen VPGs, then run script 02 with
`--reuse-selection`. Script 03 reads the same migration JSON and only cleans disks
marked `copied`. Selection does not delete VPGs or release Zerto locks.

The migration JSON contains `schema_version`, `created_at`, `source_manifest`,
`selected_vpgs`, and `disks`. Each disk keeps its discovery fields plus a
`copy_status`: `pending`, `copied`, `skipped`, or `failed`. Copy results are saved
atomically after each disk. Reusing the file retries non-completed disks; it does
not overwrite existing destination VMDKs. If execution stops after a vCenter copy
finishes but before its result is saved, verify the destination before retrying.
A fresh selection resets copy statuses and asks before replacing the existing file.
Keep this file for script 03, including when running cleanup with `--dry-run`.
See the [README](README.md#step-by-step-usage) for the complete sequence.

## Verification and references

Run offline tests without contacting Zerto or vCenter:

```bash
uv run python -m unittest discover -s tests -v
```

- [Zerto journal overview, sizing, and best practice](https://help.zerto.com/bundle/BP.Journal.Sizing.HTML/page/Journal_Overview_Sizing_and_Best_Practice.htm)
- [Accessible earlier edition of the journal guide](https://s3.amazonaws.com/zertodownload_docs/7.5_Latest/Zerto%20Virtual%20Replication%20Zerto%20Cloud%20Manager%20%28ZCM%29%20Online%20Help/content/journal_overview_and_best_practice/journal_overview_sizing_and_best_practice.htm) — describes promotion, compression, and allocation behavior; numeric defaults can differ by release.
- [Zerto 10.9 resources report](https://support.hpe.com/hpesc/public/docDisplay?docId=Admin.VC.HTML.10.9_en_us&docLocale=en_US&page=Resources_Report.html) — explains reported journal storage versus internal allocations.
- [Zerto appliance API quickstart](https://github.com/ZertoPublic/zerto-api-quickstart) — bearer authentication examples.
