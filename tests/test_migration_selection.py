"""Offline selection, copy-result persistence, and cleanup scope tests."""

import importlib.util
from contextlib import ExitStack
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from rich.console import Console

import config
import migration_selection as selection

ROOT = Path(__file__).resolve().parents[1]
ENV = {"VCENTER_IP": "vcenter.invalid", "VCENTER_USER": "test", "VCENTER_PASSWORD": "test",
       "VCENTER_DATACENTER": "DC"}


def load_script(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    with patch.object(config, "require", return_value=ENV):
        spec.loader.exec_module(module)
    return module


copier = load_script("copier", "02_vcf_seed_copy.py")


def disk(vpg="first", vm="vm-1", filename="disk.vmdk", **extra):
    return {"vpg_name": vpg, "vm_name": vm, "datastore": "DS",
            "source_raw_path": f"[DS] replica/{filename}", **extra}


def fake_connection(module):
    si = MagicMock()
    si._stub.cookie = 'vmware_soap_session="offline-test"'
    datacenter = MagicMock(spec=module.vim.Datacenter)
    datacenter.name = "DC"
    si.RetrieveContent.return_value.rootFolder.childEntity = [datacenter]
    return si


class SelectionTests(unittest.TestCase):
    def test_choose_all(self):
        disks = [disk(), disk("second")]
        with patch.object(selection.questionary, "select") as prompt, \
             patch.object(selection.questionary, "checkbox") as checkbox:
            prompt.return_value.ask.return_value = "all"
            self.assertEqual(selection.choose_disks(disks), disks)
            checkbox.assert_not_called()

    def test_subset_retains_every_disk_in_selected_vpg(self):
        disks = [disk(), disk(filename="second.vmdk"), disk("second")]
        with patch.object(selection.questionary, "select") as mode, \
             patch.object(selection.questionary, "checkbox") as choices:
            mode.return_value.ask.return_value = "subset"
            choices.return_value.ask.return_value = ["name:first"]
            self.assertEqual(selection.choose_disks(disks), disks[:2])
            validator = choices.call_args.kwargs["validate"]
            self.assertIsNot(validator([]), True)
            self.assertIs(validator(["name:first"]), True)

    def test_identifiers_distinguish_duplicate_names(self):
        disks = [disk(vpg_identifier="one"), disk(vpg_identifier="two")]
        with patch.object(selection.questionary, "select") as mode, \
             patch.object(selection.questionary, "checkbox") as choices:
            mode.return_value.ask.return_value = "subset"
            choices.return_value.ask.return_value = ["id:two"]
            self.assertEqual(selection.choose_disks(disks), [disks[1]])

    def test_cancel_or_empty_selection(self):
        with patch.object(selection.questionary, "select") as mode, \
             patch.object(selection.questionary, "checkbox") as choices:
            mode.return_value.ask.return_value = None
            self.assertIsNone(selection.choose_disks([disk()]))
            mode.return_value.ask.return_value = "subset"
            choices.return_value.ask.return_value = None
            self.assertIsNone(selection.choose_disks([disk()]))
            choices.return_value.ask.return_value = []
            with self.assertRaises(ValueError):
                selection.choose_disks([disk()])

    def test_save_and_load_preserves_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "discovery.json"
            source.write_text(json.dumps([disk(), disk("second")]))
            path = Path(directory) / "migration.json"
            plan = selection.create_plan([disk()], str(source))
            selection.save_plan(plan, str(path))
            self.assertEqual(selection.load_plan(str(path)), plan)
            self.assertEqual(plan["disks"][0]["copy_status"], "pending")
            self.assertEqual(len(json.loads(source.read_text())), 2)
            self.assertEqual(list(Path(directory).glob(".migration.json.*")), [])

    def test_bad_schema_and_disk_fields_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            for data in ([disk()], {"schema_version": 1, "disks": [{}]},
                         {"schema_version": 1, "disks": [disk(copy_status="wrong")]}):
                path.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    selection.load_plan(str(path))


class WorkflowTests(unittest.TestCase):
    def test_select_only_saves_without_vcenter(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.json"
            source.write_text(json.dumps([disk(), disk("second")]))
            target = Path(directory) / "migration.json"
            with patch("sys.argv", ["copy", "--select-only", "--migration-file", str(target)]), \
                 patch.object(copier, "MANIFEST_FILE", str(source)), \
                 patch.object(copier, "choose_disks", return_value=[disk()]), \
                 patch.object(copier, "SmartConnect") as connect, \
                 patch.object(copier, "console", Console(file=io.StringIO())):
                copier.main()
            connect.assert_not_called()
            self.assertEqual(len(selection.load_plan(str(target))["disks"]), 1)

    def run_saved_plan(self, disks, *, prompts=None, outcomes=None, dry_run=False):
        """Run only against mocks and return durable results plus API call history."""
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path = Path(directory) / "migration.json"
            plan = selection.create_plan(disks, "source.json")
            for item, source in zip(plan["disks"], disks):
                item["copy_status"] = source.get("copy_status", "pending")
            selection.save_plan(plan, str(path))
            original = path.read_bytes()
            si = fake_connection(copier)
            events = []

            def clean(*args, **kwargs):
                events.append("cleanup")
                result = next(results)
                if isinstance(result, Exception):
                    raise result
                return result

            results = iter(outcomes or [("clean", [])] * len(disks))
            si.RetrieveContent.return_value.fileManager.MakeDirectory.side_effect = (
                lambda **kwargs: events.append("mkdir")
            )
            si.RetrieveContent.return_value.virtualDiskManager.CopyVirtualDisk_Task.side_effect = (
                lambda **kwargs: events.append("copy")
            )
            argv = ["copy", "--reuse-selection", "--migration-file", str(path)]
            if dry_run:
                argv.append("--dry-run")
            stack.enter_context(patch("sys.argv", argv))
            stack.enter_context(patch.object(copier.config, "require", return_value=ENV))
            connect = stack.enter_context(patch.object(copier, "SmartConnect", return_value=si))
            stack.enter_context(patch.object(copier, "Disconnect"))
            session = stack.enter_context(patch.object(copier.requests, "Session")).return_value
            stack.enter_context(patch.object(copier, "console", Console(file=io.StringIO())))
            stack.enter_context(patch.object(copier.Confirm, "ask", side_effect=prompts or [True] * (len(disks) + 1)))
            cleaner = stack.enter_context(patch.object(copier, "clean_source_descriptor", side_effect=clean))
            stack.enter_context(patch.object(copier, "wait_for_task", return_value=True))
            copier.main()
            if dry_run:
                self.assertEqual(path.read_bytes(), original)
            return selection.load_plan(str(path)), events, cleaner, connect, session

    def test_cleanup_precedes_folder_and_copy_using_source_url(self):
        result, events, clean, _, session = self.run_saved_plan([disk()])
        self.assertEqual(events, ["cleanup", "mkdir", "copy"])
        self.assertIn("/folder/replica/disk.vmdk?", clean.call_args.args[1])
        self.assertEqual(result["disks"][0]["cleanup_status"], "clean")
        session.close.assert_called_once()

    def test_cleanup_failure_continues_without_copying_failed_disk(self):
        result, events, _, _, _ = self.run_saved_plan(
            [disk(filename="bad.vmdk"), disk(filename="good.vmdk")],
            outcomes=[ValueError("read-back mismatch"), ("cleaned", ['ddb.iofilters = "filter"'])],
        )
        self.assertEqual(events, ["cleanup", "cleanup", "mkdir", "copy"])
        self.assertEqual([d["copy_status"] for d in result["disks"]], ["failed", "copied"])
        self.assertEqual(result["disks"][0]["cleanup_error"], "read-back mismatch")

    def test_declined_and_completed_disks_are_not_cleaned(self):
        result, events, clean, _, _ = self.run_saved_plan(
            [disk(copy_status="copied"), disk(filename="declined.vmdk")],
            prompts=[True, False],
        )
        self.assertEqual(events, [])
        clean.assert_not_called()
        self.assertEqual([d["copy_status"] for d in result["disks"]], ["copied", "skipped"])

    def test_unprotect_confirmation_declined_does_not_connect(self):
        _, events, clean, connect, _ = self.run_saved_plan([disk()], prompts=[False])
        self.assertEqual(events, [])
        clean.assert_not_called()
        connect.assert_not_called()

    def test_dry_run_keeps_saved_results_and_does_not_copy(self):
        _, events, clean, _, _ = self.run_saved_plan(
            [disk()], dry_run=True, outcomes=[("would_clean", ['ddb.sidecars = "stale"'])],
        )
        self.assertEqual(events, ["cleanup"])
        self.assertTrue(clean.call_args.kwargs["dry_run"])

    def test_dry_run_does_not_mark_declined_disk_skipped(self):
        result, events, clean, _, _ = self.run_saved_plan(
            [disk()], dry_run=True, prompts=[False],
        )
        self.assertEqual(result["disks"][0]["copy_status"], "pending")
        self.assertEqual(events, [])
        clean.assert_not_called()

    def test_select_only_and_dry_run_are_mutually_exclusive(self):
        with patch("sys.argv", ["copy", "--select-only", "--dry-run"]), \
             patch("sys.stderr", io.StringIO()), \
             patch.object(copier, "SmartConnect") as connect, \
             patch.object(copier, "console", Console(file=io.StringIO())):
            with self.assertRaises(SystemExit):
                copier.main()
        connect.assert_not_called()

    def test_dry_run_failure_keeps_saved_results(self):
        _, events, _, _, _ = self.run_saved_plan(
            [disk()], dry_run=True, outcomes=[ValueError("missing")],
        )
        self.assertEqual(events, ["cleanup"])

    def test_retry_rechecks_cleanup_and_clears_previous_error(self):
        result, events, _, _, _ = self.run_saved_plan([
            disk(copy_status="failed", cleanup_status="failed", cleanup_error="old error")
        ])
        self.assertEqual(events, ["cleanup", "mkdir", "copy"])
        self.assertNotIn("cleanup_error", result["disks"][0])
        self.assertEqual(result["disks"][0]["copy_status"], "copied")

    def test_malformed_source_is_failed_before_http_or_copy(self):
        result, events, clean, _, _ = self.run_saved_plan([
            disk(source_raw_path="[WRONG] replica/disk.vmdk")
        ])
        self.assertEqual(events, [])
        clean.assert_not_called()
        self.assertEqual(result["disks"][0]["copy_status"], "failed")

    def test_unqualified_source_is_normalized_before_cleanup(self):
        _, events, clean, _, _ = self.run_saved_plan([
            disk(source_raw_path="uuid/replica/disk.vmdk")
        ])
        self.assertEqual(events, ["cleanup", "mkdir", "copy"])
        self.assertIn("/folder/uuid/replica/disk.vmdk?", clean.call_args.args[1])

    def test_datastore_root_descriptor_has_correct_destination(self):
        self.assertEqual(config.compute_dest_paths("DS", "VM", "[DS] disk.vmdk"), (
            "[DS] disk.vmdk", "[DS] VM", "[DS] VM/disk.vmdk", "disk.vmdk",
        ))

    def test_subset_cleans_only_questionary_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.json"
            source.write_text(json.dumps([disk(), disk("unselected", filename="excluded.vmdk")]))
            target = Path(directory) / "migration.json"
            si = fake_connection(copier)
            with patch("sys.argv", ["copy", "--migration-file", str(target)]), \
                 patch.object(copier, "MANIFEST_FILE", str(source)), \
                 patch.object(copier, "choose_disks", return_value=[disk()]), \
                 patch.object(copier.config, "require", return_value=ENV), \
                 patch.object(copier, "SmartConnect", return_value=si), \
                 patch.object(copier, "Disconnect"), \
                 patch.object(copier.Confirm, "ask", return_value=True), \
                 patch.object(copier, "clean_source_descriptor", return_value=("clean", [])) as clean, \
                 patch.object(copier, "wait_for_task", return_value=True), \
                 patch.object(copier, "console", Console(file=io.StringIO())):
                copier.main()
            clean.assert_called_once()
            self.assertNotIn("excluded", clean.call_args.args[1])
            self.assertEqual(len(selection.load_plan(str(target))["disks"]), 1)

    def test_cancellation_preserves_previous_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.json"
            source.write_text(json.dumps([disk()]))
            target = Path(directory) / "migration.json"
            target.write_text("existing")
            with patch("sys.argv", ["copy", "--migration-file", str(target)]), \
                 patch.object(copier, "MANIFEST_FILE", str(source)), \
                 patch.object(copier, "choose_disks", return_value=None), \
                 patch.object(copier, "SmartConnect") as connect, \
                 patch.object(copier, "console", Console(file=io.StringIO())):
                copier.main()
            self.assertEqual(target.read_text(), "existing")
            connect.assert_not_called()

    def test_copy_results_and_resume_skip_completed_disks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "migration.json"
            plan = selection.create_plan([disk(vm=f"vm-{i}") for i in range(4)], "source.json")
            plan["disks"][0]["copy_status"] = "copied"
            selection.save_plan(plan, str(path))
            si = fake_connection(copier)
            with patch("sys.argv", ["copy", "--reuse-selection", "--migration-file", str(path)]), \
                 patch.object(copier.config, "require", return_value=ENV), \
                 patch.object(copier, "SmartConnect", return_value=si), \
                 patch.object(copier, "Disconnect") as disconnect, \
                 patch.object(copier.Confirm, "ask", side_effect=[True, True, False, True]), \
                 patch.object(copier, "clean_source_descriptor", return_value=("clean", [])) as clean, \
                 patch.object(copier, "wait_for_task", side_effect=[True, False]), \
                 patch.object(copier, "console", Console(file=io.StringIO())):
                copier.main()
            statuses = [d["copy_status"] for d in selection.load_plan(str(path))["disks"]]
            self.assertEqual(statuses, ["copied", "copied", "skipped", "failed"])
            self.assertEqual(si.RetrieveContent.return_value.virtualDiskManager.CopyVirtualDisk_Task.call_count, 2)
            self.assertEqual(clean.call_count, 2)
            self.assertEqual(selection.load_plan(str(path))["disks"][3]["cleanup_status"], "clean")
            disconnect.assert_called_once_with(si)

    def test_missing_selection_does_not_fall_back_to_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            with patch("sys.argv", ["copy", "--reuse-selection", "--migration-file", str(missing)]), \
                 patch.object(copier, "SmartConnect") as connect, \
                 patch.object(copier, "console", Console(file=io.StringIO())):
                with self.assertRaises(SystemExit):
                    copier.main()
            connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
