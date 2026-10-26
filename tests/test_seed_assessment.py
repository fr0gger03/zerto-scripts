"""Offline checks: uv run python -m unittest discover -s tests -v."""

import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import requests
from rich.console import Console

from journal_stats import assess, get_val, volume_type

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("discovery", ROOT / "01_zerto_discover_and_export.py")
discovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(discovery)


def vm(identifier="vm-1", vpg="group-1", name="VM"):
    return {"VmIdentifier": identifier, "VmName": name, "VpgIdentifier": vpg, "VpgName": vpg}


def vol(kind, used=30, provisioned=100, identifier="vm-1", vpg="group-1", path=None):
    return {"VolumeType": kind, "ProtectedVm": {"Identifier": identifier},
            "Vpg": {"Identifier": vpg, "Name": vpg},
            "Size": {"UsedInBytes": used, "ProvisionedInBytes": provisioned},
            "Datastore": {"Name": "DS"},
            "Path": {"Full": path, "FileName": "disk.vmdk", "Relative": "folder/disk.vmdk"}}


class AssessmentTests(unittest.TestCase):
    def test_multivolume_sum_and_case_variants(self):
        journal = vol(2, 10)
        journal = {k.upper(): v for k, v in journal.items()}
        result = assess([vm()], [journal, vol("Journal", 20), vol(1, 100)], 30)
        row = result["vms"][0]
        self.assertEqual(row["journal_used_bytes"], 30)
        self.assertEqual(row["journal_provisioned_bytes"], 200)
        self.assertEqual(row["journal_replica_used_percent"], 30)
        self.assertEqual(row["journal_ratio_flag"], "WITHIN THRESHOLD")
        self.assertEqual(volume_type({"volumeType": "1"}), "recovery")
        self.assertEqual(get_val({"OwningVM": 1}, "owningVm"), 1)

    def test_threshold_and_zero_journal(self):
        for used, expected in ((0, "WITHIN THRESHOLD"), (30, "WITHIN THRESHOLD"),
                               (31, "ABOVE THRESHOLD")):
            with self.subTest(used=used):
                row = assess([vm()], [vol("Journal", used), vol("Recovery", 100)], 30)["vms"][0]
                self.assertEqual(row["journal_ratio_flag"], expected)

    def test_missing_invalid_and_zero_denominator(self):
        for value in (None, -1, True, 2.5):
            with self.subTest(value=value):
                result = assess([vm()], [vol("Journal", value), vol("Recovery", 100)], 30)
                self.assertIsNone(result["vms"][0]["journal_used_bytes"])
                self.assertEqual(result["vpgs"][0]["journal_ratio_flag"], "UNKNOWN")
        row = assess([vm()], [vol("Journal"), vol("Recovery", 0)], 30)["vms"][0]
        self.assertIsNone(row["journal_replica_used_percent"])

    def test_missing_volumes_are_not_zero(self):
        result = assess([vm()], [], 30)
        self.assertEqual(len(result["vms"]), 1)
        self.assertIsNone(result["vms"][0]["journal_used_bytes"])
        self.assertEqual(result["vms"][0]["journal_ratio_flag"], "UNKNOWN")

    def test_partial_metric_is_unknown(self):
        result = assess([vm()], [vol(2, 20), vol(2, None), vol(1, 100)], 30)
        self.assertIsNone(result["vms"][0]["journal_used_bytes"])

    def test_weighted_vpg_ratio_and_individual_flags(self):
        volumes = [vol(2, 50), vol(1, 100), vol(2, 10, identifier="vm-2"), vol(1, 900, identifier="vm-2")]
        result = assess([vm(), vm("vm-2")], volumes, 30)
        group = result["vpgs"][0]
        self.assertEqual(group["journal_replica_used_percent"], 6)
        self.assertEqual(group["above_threshold_vm_count"], 1)
        self.assertEqual(group["journal_ratio_flag"], "WITHIN THRESHOLD")

    def test_duplicate_names_and_multiple_vpgs(self):
        volumes = [vol(2), vol(1, 100), vol(2, 60, vpg="group-2"), vol(1, 100, vpg="group-2")]
        result = assess([vm(), vm(vpg="group-2")], volumes, 30)
        self.assertEqual(len(result["vpgs"]), 2)
        self.assertEqual([r["journal_used_bytes"] for r in result["vms"]], [30, 60])

    def test_ambiguous_and_orphan_volumes(self):
        ambiguous = vol(2)
        ambiguous.pop("Vpg")
        result = assess([vm(), vm(vpg="group-2")], [ambiguous], 30)
        self.assertTrue(result["warnings"])
        orphan = vol(2, identifier="missing")
        result = assess([vm()], [vol(2), vol(1, 100), orphan], 30)
        self.assertEqual(result["vms"][0]["journal_ratio_flag"], "UNKNOWN")

    def test_duplicate_physical_volume(self):
        journal = vol(2, path="[DS] journal/disk.vmdk")
        row = assess([vm()], [journal, journal, vol(1, 100)], 30)["vms"][0]
        self.assertEqual(row["journal_used_bytes"], 30)
        self.assertEqual(row["journal_ratio_flag"], "UNKNOWN")

    def test_vpg_identifier_controls_grouping_even_if_names_differ(self):
        second = vm("vm-2")
        second["VpgName"] = "renamed"
        result = assess([vm(), second], [vol(2), vol(1, 100),
                        vol(2, identifier="vm-2"), vol(1, 100, identifier="vm-2")], 30)
        self.assertEqual(len(result["vpgs"]), 1)
        self.assertEqual(result["vpgs"][0]["vm_count"], 2)

    def test_missing_inventory_vpg_id_can_be_resolved_from_volumes(self):
        inventory = vm()
        inventory.pop("VpgIdentifier")
        result = assess([inventory], [vol(2), vol(1, 100)], 30)
        self.assertEqual(result["vms"][0]["vpg_identifier"], "group-1")
        self.assertEqual(result["vms"][0]["journal_ratio_flag"], "WITHIN THRESHOLD")

    def test_custom_threshold_and_int64_byte_counts(self):
        row = assess([vm()], [vol(2, 40 * 1024**3), vol(1, 100 * 1024**3)], 40)["vms"][0]
        self.assertEqual(row["journal_used_bytes"], 40 * 1024**3)
        self.assertEqual(row["journal_replica_used_percent"], 40)
        self.assertEqual(row["journal_ratio_flag"], "WITHIN THRESHOLD")

    def test_incomplete_member_invalidates_vpg_total(self):
        result = assess([vm(), vm("vm-2")], [vol(2), vol(1, 100), vol(1, 100, identifier="vm-2")], 30)
        self.assertIsNone(result["vpgs"][0]["journal_used_bytes"])
        self.assertEqual(result["vpgs"][0]["unknown_vm_count"], 1)

    def test_terminal_unit_conversion(self):
        report = assess([vm()], [vol(2, 1024**3), vol(1, 10 * 1024**3)], 30)
        report["threshold_percent"] = 30
        for units, expected in (("MiB", "1,024.00"), ("GiB", "1.00")):
            output = io.StringIO()
            with patch.object(discovery, "console", Console(file=output, width=200)):
                discovery.print_assessment(report, units)
            self.assertIn(expected, output.getvalue())
            self.assertIn(units, output.getvalue())

    def test_disk_details_reconcile_with_vm_totals(self):
        volumes = [vol(2, 10, path="[DS] journals/one.vmdk"),
                   vol(2, 20, path="[DS] journals/two.vmdk"),
                   vol(1, 100, path="[DS] replicas/base.vmdk"),
                   vol(0, 200), vol("Scratch", 300)]
        report = assess([vm()], volumes, 30)
        disks = report["volume_details"]
        self.assertEqual(len(disks), 3)
        self.assertEqual(disks[0]["volume_type"], "replica")
        self.assertEqual(disks[0]["vmdk_path"], "[DS] replicas/base.vmdk")
        self.assertEqual(disks[0]["vm_name"], "VM")
        self.assertTrue(all(d["included_in_totals"] for d in disks))
        for kind in ("journal", "replica"):
            for metric in ("used", "provisioned"):
                self.assertEqual(
                    sum(d[f"{metric}_bytes"] for d in disks if d["volume_type"] == kind),
                    report["vms"][0][f"{kind}_{metric}_bytes"],
                )

    def test_duplicate_and_orphan_disks_remain_visible(self):
        journal = vol(2, path="[DS] journal.vmdk")
        report = assess([vm()], [journal, journal, vol(1, 100),
                        vol(2, identifier="orphan", path="[DS] orphan.vmdk")], 30)
        self.assertEqual(len(report["volume_details"]), 4)
        excluded = [d for d in report["volume_details"] if not d["included_in_totals"]]
        self.assertEqual({d["attribution_status"] for d in excluded}, {"duplicate", "unassigned"})
        self.assertEqual(report["vms"][0]["journal_used_bytes"], 30)

    def test_disk_details_preserve_unknown_sizes_and_missing_paths(self):
        disk = {"VOLUMETYPE": 2, "PROTECTEDVM": {"IDENTIFIER": "vm-1"},
                "SIZE": {"USEDINBYTES": None, "PROVISIONEDINBYTES": "1024"}}
        row = assess([vm()], [disk], 30)["volume_details"][0]
        self.assertIsNone(row["vmdk_path"])
        self.assertIsNone(row["used_bytes"])
        self.assertEqual(row["provisioned_bytes"], 1024)
        self.assertEqual(row["vm_identifier"], "vm-1")

    def test_disk_table_keeps_full_paths_on_narrow_terminal(self):
        path = "[DS] folder/replica.vmdk"
        report = assess([vm()], [vol(2, path="[DS] journal.vmdk"), vol(1, 100, path=path)], 30)
        report["threshold_percent"] = 30
        output = io.StringIO()
        with patch.object(discovery, "console", Console(file=output, width=100)):
            discovery.print_assessment(report, "MiB", show_volume_details=True)
        rendered = output.getvalue()
        self.assertIn("Replica and journal VMDKs (MiB)", rendered)
        default_output = io.StringIO()
        with patch.object(discovery, "console", Console(file=default_output, width=100)):
            discovery.print_assessment(report, "MiB")
        self.assertNotIn("Replica and journal VMDKs", default_output.getvalue())
        self.assertIn("Per-VM seed assessment", default_output.getvalue())
        # Wrapping can split paths, but must not replace path characters with ellipses.
        self.assertNotIn("…", rendered)
        self.assertIn("replica.vmdk", rendered)
        self.assertIn("journal.vmdk", rendered)


class ClientTests(unittest.TestCase):
    def client(self, mode):
        client = discovery.ZertoClient("zvm.example", 443, mode, True, 12)
        client.session = MagicMock()
        client.session.headers = {}
        return client

    @patch.object(discovery.config, "require", return_value={"ZVM_CLIENT_ID": "client", "ZVM_CLIENT_SECRET": "secret"})
    def test_oauth_uses_bearer_without_legacy_session(self, require):
        client = self.client("oauth")
        client.session.post.return_value.json.return_value = {"access_token": "token"}
        client.authenticate()
        self.assertEqual(client.session.headers, {"Authorization": "Bearer token"})
        client.close()
        client.session.delete.assert_not_called()

    @patch.object(discovery.config, "require", return_value={"ZVM_USER": "user", "ZVM_PASSWORD": "secret"})
    def test_legacy_auth_and_logout(self, require):
        client = self.client("legacy")
        client.session.post.return_value.headers = {"x-zerto-session": "session"}
        client.authenticate()
        self.assertEqual(client.session.headers, {"x-zerto-session": "session"})
        self.assertEqual(client.session.post.call_args.kwargs["auth"], ("user", "secret"))
        client.close()
        client.session.delete.assert_called_once()

    def test_http_and_payload_errors(self):
        client = self.client("oauth")
        client.session.get.return_value.raise_for_status.side_effect = requests.HTTPError()
        with self.assertRaises(requests.HTTPError):
            client.get_list("volumes")
        client.session.get.return_value.raise_for_status.side_effect = None
        client.session.get.return_value.json.return_value = {"error": "bad"}
        with self.assertRaises(ValueError):
            client.get_list("volumes")
        self.assertEqual(client.session.get.call_args.kwargs["timeout"], 12)

    @patch.object(discovery.config, "require", return_value={"ZVM_CLIENT_ID": "client", "ZVM_CLIENT_SECRET": "secret"})
    def test_missing_auth_token(self, require):
        client = self.client("oauth")
        client.session.post.return_value.json.return_value = {}
        with self.assertRaisesRegex(ValueError, "access token"):
            client.authenticate()

    def test_failed_collection_does_not_overwrite_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text("existing")
            assessment = Path(directory) / "assessment.json"
            client = MagicMock()
            client.get_list.side_effect = requests.HTTPError()
            with patch("sys.argv", ["discovery", "--assessment-file", str(assessment)]), \
                 patch.object(discovery.config, "MANIFEST_FILE", str(manifest)), \
                 patch.object(discovery.config, "require", return_value={"ZVM_IP": "zvm.example"}), \
                 patch.object(discovery, "ZertoClient", return_value=client), \
                 patch.object(discovery, "console", Console(file=io.StringIO())):
                with self.assertRaises(requests.HTTPError):
                    discovery.main()
            self.assertEqual(manifest.read_text(), "existing")
            self.assertFalse(assessment.exists())
            client.close.assert_called_once()

    def test_cli_validation(self):
        with patch("sys.argv", ["discovery", "--auth-mode", "legacy"]):
            args = discovery.parse_args()
            self.assertEqual(args.port, 9669)
            self.assertFalse(args.show_volume_details)
        with patch("sys.argv", ["discovery", "--show-volume-details"]):
            self.assertTrue(discovery.parse_args().show_volume_details)
        with patch("sys.argv", ["discovery", "--threshold", "nan"]), patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                discovery.parse_args()

    def test_main_exports_compatible_manifest_and_assessment(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            assessment = Path(directory) / "assessment.json"
            client = MagicMock()
            client.get_list.side_effect = [[], [vm()], [vol(2), vol(1, 100, path="[DS] folder/disk.vmdk")]]
            output = io.StringIO()
            with patch("sys.argv", ["discovery", "--assessment-file", str(assessment)]), \
                 patch.object(discovery.config, "MANIFEST_FILE", str(manifest)), \
                 patch.object(discovery.config, "require", return_value={"ZVM_IP": "zvm.example"}), \
                 patch.object(discovery, "ZertoClient", return_value=client), \
                 patch.object(discovery, "console", Console(file=output, width=180)):
                discovery.main()
            disks = json.loads(manifest.read_text())
            report = json.loads(assessment.read_text())
            self.assertEqual(len(disks), 1)
            self.assertEqual(set(disks[0]), {"vm_name", "vpg_name", "datastore", "source_raw_path", "ui_display_path", "destination_seed_path"})
            self.assertEqual(disks[0]["destination_seed_path"], "[DS] VM/disk.vmdk")
            self.assertEqual(report["vms"][0]["journal_ratio_flag"], "WITHIN THRESHOLD")
            self.assertEqual(report["schema_version"], 3)
            self.assertEqual(len(report["volume_details"]), 2)
            self.assertEqual(report["volume_details"][0]["vmdk_path"], "[DS] folder/disk.vmdk")
            self.assertIn("not unique changed bytes", output.getvalue())
            self.assertIn("context only", output.getvalue())
            self.assertIn("Flags do not predict the faster method", output.getvalue())
            self.assertNotIn("FULL SYNC RECOMMENDED", output.getvalue())
            self.assertNotIn("SEED CANDIDATE", output.getvalue())
            for row in report["vms"] + report["vpgs"]:
                self.assertNotIn("recommendation", row)
                self.assertIn("journal_ratio_flag", row)
            self.assertNotIn("full_sync_vm_count", report["vpgs"][0])
            client.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
