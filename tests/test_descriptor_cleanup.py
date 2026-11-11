"""Offline safety tests for source descriptor HTTP access and transformations."""

import unittest
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import requests

import descriptor_cleanup as cleanup


DESCRIPTOR = (
    '# Disk DescriptorFile\nversion=5\nencoding="UTF-8"\n'
    'CID=fffffffe\nparentCID=ffffffff\ncreateType="vmfs"\n'
    '# Extent description\nRW 104857600 VMFS "disk-flat.vmdk"\n'
    '#DDB\nddb.adapterType = "buslogic"\n'
)
FILTERS = 'ddb.iofilters = "hpereplication"\nddb.sidecars = "missing.vmfd"\n'


def response(text=DESCRIPTOR, status=200):
    result = MagicMock()
    result.status_code = status
    result.iter_content.return_value = [text.encode("utf-8")]
    return result


class DescriptorTests(unittest.TestCase):
    def test_exact_assignments_only_preserving_other_content(self):
        text = DESCRIPTOR + FILTERS + (
            '#ddb.iofilters = "already commented"\n'
            '  # ddb.sidecars = "also commented"\n'
            'ddb.iofiltersExtra = "keep"\nddb.sidecars.other = "keep"\n'
            '  DDB.SIDECARS\t= "stale"\n'
        )
        cleaned, changed = cleanup.scrub_descriptor(text)
        expected = text.replace('ddb.iofilters = "hpereplication"', '#ddb.iofilters = "hpereplication"')
        expected = expected.replace('ddb.sidecars = "missing.vmfd"', '#ddb.sidecars = "missing.vmfd"')
        expected = expected.replace('  DDB.SIDECARS\t=', '#  DDB.SIDECARS\t=')
        self.assertEqual(cleaned, expected)
        self.assertEqual(len(changed), 3)
        self.assertEqual(cleanup.scrub_descriptor(cleaned), (cleaned, []))

    def test_newlines_and_missing_final_newline_are_preserved(self):
        for ending in ("\n", "\r\n"):
            for final_newline in (True, False):
                text = (DESCRIPTOR + FILTERS).replace("\n", ending)
                if not final_newline:
                    text = text.rstrip("\r\n")
                cleaned, _ = cleanup.scrub_descriptor(text)
                self.assertEqual(cleaned, text.replace("ddb.iofilters", "#ddb.iofilters")
                                 .replace("ddb.sidecars", "#ddb.sidecars"))

    def test_vsan_extent_and_bom_are_unchanged(self):
        text = "\ufeff" + DESCRIPTOR.replace('"disk-flat.vmdk"', '"vsan://uuid/object"') + FILTERS
        cleaned, _ = cleanup.scrub_descriptor(text)
        self.assertTrue(cleaned.startswith("\ufeff"))
        self.assertIn('RW 104857600 VMFS "vsan://uuid/object"', cleaned)

    def test_invalid_content_is_rejected(self):
        for text in ("", "<html>login</html>", DESCRIPTOR + "\0", FILTERS,
                     DESCRIPTOR.replace("CID=fffffffe\n", ""),
                     DESCRIPTOR.replace('RW 104857600 VMFS "disk-flat.vmdk"\n', ""),
                     DESCRIPTOR + (" " * cleanup.MAX_DESCRIPTOR_BYTES)):
            with self.subTest(text=text[:30]), self.assertRaises(ValueError):
                cleanup.scrub_descriptor(text)

    def test_source_url_encodes_paths_and_query(self):
        url = cleanup.build_datastore_url(
            "vc.invalid", "DC & lab", "DS #1",
            "[DS #1] uuid/nested folder/disk #1%?.vmdk",
        )
        parsed = urlsplit(url)
        self.assertEqual(parsed.path, "/folder/uuid/nested%20folder/disk%20%231%25%3F.vmdk")
        self.assertEqual(parse_qs(parsed.query), {"dcPath": ["DC & lab"], "dsName": ["DS #1"]})

    def test_invalid_paths_and_data_extents_are_rejected(self):
        for path in ("[OTHER] f/d.vmdk", "f/d.vmdk", "[DS] /d.vmdk",
                     "[DS] f/../d.vmdk", "[DS] f//d.vmdk", "[DS] f\\d.vmdk",
                     "[DS] f/d.txt", "[DS] f/d-flat.vmdk", "[DS] f/d-delta.vmdk",
                     "[DS] f/d-sesparse.vmdk", "[DS] f/d-s001.vmdk", "[DS] f/d-ctk.vmdk"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                cleanup.build_datastore_url("vc", "DC", "DS", path)

    def test_write_is_followed_by_verified_read(self):
        original = DESCRIPTOR + FILTERS
        cleaned, _ = cleanup.scrub_descriptor(original)
        session = MagicMock()
        session.get.side_effect = [response(original), response(cleaned)]
        session.put.return_value = response(status=201)
        status, changed = cleanup.clean_source_descriptor(session, "https://vc/folder/d.vmdk", True)
        self.assertEqual(status, "cleaned")
        self.assertEqual(len(changed), 2)
        self.assertEqual([c[0] for c in session.method_calls], ["get", "put", "get"])
        self.assertEqual(session.put.call_args.kwargs["data"], cleaned.encode("utf-8"))
        self.assertFalse(session.put.call_args.kwargs["allow_redirects"])
        self.assertEqual(session.get.call_args.kwargs["timeout"], 30)
        self.assertTrue(session.get.call_args.kwargs["verify"])

    def test_clean_and_dry_run_do_not_write(self):
        for text, dry_run, expected in (
            (DESCRIPTOR, False, "clean"),
            (DESCRIPTOR + FILTERS, True, "would_clean"),
        ):
            session = MagicMock()
            session.get.return_value = response(text)
            status, _ = cleanup.clean_source_descriptor(session, "https://vc/file", False, dry_run=dry_run)
            self.assertEqual(status, expected)
            session.put.assert_not_called()
            session.get.assert_called_once()

    def test_readback_mismatch_prevents_success(self):
        session = MagicMock()
        session.get.side_effect = [response(DESCRIPTOR + FILTERS), response(DESCRIPTOR + FILTERS)]
        session.put.return_value = response(status=204)
        with self.assertRaisesRegex(ValueError, "read-back"):
            cleanup.clean_source_descriptor(session, "https://vc/file", True)

    def test_http_errors_timeouts_and_redirects_propagate(self):
        for failure in (requests.HTTPError("404"), requests.Timeout("timeout")):
            session = MagicMock()
            session.get.side_effect = failure
            with self.assertRaises(requests.RequestException):
                cleanup.clean_source_descriptor(session, "https://vc/file", True)
            session.put.assert_not_called()
        session = MagicMock()
        session.get.return_value = response(status=302)
        with self.assertRaises(ValueError):
            cleanup.fetch_descriptor(session, "https://vc/file", True)
        session.get.return_value.close.assert_called_once()

    def test_http_fetch_failure_is_closed_without_upload(self):
        session = MagicMock()
        session.get.return_value = response(status=404)
        session.get.return_value.raise_for_status.side_effect = requests.HTTPError("not found")
        with self.assertRaises(requests.HTTPError):
            cleanup.clean_source_descriptor(session, "https://vc/file", True)
        session.get.return_value.close.assert_called_once()
        session.put.assert_not_called()

    def test_failed_upload_or_redirect_is_not_success(self):
        for status in (302, 403):
            session = MagicMock()
            session.get.return_value = response(DESCRIPTOR + FILTERS)
            session.put.return_value = response(status=status)
            if status == 403:
                session.put.return_value.raise_for_status.side_effect = requests.HTTPError("forbidden")
            with self.assertRaises((ValueError, requests.HTTPError)):
                cleanup.clean_source_descriptor(session, "https://vc/file", True)
            session.get.assert_called_once()
            session.put.return_value.close.assert_called_once()

    def test_oversized_or_binary_download_is_rejected_and_closed(self):
        for data in (b"x" * (cleanup.MAX_DESCRIPTOR_BYTES + 1), b"\xff\x00"):
            session = MagicMock()
            session.get.return_value = response()
            session.get.return_value.iter_content.return_value = [data]
            with self.assertRaises((ValueError, UnicodeError)):
                cleanup.fetch_descriptor(session, "https://vc/file", True)
            session.get.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
