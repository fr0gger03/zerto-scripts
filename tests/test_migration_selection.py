"""Offline selection, copy-result persistence, and cleanup scope tests."""

import importlib.util
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
cleanup = load_script("cleanup", "03_vmdk_descriptor_cleanup.py")


def disk(vpg="first", vm="vm-1", filename="disk.vmdk", **extra):
    return {"vpg_name": vpg, "vm_name": vm, "datastore": "DS",
            "source_raw_path": f"[DS] replica/{filename}", **extra}


def fake_connection(module):
    si = MagicMock()
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
                 patch.object(copier.Confirm, "ask", side_effect=[True, False, True]), \
                 patch.object(copier, "wait_for_task", side_effect=[True, False]), \
                 patch.object(copier, "console", Console(file=io.StringIO())):
                copier.main()
            statuses = [d["copy_status"] for d in selection.load_plan(str(path))["disks"]]
            self.assertEqual(statuses, ["copied", "copied", "skipped", "failed"])
            self.assertEqual(si.RetrieveContent.return_value.virtualDiskManager.CopyVirtualDisk_Task.call_count, 2)
            disconnect.assert_called_once_with(si)

    def test_cleanup_only_reads_successfully_copied_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "migration.json"
            plan = selection.create_plan([disk(vm=f"vm-{i}", filename=f"{i}.vmdk") for i in range(4)], "source.json")
            for item, status in zip(plan["disks"], ("copied", "pending", "skipped", "failed")):
                item["copy_status"] = status
            selection.save_plan(plan, str(path))
            si = fake_connection(cleanup)
            with patch("sys.argv", ["cleanup", "--dry-run", "--migration-file", str(path)]), \
                 patch.object(cleanup, "SmartConnect", return_value=si), \
                 patch.object(cleanup, "Disconnect"), \
                 patch.object(cleanup, "fetch_descriptor", return_value='ddb.iofilters = "filter"\n') as fetch, \
                 patch.object(cleanup, "upload_descriptor") as upload, \
                 patch.object(cleanup, "console", Console(file=io.StringIO())):
                cleanup.main()
            fetch.assert_called_once()
            self.assertIn("vm-0/0.vmdk", fetch.call_args.args[1])
            upload.assert_not_called()

    def test_missing_selection_does_not_fall_back_to_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            with patch("sys.argv", ["cleanup", "--migration-file", str(missing)]), \
                 patch.object(cleanup, "SmartConnect") as connect, \
                 patch.object(cleanup, "console", Console(file=io.StringIO())):
                with self.assertRaises(SystemExit):
                    cleanup.main()
            connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
