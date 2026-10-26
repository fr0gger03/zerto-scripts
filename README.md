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
│ 3. Execute vSphere Seed Copy   │ ──► Run 02_vcf_seed_copy.py
└────────────────────────────────┘     Server-side VMDK copy on vSAN/VMFS
               │
               ▼
┌────────────────────────────────┐
│ 4. Clean I/O Filter References │ ──► Run 03_vmdk_descriptor_cleanup.py
└────────────────────────────────┘     Comments out stale ddb.iofilters/ddb.sidecars

```

---

## Prerequisites

| Requirement | Details |
| --- | --- |
| **Python Engine** | Python 3.13+ (Managed via `uv` or standard `pip`) |
| **Python Libraries** | `requests`, `pyvmomi`, `rich`, `python-dotenv` |
| **Zerto Permissions** | ZVMA / ZVM Keycloak Client ID & Client Secret |
| **vCenter Permissions** | Account with `Datastore.FileManagement` privileges |

### Installation

Install dependencies using `uv` or `pip`:

```bash
# Using uv (recommended)
uv sync

# Or standard pip
pip install requests pyvmomi rich python-dotenv

```

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
| `JOURNAL_THRESHOLD_PERCENT` | Script 1 | Optional. Screening threshold, default `30` |
| `ASSESSMENT_FILE` | Script 1 | Optional. Assessment JSON, default `zerto_seed_assessment.json` |
| `ZVM_CLIENT_ID` | Script 1 | Keycloak OAuth client ID (see [keycloak.md](keycloak.md)) |
| `ZVM_CLIENT_SECRET` | Script 1 | Keycloak OAuth client secret |
| `VCENTER_IP` | Script 2 | vCenter Server IP or FQDN (no scheme) |
| `VCENTER_USER` | Script 2 | vCenter account with `Datastore.FileManagement` |
| `VCENTER_PASSWORD` | Script 2 | vCenter account password |
| `VCENTER_DATACENTER` | Script 2 | vCenter Datacenter object name |
| `MANIFEST_FILE` | Both | Optional. Manifest filename (default `zerto_seeds_manifest.json`) |
| `VERIFY_SSL` | Both | Optional. `true` to enforce TLS verification (default `false` for self-signed certs) |

Environment variables exported in your shell (or injected by a secret manager
such as `op run --env-file=.env -- uv run ...`) take precedence over the `.env` file,
so credentials never have to touch disk if you prefer.

Both scripts fail fast with a clear message listing every missing variable.

---

## Repository Structure

* `01_zerto_discover_and_export.py` — Connects to Zerto ZVMA REST API, maps protected VMs to target ESXi hosts/VRAs, displays a formatted terminal table, and generates `zerto_seeds_manifest.json`.
* `02_vcf_seed_copy.py` — Imports the manifest, connects to vCenter via `pyVmomi`, creates standard seed folders, and executes server-side VMDK copies.
* `03_vmdk_descriptor_cleanup.py` — Imports the manifest, connects to vCenter, and comments out stale `ddb.iofilters` / `ddb.sidecars` references in each copied seed disk's descriptor file (see [Step 4](#step-4-clean-up-lingering-io-filter-references)).
* `config.py` — Shared `.env` loader, required-variable validation, and the `[Datastore] VM_Name/VMDK` path logic shared by scripts 2 and 3.
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
uv run 01_zerto_discover_and_export.py

```
![alt text](images/move-vmdk-01.png)

![alt text](images/move-vmdk-02.png)

3. Review the per-VM and per-VPG journal assessment before deciding to proceed.
   The default policy flags journal-used / replica-used ratios above 30% as
   `FULL SYNC RECOMMENDED`; missing or ambiguous measurements are `UNKNOWN`.
   This is a storage heuristic, not a transfer estimate. See the
   [decision guidance](seed-assessment.md) for interpretation and limitations.

```bash
# Optional: use a 40% threshold and display MiB instead of GiB
uv run 01_zerto_discover_and_export.py --threshold 40 --units MiB
```

4. Inspect `zerto_seed_assessment.json` and `zerto_seeds_manifest.json`.
   The replica/journal VMDK table shows current disk paths and individual sizes.
   Paths and individual sizes are always saved in the assessment's `volume_details` rows.
   Recommendations do not filter the manifest: keep only selected disk entries
   before supplying it to scripts 02–03, which process every entry.

### Step 2: Release File Locks in Zerto

Before moving or copying replica files, ESXi file locks held by Zerto VRAs must be released:

1. Open the **Zerto Management Interface**.
2. Select the VPG(s) to migrate and click **Delete VPG** (or remove specific VMs).
3. **CRITICAL:** Select **"Keep target disks"** when prompted.
4. Confirm deletion. Zerto will detach the VMDKs from the target VRA.

![alt text](images/zerto-delete-vpg.png)

### Step 3: Copy VMDKs to VCF Seed Directories

Examine your vSphere datastore to confirm current location of replica disks


Run Script 2 after Zerto has released the file locks.

1. Set your vCenter credentials in the same `.env` file:
```bash
VCENTER_IP=your.vcenter.ip.or.fqdn
VCENTER_USER=administrator@vsphere.local
VCENTER_PASSWORD=YourVcenterPasswordHere
VCENTER_DATACENTER=Datacenter-DR

```


2. Execute the copy script:
```bash
uv run 02_vcf_seed_copy.py

```

3. Confirm the prompt for each disk. The script creates the target `[Datastore] VM_Name` directory on vSAN/VMFS and initiates a `VirtualDiskManager` copy task.

![alt text](images/move-vmdk-04.png)

### Step 4: Clean Up Lingering I/O Filter References

If the original Zerto replica had an I/O filter attached (e.g. `vmwarelwd`), the
copied seed disk's descriptor can retain `ddb.iofilters` / `ddb.sidecars` lines
that reference sidecar files no longer present on the target. Left in place,
these can cause the eventual VM to fail to power on with an error like
`Cannot open the disk '...' or one of the snapshot disks it depends on.`
See [Broadcom KB 334555](https://knowledge.broadcom.com/external/article/334555/unable-to-power-on-vm-when-iofilters-is.html).

Run Script 3 after Script 2 has finished copying:

```bash
# Preview what would change, without writing anything
uv run 03_vmdk_descriptor_cleanup.py --dry-run

# Apply, confirming each affected VM
uv run 03_vmdk_descriptor_cleanup.py

# Apply without per-VM prompts
uv run 03_vmdk_descriptor_cleanup.py --yes

```

The script reuses the same `VCENTER_*` variables as Script 2 and connects to
vCenter's datastore HTTP file-access API (via the same `pyVmomi` session) to
read and rewrite only the small text descriptor file — never the large
`-flat.vmdk` data extent. Matching lines are commented out (`#ddb.iofilters = ...`),
not deleted, per the KB's guidance, and the script is safe to re-run: VMs with
no matching lines, or already-commented lines, are reported and left untouched.
No separate backup is made, since Script 2 already copied the seed disk out from
the original Zerto target disk.

---

## Important Technical Notes

* **Credential Handling:** Credentials live only in the git-ignored `.env` file (or your shell/secret manager). If you ever committed real credentials to this repository, rotate the Zerto Keycloak client secret and the vCenter account password — removing them from the working tree does not remove them from git history.
* **Server-Side Processing:** All VMDK copy operations take place 100% server-side within the vSphere ESXi storage stack. Large file transfers do **not** route over your local workstation or VPN connection.
* **vSAN Compatibility:** Script 2 automatically handles datastore bracket formatting (`[DatastoreName]`) and utilizes `VirtualDiskManager.CopyVirtualDisk_Task` to safely duplicate virtual disk descriptor files and vSAN storage objects.
