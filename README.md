# Zerto to VCF Protection & Recovery Seed Migration Toolset

This repository provides an automation workflow for converting active Zerto replica VMDKs into clean, vSphere-native seed disks for **VMware Cloud Foundation (VCF) Protection & Recovery** or **vSphere Replication**.

Copying Zerto target disks into standard datastore folders (`[Datastore] VM_Name/VMDK`) can reduce initial replication traffic. Script 01 reports journal and replica storage per VM and VPG to help evaluate whether seeds are worthwhile. See [journal assessment and decision guidance](seed-assessment.md).

---

## Architecture & Workflow Overview

```
┌────────────────────────────────┐
│ 1. Active Zerto Replication    │ ──► Run 01_zerto_discover_and_export.py
└────────────────────────────────┘     Exports zerto_seeds_manifest.json
               │
               ▼
┌────────────────────────────────┐
│ 2. Unprotect / Delete VPG      │ ──► Manual Step in Zerto UI
└────────────────────────────────┘     Option: "Keep target disks" (Releases locks)
               │
               ▼
┌────────────────────────────────┐
│ 3. Clean Source & Copy Seeds   │ ──► Run 02_vcf_seed_copy.py
└────────────────────────────────┘     Comment source filter references, verify,
                                       then server-side VMDK copy on vSAN/VMFS

```

---

## Prerequisites

| Requirement | Details |
| --- | --- |
| **Python Engine** | Python 3.13+ (Managed via `uv` or standard `pip`) |
| **Python Libraries** | `requests`, `pyvmomi`, `rich`, `python-dotenv`, `questionary` |
| **Zerto Permissions** | ZVMA / ZVM Keycloak Client ID & Client Secret |
| **vCenter Permissions** | Account with `Datastore.FileManagement` privileges |

### Installation

Use the committed `requirements.txt` to install the same locked versions and
hashes on either laptop. `uv.lock` remains the authoritative resolution, but
contains corporate Artifactory download URLs; normal `uv sync` is therefore not
the portable installation path.

```bash
# Create the environment once per checkout (skip if .venv already exists).
uv venv --python 3.13

# Personal laptop: public PyPI, no corporate VPN needed.
uv pip sync --python .venv/bin/python --require-hashes requirements.txt --default-index https://pypi.org/simple

# Corporate laptop: connect the VPN first and use Artifactory.
uv pip sync --python .venv/bin/python --require-hashes requirements.txt --default-index https://packages.vcfd.broadcom.net/artifactory/api/pypi/pypi-virtual/simple
```

Run **only the sync command appropriate to the laptop**. Re-run it after pulling
dependency changes. `uv pip sync` makes `.venv` match the requirements exactly,
removing extraneous packages; it does not rewrite `uv.lock`. No activation is
required because the target interpreter is explicit. Keep any index credentials
in your existing local credential setup, not in committed files or URLs.

These commands assume no additional private indexes are configured with higher
priority. `--default-index` selects the default, but does not remove extra indexes;
keep each laptop's local uv configuration limited to its permitted sources.
Both indexes must serve artifacts matching the exported hashes. If verification
fails, investigate the mirror/package difference rather than disabling hashes.

Use **`uv run --no-sync` for all scripts and tests**, as shown below. This runs
the installed environment without automatically resolving/syncing against the
corporate URLs. It does not check for missing or outdated dependencies; install
from `requirements.txt` first. `.venv` stays local and is never copied between laptops.

#### Updating dependencies

Resolve intended dependency changes on the VPN-connected corporate laptop,
then export the lock for installation on both laptops:

```bash
# After editing dependency declarations in pyproject.toml:
uv lock
uv export --frozen --no-emit-project --output-file requirements.txt
```

Review and commit `pyproject.toml`, `uv.lock`, and `requirements.txt` together
when they change. Do not edit `requirements.txt` manually or regenerate the lock
just to switch indexes. The export uses the existing lock without contacting an
index, preserves exact versions and hashes, and omits index/download URLs.

Offline tests check that the requirements still match the lock. Exporting with
`--frozen` does not check whether `pyproject.toml` has changed, so run `uv lock`
first when actually changing dependencies. The corporate resolution step still
requires the VPN; ordinary installs here do not.

---

## Configuration

All connection details and credentials are read from environment variables loaded
from a local `.env` file. **No credentials are stored in the scripts.**

```bash
cp .env.example .env
# then edit .env with your own values

```

`.env` is listed in `.gitignore` and must never be committed. `.env.example` is the
committed template containing placeholders only.

| Variable | Used by | Description |
| --- | --- | --- |
| `ZVM_IP` | Script 1 | Zerto ZVM IP or FQDN (no scheme or port) |
| `ZVM_AUTH_MODE` / `ZVM_PORT` | Script 1 | Optional. `oauth` (443, default) or `legacy` (9669); port can be overridden |
| `ZVM_USER` / `ZVM_PASSWORD` | Script 1 | Credentials required only in legacy mode |
| `JOURNAL_THRESHOLD_PERCENT` | Script 1 | Optional. Context-only journal ratio threshold, default `30` |
| `ASSESSMENT_FILE` | Script 1 | Optional. Assessment JSON, default `zerto_seed_assessment.json` |
| `ZVM_CLIENT_ID` | Script 1 | Keycloak OAuth client ID (see [keycloak.md](keycloak.md)) |
| `ZVM_CLIENT_SECRET` | Script 1 | Keycloak OAuth client secret |
| `VCENTER_IP` | Script 2 | vCenter Server IP or FQDN (no scheme) |
| `VCENTER_USER` | Script 2 | vCenter account with `Datastore.FileManagement` |
| `VCENTER_PASSWORD` | Script 2 | vCenter account password |
| `VCENTER_DATACENTER` | Script 2 | vCenter Datacenter object name |
| `MANIFEST_FILE` | Scripts 1–2 | Optional. Discovery manifest (default `zerto_seeds_manifest.json`) |
| `MIGRATION_FILE` | Script 2 | Optional. Selected migration disks and cleanup/copy results (default `zerto_migration_selection.json`) |
| `VERIFY_SSL` | Both | Optional. `true` to enforce TLS verification (default `false` for self-signed certs) |

Environment variables exported in your shell (or injected by a secret manager
such as `op run --env-file=.env -- uv run --no-sync ...`) take precedence over the `.env` file,
so credentials never have to touch disk if you prefer.

Both scripts fail fast with a clear message listing every missing variable.

---

## Repository Structure

* `01_zerto_discover_and_export.py` — Connects to Zerto ZVMA REST API, maps protected VMs to target ESXi hosts/VRAs, displays a formatted terminal table, and generates `zerto_seeds_manifest.json`.
* `02_vcf_seed_copy.py` — Selects all VPGs or a subset with Questionary, saves `zerto_migration_selection.json`, cleans each confirmed original descriptor and verifies it before creating standard seed folders and executing server-side VMDK copies.
* `descriptor_cleanup.py` — Validates source descriptor paths/content, comments stale `ddb.iofilters` / `ddb.sidecars` assignments, and verifies writes through authenticated vCenter datastore HTTP access.
* `config.py` — Shared `.env` loader, required-variable validation, and `[Datastore] VM_Name/VMDK` path logic.
* `migration_selection.py` — Shared selection validation and migration JSON persistence.
* `requirements.txt` — Portable, hash-pinned export of `uv.lock`; installed through each laptop's permitted package index.
* `journal_stats.py` — Offline VM/VPG journal aggregation and screening logic; `tests/` contains mocked unit tests.
* `seed-assessment.md` — Interpretation, limitations, authentication options, and assessment output details.
* `.env.example` — Template for the git-ignored `.env` file.

---

## Step-by-Step Usage

### Prerequisite: Setup Zerto Keycloak client token
See [keycloak.md](keycloak.md) for instructions.

### Step 1: Discover Zerto Replicas & Export Manifest

Run Script 1 while Zerto protection is still active.

1. Set the Zerto ZVMA parameters in your `.env` file:
```bash
ZVM_IP=your.zvm.ip.or.fqdn
ZVM_CLIENT_ID=zerto-python-script
ZVM_CLIENT_SECRET=YourClientSecretHere

```


2. Run the discovery script:
```bash
uv run --no-sync 01_zerto_discover_and_export.py

```
![alt text](images/move-vmdk-01.png)

![alt text](images/move-vmdk-02.png)

3. Review the per-VM and per-VPG journal assessment before deciding to proceed.
   The default policy flags journal-used / replica-used ratios above 30% as
   `ABOVE THRESHOLD`; ratios at or below it are `WITHIN THRESHOLD`, and missing
   or ambiguous measurements are `UNKNOWN`. These flags do not recommend a
   synchronization method. Without measurements of seed-copy time, seeded-sync
   time, fresh-sync time, and bytes transferred under comparable conditions,
   thresholds and any migration recommendations are **context only** and require
   examination for the customer's environment. See the
   [decision guidance and benchmarking requirements](seed-assessment.md#benchmark-before-recommending-a-method).

```bash
# Optional: use a 40% threshold and display MiB instead of GiB
uv run --no-sync 01_zerto_discover_and_export.py --threshold 40 --units MiB
```

4. Inspect `zerto_seed_assessment.json` and `zerto_seeds_manifest.json`.
   Add `--show-volume-details` to print the optional replica/journal VMDK table.
   Paths and individual sizes are always saved in the assessment's `volume_details` rows.
   Ratio flags do not select workloads. Choose migration VPGs with script 02:

```bash
# Optional: select VPGs now, without connecting to vCenter or copying
uv run --no-sync 02_vcf_seed_copy.py --select-only
```

Choose **Keep all VPGs** or **Select a subset of VPGs**. In the checkbox list,
use arrows to navigate, Space to toggle, and Enter to accept. All disks in each
selected VPG are saved to `zerto_migration_selection.json`; discovery remains intact.
Cancel with Ctrl+C to leave existing files unchanged.

### Step 2: Release File Locks in Zerto

Before moving or copying replica files, ESXi file locks held by Zerto VRAs must be released:

1. Open the **Zerto Management Interface**.
2. Select only the VPG(s) chosen for migration and click **Delete VPG** (or remove specific VMs).
3. **CRITICAL:** Select **"Keep target disks"** when prompted.
4. Confirm deletion. Zerto will detach the VMDKs from the target VRA.

![alt text](images/zerto-delete-vpg.png)

### Step 3: Clean Original Descriptors and Copy VMDKs to VCF Seed Directories

Examine your vSphere datastore to confirm current location of replica disks


Run Script 2 after Zerto has released the file locks.

1. Set your vCenter credentials in the same `.env` file:
```bash
VCENTER_IP=your.vcenter.ip.or.fqdn
VCENTER_USER=administrator@vsphere.local
VCENTER_PASSWORD=YourVcenterPasswordHere
VCENTER_DATACENTER=Datacenter-DR

```


2. Execute the copy script to choose all VPGs or a subset and save the migration selection:
```bash
uv run --no-sync 02_vcf_seed_copy.py

```

If you already saved a selection with `--select-only`, reuse it instead:

```bash
uv run --no-sync 02_vcf_seed_copy.py --reuse-selection
```

3. Confirm that selected workloads were unprotected using **Keep target disks** and
   their locks released. This is an operator confirmation, not an automatic lock check.
   The script warns that original descriptors will be modified without separate backups.
4. Confirm each selected disk. The script reads its **original** descriptor, comments
   stale `ddb.iofilters` / `ddb.sidecars` assignments, and reads back any write to verify
   the exact content. Only then does it create `[Datastore] VM_Name` and initiate a
   `VirtualDiskManager` copy task. Already-clean descriptors need no write.

Only disks in the Questionary selection (or its saved migration JSON) are eligible.
Declined and already-copied disks are never cleaned. All disks in a selected VPG are
included, subject to per-disk confirmation.

Preview source edits and intended copies with no remote writes:

```bash
uv run --no-sync 02_vcf_seed_copy.py --reuse-selection --dry-run
```

Dry run still connects to vCenter and reads descriptors, but never uploads, creates
folders, starts copy tasks, or updates saved results. Without `--reuse-selection`,
it asks for a new selection and saves that selection locally. `--select-only`
remains the offline selection mode and cannot be combined with `--dry-run`.

Cleanup and copy results are saved per disk. `--reuse-selection` skips completed disks
and offers pending, skipped, or failed disks again. A new selection asks before
replacing an existing migration JSON. Use `--migration-file PATH` on script 2,
or set `MIGRATION_FILE` in `.env`, to use a different selection file.

![alt text](images/move-vmdk-04.png)

### Source Descriptor Cleanup Details

If the original Zerto replica had an I/O filter attached (e.g. `hpereplication`), the
copied seed disk's descriptor can retain `ddb.iofilters` / `ddb.sidecars` lines
that reference sidecar files no longer present on the target. Left in place,
these can cause the eventual VM to fail to power on with an error like
`Cannot open the disk '...' or one of the snapshot disks it depends on.`
See [Broadcom KB 334555](https://knowledge.broadcom.com/external/article/334555/unable-to-power-on-vm-when-iofilters-is.html).

``` bash
# Example disk descriptor file containing references to hepreplicationfilter

cat /vmfs/volumes/consolidated-sandbox-cl01-ds-vsan01/ubuntu01-src.yellow.set.lab/ub
untu01-src.yellow.set.lab.vmdk 

# Disk DescriptorFile
version=5
encoding="UTF-8"
CID=fffffffe
parentCID=ffffffff
createType="vmfs"
# Extent description
RW 104857600 VMFS "vsan://522575f1cd03a467-4c173d347cbd724c/ce847f6a-aa67-4210-2c4d-0c42a1ed6c18"
# The Disk Data Base 
#DDB
ddb.adapterType = "buslogic"
ddb.deletable = "true"
ddb.geometry.cylinders = "6527"
ddb.geometry.heads = "255"
ddb.geometry.sectors = "63"
# ********** Note the presence of hpereplication ************
ddb.iofilters = "hpereplication"
ddb.longContentID = "fee616dd47a9c48c76eae093fffffffe"
# ********** Note the dependency for the sidecar files ************
ddb.sidecars = "hpereplication_2,ubuntu01-src.yellow.set.lab-b7276352f854ae50.vmfd;hpereplication_1,ubuntu01-src.yellow.set.lab-2cb493a4ea28a9a0.vmfd"
ddb.uuid = "60 00 C2 9c 2b 8f 65 f2-ea d3 24 68 6e be e4 81"
ddb.virtualHWVersion = "14"


```

Cleanup is integrated into Script 2 **before copying**; there is no separate Script 3.
It reuses the authenticated `pyVmomi` session for vCenter datastore HTTP access,
targeting the actual source path, not the destination VM folder or display path.
Only small UTF-8 text descriptors (up to 1 MiB) are accepted. Data extent paths,
binary content, and malformed descriptors are rejected; HTTP requests have
30-second timeouts. The large `-flat.vmdk` extent and vSAN data objects are never edited.

Matching assignments are commented out (`#ddb.iofilters = ...`), not deleted,
per the KB's guidance. Existing comments, unrelated descriptor content, extent
references, and LF/CRLF line endings are preserved. Re-running cleanup is idempotent.
**No separate backup is made, and the original descriptor is no longer untouched.**
If copying fails after cleanup, the source remains cleaned.

If fetching, validating, writing, or verifying a descriptor fails, Script 2 marks
that disk `failed`, records `cleanup_status: failed` and `cleanup_error` in the
migration JSON, does **not** copy it, and continues with other selected disks.
Successful cleanup records `cleanup_status: cleaned` (or `clean` if no edit was needed)
separately from `copy_status`. Retry with `--reuse-selection`; source descriptors
are rechecked rather than trusting old cleanup results. Existing schema-version-1
migration selections without cleanup metadata still work.

Disks already marked `copied` remain skipped, including copies made by older
versions. They are not retroactively repaired by this workflow; inspect existing
copies separately before using them as seeds.

---

## Important Technical Notes

* **Credential Handling:** Credentials live only in the git-ignored `.env` file (or your shell/secret manager). If you ever committed real credentials to this repository, rotate the Zerto Keycloak client secret and the vCenter account password — removing them from the working tree does not remove them from git history.
* **Server-Side Processing:** All VMDK copy operations take place 100% server-side within the vSphere ESXi storage stack. Large file transfers do **not** route over your local workstation or VPN connection.
* **vSAN Compatibility:** Script 2 automatically handles datastore bracket formatting (`[DatastoreName]`) and utilizes `VirtualDiskManager.CopyVirtualDisk_Task` to safely duplicate virtual disk descriptor files and vSAN storage objects.
